"""Model-controlled plans, execution proofs and explicit acceptance checks.

Conversation setup does not call a planner. The model may use update_plan;
legacy explicit make_plan callers remain supported without synthesized steps.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

from sqlalchemy.orm import Session

VALID_PHASES = ("search", "read", "analyze", "write", "audit", "final")


def _step_identity(step):
    return (step["title"], step["phase"], step.get("tool_name"),
            json.dumps(step.get("tool_arguments"), sort_keys=True, ensure_ascii=False))


def merge_model_plan(existing: list[dict], proposed: list[dict]) -> list[dict]:
    """Revise methods without erasing unfinished obligations or execution proof."""
    normalized = _normalize(proposed)
    by_identity = {_step_identity(step): step for step in existing}
    result = []
    for step in normalized:
        previous = by_identity.get(_step_identity(step))
        if previous:
            retained = deepcopy(previous)
            for key in ("requirement_id", "requirement_ids"):
                if key in step:
                    retained[key] = deepcopy(step[key])
            result.append(retained)
        else:
            result.append(step)
    identities = {_step_identity(step) for step in result}
    # User checks and unclassified legacy obligations cannot disappear by
    # omission. Server-classified model methods may be replaced; their durable
    # history lives in the goal ledger rather than blocking the active plan.
    retained = [step for step in existing if _step_identity(step) not in identities
                and (step.get("status") == "done"
                     or step.get("requirement_origin") != "model_plan"
                     or step.get("requirement_required") is not False)]
    final_at = next((i for i, step in enumerate(result) if step["phase"] == "final"), len(result))
    result[final_at:final_at] = retained
    return result


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 花括号兜底
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def make_plan(db: Session, llm, question: str) -> list[dict]:
    """返回待办步骤列表：[{"title","phase","status":"pending"}]。"""
    try:
        resp = llm.chat(
            [{"role": "system", "content": "你是任务规划器，根据实际请求规划，只输出 JSON。"},
             {"role": "user", "content": _wrap(question)}],
            tools=None, temperature=0.1,
        )
        data = _extract_json(resp.content)
        raw = (data or {}).get("steps") if data else None
        steps = []
        if isinstance(raw, list):
            for s in raw:
                if not isinstance(s, dict):
                    continue
                title = str(s.get("title", "")).strip()
                phase = str(s.get("phase", "")).strip()
                if not title:
                    continue
                if phase not in VALID_PHASES:
                    phase = "analyze"
                steps.append({"title": title, "phase": phase})
        if steps:
            return _normalize(steps)
    except Exception as exc:  # noqa: BLE001 — 规划失败不影响主流程，不虚构步骤
        from ..request_resources import raise_if_resource_boundary
        raise_if_resource_boundary(exc)
        pass
    return []


def _wrap(question: str) -> str:
    from .prompts import PLANNER_PROMPT

    return f"{PLANNER_PROMPT}\n用户任务：{question}"


def _normalize(steps: list[dict]) -> list[dict]:
    """Add execution state without inventing steps the model did not propose."""
    out = []
    for step in steps:
        normalized = {"title": step["title"], "phase": step["phase"],
                      "status": "pending", "evidence": []}
        if step.get("acceptance"):
            normalized["acceptance"] = step["acceptance"]
        for key in ("requirement_id", "requirement_ids"):
            if step.get(key):
                normalized[key] = deepcopy(step[key])
        if step.get("tool_name"):
            normalized["tool_name"] = step["tool_name"]
            normalized["tool_arguments"] = deepcopy(step.get("tool_arguments", {}))
        out.append(normalized)
    return out


def advance(plan: list[dict], phase: str) -> list[dict]:
    """进入某 phase 时只标记当前步骤，不把先前步骤推定为完成。

    返回更新后的 plan（原地修改并返回）。
    """
    target = next((i for i, s in enumerate(plan) if s["phase"] == phase
                   and s["status"] != "done"), None)
    if target is None:
        return plan
    plan[target]["status"] = "in_progress"
    return plan


def complete_one(plan: list[dict], phase: str, evidence: dict) -> bool:
    """Only matching tool/target proof or actual answer/delivery can finish a step.

    Legacy unbound tool steps remain unverified instead of accepting an
    unrelated operation in the same broad phase as proof of completion.
    """
    def matches(expected, actual):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and matches(value, actual[key]) for key, value in expected.items())
        return type(expected) is type(actual) and expected == actual

    def proves(step):
        if evidence.get("kind") == "tool_result":
            return (evidence.get("status") == "completed"
                    and step.get("tool_name") not in (None, "update_plan")
                    and step.get("tool_name") == evidence.get("tool_name")
                    and isinstance(step.get("tool_arguments"), dict)
                    and matches(step["tool_arguments"], evidence.get("arguments")))
        if step.get("tool_name"):
            return False
        return ((phase in ("analyze", "write") and evidence.get("kind") == "answer")
                or (phase == "audit" and evidence.get("kind") == "citation_audit"
                    and evidence.get("status") == "passed")
                or (phase == "final" and evidence.get("kind") == "delivery"))

    target = next((step for step in plan
                   if (step.get("phase") == phase or evidence.get("kind") == "tool_result")
                   and step.get("status") != "done"
                   and not step.get("acceptance") and proves(step)), None)
    if target is None:
        return False
    target["status"] = "done"
    target.setdefault("evidence", []).append(evidence)
    return True


def complete_requirement(plan: list[dict], acceptance: str, evidence: dict) -> bool:
    """完成由明确用户要求生成的验收项。"""
    if acceptance == "project_file" and evidence.get("status") != "applied":
        return False
    if (acceptance == "successful_command"
            and (evidence.get("status") != "succeeded" or evidence.get("exit_code") != 0)):
        return False
    target = next((step for step in plan
                   if step.get("acceptance") == acceptance and step.get("status") != "done"), None)
    if target is None:
        return False
    target["status"] = "done"
    target.setdefault("evidence", []).append(evidence)
    return True


def add_acceptance_requirements(plan: list[dict], question: str) -> list[dict]:
    """把明确要求的文件产物和测试/验证加入可验收清单。"""
    from ..goal_candidates import mask_policy_clauses, explicit_action
    question = mask_policy_clauses(question)
    text = question.lower()
    requirements = []
    full_scope = r"(?:全部|所有|整个|完整|全文|逐一|all|every|entire|whole)"
    source_term = r"(?:文件|文档|文件夹|目录|论文|文献|files?|documents?|papers?|folder|directory)"
    full_match = re.search(rf"{full_scope}.{{0,12}}{source_term}|{source_term}.{{0,12}}{full_scope}", text)
    if full_match and explicit_action(question, r"(?:读取|阅读|检查|处理|read|inspect)"):
        named_file = re.search(r"(?:[\w.-]+[\\/])*[\w.-]+\.(?:txt|md|rst|py|js|ts|json|csv|html|xml|ya?ml|toml)", question, re.I)
        if named_file and re.search(r"(?:整个|完整|全文|entire|whole).{0,12}文件|file.{0,12}(?:entire|whole)", text):
            path = named_file.group(0).replace("\\", "/").casefold()
            requirements.append((f"full_file:{path}", f"完整读取指定文件：{named_file.group(0)}"))
        else:
            requirements.append(("source_coverage", "核对要求范围内的全部来源均已处理"))
    file_term = r"(?:文件|文档|word|docx|pdf|xlsx|pptx)"
    file_action = r"(?:生成|创建|新建|保存|导出|写入|produce|create|save|export)"
    from ..artifact_export_intents import project_file_text
    if explicit_action(project_file_text(question), rf"{file_action}.{{0,18}}{file_term}|save\s+as|save\s+to"):
        requirements.append(("project_file", "确认指定文件已写入项目"))
    if explicit_action(question, r"(?:运行|执行|跑).{0,12}(?:测试|检查|校验|验证|构建|命令)|(?:run|execute)\b.{0,18}(?:tests?|checks?|build|command)|\b(?:compile|build)\b"):
        requirements.append(("successful_command", "运行要求的检查并确认退出码为 0"))
    for acceptance, title in requirements:
        if any(step.get("acceptance") == acceptance for step in plan):
            continue
        insert_at = next((i for i, step in enumerate(plan) if step.get("phase") == "final"), len(plan))
        phase = ("read" if acceptance.startswith("full_file:") or acceptance == "source_coverage"
                 else "write" if acceptance == "project_file" else "audit")
        plan.insert(insert_at, {"title": title, "phase": phase, "status": "pending",
                                "evidence": [], "acceptance": acceptance})
    return plan


def record_file_read(plan: list[dict], path: str, page: dict) -> bool:
    """累计同一文件的连续读取页；只有覆盖完整且哈希一致才完成清单项。"""
    normalized_path = str(path or "").replace("\\", "/").casefold()
    target = next((step for step in plan
                   if step.get("acceptance") == f"full_file:{normalized_path}"
                   and step.get("status") != "done"), None)
    if target is None:
        return False
    offset = page.get("offset")
    total = page.get("total_chars")
    next_offset = page.get("next_offset")
    digest = page.get("sha256")
    if (type(offset) is not int or offset < 0 or type(total) is not int or total < 0
            or not isinstance(digest, str) or not digest
            or (next_offset is not None and (type(next_offset) is not int or next_offset <= offset))):
        return False
    end = next_offset if next_offset is not None else total
    proof = {"kind": "file_page", "path": path, "offset": offset, "end": end,
             "total_chars": total, "sha256": digest}
    pages = target.setdefault("evidence", [])
    if not any(item.get("offset") == offset and item.get("sha256") == digest for item in pages):
        pages.append(proof)
    matching = sorted((item for item in pages
                       if item.get("sha256") == digest and item.get("total_chars") == total),
                      key=lambda item: item.get("offset", -1))
    covered = 0
    for item in matching:
        if item.get("offset") > covered:
            break
        covered = max(covered, item.get("end", 0))
    if covered >= total:
        target["status"] = "done"
        return True
    return False


def finalize(plan: list[dict], evidence: dict) -> dict:
    """只在其它步骤均有证据时完成最终交付步骤，并返回验收结果。"""
    final_steps = [step for step in plan if step.get("phase") == "final"]
    unfinished = [step for step in plan if step.get("phase") != "final"
                  and step.get("status") != "done"]
    if final_steps and not unfinished:
        complete_one(plan, "final", evidence)
    remaining = [
        {"title": step.get("title", ""), "phase": step.get("phase", "analyze"),
         "status": step.get("status", "pending")}
        for step in plan if step.get("status") != "done"
    ]
    return {"completed": not remaining, "incomplete_steps": remaining}
