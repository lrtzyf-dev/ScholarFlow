"""Explicit user criteria in the existing Goal ledger, never model authority.

Natural language candidates remain unverified. This module lets a user state
bounded, inspectable acceptance checks and edit just one requirement. No new
task/goal system, model call or tool execution is created here.
"""
from copy import deepcopy
import hashlib
import json
import re

from sqlalchemy import select
from .goal_models import GoalRequirement, GoalRevision
from . import goal_ledger as ledger
from .timeutil import utcnow


FIELDS = {
    "manual": set(),
    "answer_terms": {"contains", "excludes"},
    "command_result": {"command", "path", "contains", "excludes"},
    "artifact_result": {"contains", "excludes", "min_fixed_sources"},
    "artifact_export_result": {"contains", "excludes", "min_fixed_sources", "format", "artifact_id", "revision_id", "requires_recorded_execution"},
    "policy": {"rule"},
}
RULES = {"no_file_read", "no_file_write", "no_external_tools", "no_commands"}


def _terms(value):
    if not isinstance(value, list) or len(value) > 30 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 500 or "\x00" in v for v in value):
        raise ledger.GoalError("验收词条须为最多30条非空文本，每条不超过500字符")
    if len(set(value)) != len(value):
        raise ledger.GoalError("验收词条不能重复")
    return value


def normalize(value):
    if not isinstance(value, dict) or value.get("kind") not in FIELDS:
        raise ledger.GoalError("请选择支持的验收类型")
    kind = value["kind"]
    if set(value) - ({"kind"} | FIELDS[kind]):
        raise ledger.GoalError("验收条件包含不支持的字段")
    result = {"kind": kind}
    if kind in ("answer_terms", "command_result", "artifact_result", "artifact_export_result"):
        result.update(contains=_terms(value.get("contains", [])), excludes=_terms(value.get("excludes", [])))
        if set(result["contains"]) & set(result["excludes"]):
            raise ledger.GoalError("同一词条不能同时要求出现和禁止出现")
    if kind == "answer_terms" and not (result["contains"] or result["excludes"]):
        raise ledger.GoalError("正文检查至少提供一个必含或禁含词条")
    if kind == "command_result":
        result["command"] = ledger._text(value.get("command"), "待验收的完整命令", limit=16000)
        from .workspace import relative_path
        from fastapi import HTTPException
        try:
            result["path"] = relative_path(value.get("path", ""))
        except HTTPException:
            raise ledger.GoalError("命令目录须为项目内相对路径") from None
    if kind in ("artifact_result", "artifact_export_result"):
        count = value.get("min_fixed_sources", 0)
        if type(count) is not int or not 0 <= count <= 500:
            raise ledger.GoalError("固定来源数量须为0到500的整数")
        result["min_fixed_sources"] = count
        if kind == "artifact_result" and not (result["contains"] or result["excludes"] or count):
            raise ledger.GoalError("成果检查至少提供词条或固定来源数量")
    if kind == "artifact_export_result":
        if value.get("format") not in ("docx", "xlsx", "svg", "bundle"):
            raise ledger.GoalError("固定导出须选择 DOCX、XLSX、SVG 或复算包")
        result["format"] = value["format"]
        for field in ("artifact_id", "revision_id"):
            if field in value:
                if not isinstance(value[field], str) or not re.fullmatch(r"[a-f0-9]{32}", value[field]):
                    raise ledger.GoalError("成果及版本编号无效")
                result[field] = value[field]
        if "revision_id" in result and "artifact_id" not in result:
            raise ledger.GoalError("指定版本时须同时指定所属成果")
        recompute = value.get("requires_recorded_execution", False)
        if type(recompute) is not bool or recompute and result["format"] == "docx":
            raise ledger.GoalError("已执行复算条件仅用于分析文件，且须为布尔值")
        result["requires_recorded_execution"] = recompute
    if kind == "policy":
        if value.get("rule") not in RULES:
            raise ledger.GoalError("不支持此操作限制")
        result["rule"] = value["rule"]
    return result


def user_change(db, goal_id, *, expected_revision, request_id, action, title=None,
                contract=None, requirement_id=None, candidate_id=None, reason=""):
    """CAS + idempotency; replacing a criterion preserves its original receipts."""
    if action not in ("add", "replace", "cancel", "confirm", "reopen", "blocked"):
        raise ledger.GoalError("要求操作无效")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", request_id):
        raise ledger.GoalError("要求操作编号无效")
    message_id = "user:criteria:" + request_id
    payload = {"action": action, "title": title, "contract": contract, "requirement_id": requirement_id,
               "candidate_id": candidate_id, "reason": reason}
    previous = db.scalar(select(GoalRevision).where(GoalRevision.goal_id == goal_id,
        GoalRevision.action == "user_criteria", GoalRevision.source_message_id == message_id))
    if previous:
        if previous.changes.get("request") != payload:
            raise ledger.GoalConflict("同一操作编号不能提交不同要求")
        return ledger.snapshot_goal(db, goal_id)
    goal = ledger._goal(db, goal_id)
    if type(expected_revision) is not int or goal.revision != expected_revision:
        raise ledger.GoalConflict("任务要求已在其他窗口更新，请刷新后核对")
    if goal.outcome == "cancelled":
        raise ledger.GoalError("已取消的目标不能改写")
    rows = ledger._requirements(db, goal_id)
    selected = next((r for r in rows if r.id == requirement_id), None)
    if action != "add" and (selected is None or selected.status in ("cancelled", "superseded")):
        raise ledger.GoalError("请选择本目标内仍有效的要求")
    if action in ("add", "replace"):
        title = ledger._text(title, "要求描述", limit=4000)
        spec = normalize(contract)
    elif contract is not None or title is not None or candidate_id is not None:
        raise ledger.GoalError("状态操作不能同时修改验收内容")
    if action in ("confirm", "blocked") and not isinstance(reason, str):
        raise ledger.GoalError("核对说明须为文本")
    if action in ("confirm", "blocked") and not reason.strip():
        raise ledger.GoalError("请填写核对依据或受阻原因")
    if selected and (selected.tool_arguments or {}).get("result_contract", {}).get("kind") == "policy" and action in ("confirm", "blocked"):
        raise ledger.GoalError("操作限制持续生效，只有明确取消或替换才能解除")
    source = None
    if candidate_id:
        source = next((c for c in ledger.snapshot_goal(db, goal_id)["input_coverage"]["candidates"]
                       if c["candidate_id"] == candidate_id and not c.get("quoted_context")), None)
        if source is None:
            raise ledger.GoalError("候选不属于本目标的直接用户原话")
    old = ({"id": selected.id, "title": selected.title, "status": selected.status,
            "contract": deepcopy(selected.tool_arguments), "evidence": deepcopy(selected.evidence)} if selected else None)
    revision = ledger._revision(db, goal, "user_criteria", "user", message_id,
        {"request": deepcopy(payload), "previous": old, "candidate": deepcopy(source)}, reason, expected_revision)
    if action in ("add", "replace"):
        if selected:
            selected.status, selected.required, selected.cancelled_revision = "superseded", 0, revision
            selected.updated_at = utcnow()
        identifier = hashlib.sha256(json.dumps([goal_id, request_id], separators=(",", ":")).encode()).hexdigest()
        db.add(GoalRequirement(goal_id=goal_id, identity="criteria:" + identifier, title=title,
            phase="audit", origin="user", source_message_id=message_id, source_revision=revision,
            required=0 if spec["kind"] == "policy" else 1, status="active_constraint" if spec["kind"] == "policy" else "open",
            acceptance="user_result_contract", tool_arguments={"result_contract": spec}, evidence=[]))
    else:
        selected.status = {"cancel": "cancelled", "confirm": "completed", "reopen": "open", "blocked": "blocked"}[action]
        selected.updated_at = utcnow()
        if action == "cancel": selected.cancelled_revision = revision
        if action == "confirm":
            selected.evidence = [*selected.evidence, {"kind": "user_confirmation", "source_message_id": message_id,
                "revision": revision, "reference": reason, "semantic_verification": "user_confirmed"}]
        elif action in ("blocked", "reopen"):
            selected.evidence = [*selected.evidence, {"kind": "user_" + action, "source_message_id": message_id,
                "revision": revision, "reference": reason}]
    # An explicit amendment may reopen a previously delivered/completed task;
    # a UI edit alone never starts execution or clears an explicit pause.
    if action in ("add", "replace", "reopen", "blocked"):
        goal.outcome, goal.completed_at = "open", None
    db.flush()
    return ledger.snapshot_goal(db, goal_id)


def policy_denial(snapshot, name):
    """Known capabilities only; unknown commands/remote tools fail closed."""
    from .agent.tools import SAFE_TOOLS
    clauses = [r for r in snapshot.get("requirements", [])
        if r.get("status") not in ("cancelled", "superseded")
        and (r.get("tool_arguments") or {}).get("result_contract", {}).get("kind") == "policy"]
    always = {"update_plan", "respond_in_plan_mode"}
    local = SAFE_TOOLS - {"retrieve_evidence", "compare_papers", "load_skill", "verify_attached_table"}
    no_process = always | {"list_papers", "retrieve_evidence", "compare_papers", "list_skills", "load_skill",
        "read_conversation_history", "list_project_files", "read_project_file", "read_project_command_output",
        "list_attached_sources", "read_attached_source", "list_research_artifacts", "list_research_artifact_versions", "read_research_artifact",
        "list_evidence_matrices", "read_evidence_matrix"}
    for row in clauses:
        rule = row["tool_arguments"]["result_contract"]["rule"]
        allowed = (name in always or
            rule == "no_commands" and name in no_process or
            rule == "no_file_read" and name in always or
            rule == "no_file_write" and name in SAFE_TOOLS or
            rule == "no_external_tools" and name in local)
        if not allowed:
            return {"blocked_by_goal_constraint": True, "goal_id": snapshot["id"], "goal_revision": snapshot["revision"],
                "requirement_id": row["id"], "source_message_id": row["source_message_id"],
                "error": "用户设置的操作限制仍生效：" + row["title"] + "。此工具尚未执行；需由用户明确修改或解除。"}
    return None
