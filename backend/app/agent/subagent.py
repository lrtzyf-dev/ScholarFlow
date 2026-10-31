"""子 agent（Subagents）：主 agent 可并行派发多个专题子任务，各自独立检索、归纳。

子 agent 只使用只读检索工具（safe），不做写入/外联抓取，因此无需审批；
多个子任务用线程池并行执行，主 agent 汇总结果。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from ..config import settings
from ..llm import BaseLLM
from .prompts import SUBAGENT_PROMPT, SUBAGENT_SUMMARY_PROMPT
from .tools import execute_tool, get_tool_specs, tool_risk


def _safe_tool_specs() -> list[dict]:
    return [t for t in get_tool_specs() if tool_risk(t["name"]) == "safe"]


def run_subagent(db: Session, llm: BaseLLM, task: dict,
                 max_rounds: int = 2) -> dict:
    """运行单个子任务，返回 {"topic","findings","evidence"}。"""
    topic = str(task.get("topic", "")).strip()
    goal = str(task.get("goal", topic)).strip()
    if not topic:
        return {"topic": topic, "findings": "（子任务为空，已跳过）", "evidence": []}

    specs = _safe_tool_specs()
    allowed_tools = {spec['name'] for spec in specs}
    messages: list[dict] = [
        {"role": "system", "content": SUBAGENT_PROMPT},
        {"role": "user", "content": f"子任务：{topic}\n目标：{goal}"},
    ]
    collected: list[dict] = []

    # 阶段 1：自主检索（只给只读工具，最多 max_rounds 轮）
    for _ in range(max_rounds):
        try:
            resp = llm.chat(messages, tools=specs, temperature=0.2)
        except Exception as exc:  # noqa: BLE001
            return {"topic": topic, "findings": f"子 agent 调用失败：{type(exc).__name__}: {exc}",
                    "evidence": []}
        if not resp.tool_calls:
            break
        messages.append({"role": "assistant", "content": resp.content,
                         "tool_calls": resp.tool_calls})
        for call in resp.tool_calls:
            try:
                if call['name'] not in allowed_tools:
                    result = json.dumps({'error': '子任务仅允许当前已开放的只读工具，已阻止此调用。',
                        'blocked_by_subagent_policy': True}, ensure_ascii=False)
                else:
                    result = execute_tool(db, call["name"], call.get("arguments", {}))
            except Exception as exc:  # noqa: BLE001
                result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            collected.append({"tool": call["name"], "result": result})

    # 阶段 2：基于证据归纳该子主题
    evidence_text = "\n".join(
        f"[{i+1}] {c['result'][:1500]}" for i, c in enumerate(collected)
    ) or "（未检索到证据）"
    try:
        summary = llm.chat(
            [{"role": "system", "content": SUBAGENT_SUMMARY_PROMPT.format(topic=topic)},
             {"role": "user", "content": f"证据：\n{evidence_text}"}],
            tools=None, temperature=0.2,
        )
        findings = summary.content.strip()
    except Exception as exc:  # noqa: BLE001
        findings = f"归纳失败：{type(exc).__name__}: {exc}"

    return {"topic": topic, "findings": findings, "evidence": collected}


def run_subagents(db: Session, llm: BaseLLM, tasks: list[dict],
                  max_workers: int = 4) -> list[dict]:
    """并行运行多个子任务，返回结果列表（顺序与 tasks 一致）。"""
    if not tasks:
        return []
    # 每个子任务用独立 db 会话（线程内不可共享 Session）
    from ..db import SessionLocal

    def _work(task: dict) -> dict:
        sub_db = SessionLocal()
        try:
            return run_subagent(sub_db, llm, task)
        finally:
            sub_db.close()

    workers = min(max_workers, len(tasks))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_work, tasks))
