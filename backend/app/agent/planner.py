"""任务规划器：把科研任务拆解成可执行、可勾选的待办清单（to-do list）。

在线 LLM 输出结构化计划；离线/解析失败时用确定性默认计划兜底，
保证任何模式下都有一份可展示、可推进的清单。
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

VALID_PHASES = ("search", "read", "analyze", "write", "audit", "final")


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


def _default_plan(question: str) -> list[dict]:
    """确定性默认计划：按问题关键词判断复杂度。"""
    q = question.lower()
    complex_kw = ("综述", "调研", "对比", "比较", "梳理", "研究现状", "写作",
                  "review", "survey", "compare", "summarize", "总结", "写", "draft")
    is_complex = any(k in q for k in complex_kw)
    if is_complex:
        return [
            {"title": "明确任务范围与关键词", "phase": "analyze"},
            {"title": "检索相关文献与原文证据", "phase": "search"},
            {"title": "阅读并去重、按主题归纳", "phase": "read"},
            {"title": "起草内容并逐句标注引用", "phase": "write"},
            {"title": "引用校验与语义审计", "phase": "audit"},
            {"title": "定稿并整理交付", "phase": "final"},
        ]
    return [
        {"title": "检索相关原文证据", "phase": "search"},
        {"title": "基于证据作答并标注引用", "phase": "write"},
        {"title": "引用校验后定稿", "phase": "audit"},
    ]


def make_plan(db: Session, llm, question: str) -> list[dict]:
    """返回待办步骤列表：[{"title","phase","status":"pending"}]。"""
    try:
        resp = llm.chat(
            [{"role": "system", "content": "你是科研任务规划器，只输出 JSON。"},
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
    except Exception:  # noqa: BLE001 — 规划失败不影响主流程，走默认计划
        pass
    return _normalize(_default_plan(question))


def _wrap(question: str) -> str:
    from .prompts import PLANNER_PROMPT

    return f"{PLANNER_PROMPT}\n用户任务：{question}"


def _normalize(steps: list[dict]) -> list[dict]:
    """补 status、保证以 final 收尾。"""
    out = [{"title": s["title"], "phase": s["phase"], "status": "pending"} for s in steps]
    if out and out[-1]["phase"] != "final":
        out.append({"title": "定稿并交付", "phase": "final", "status": "pending"})
    return out


def advance(plan: list[dict], phase: str) -> list[dict]:
    """进入某 phase：把该 phase 第一个未完成步骤标进行中，其之前的步骤标完成。

    返回更新后的 plan（原地修改并返回）。
    """
    target = next((i for i, s in enumerate(plan) if s["phase"] == phase
                   and s["status"] != "done"), None)
    if target is None:
        return plan
    for i, s in enumerate(plan):
        if i < target and s["status"] != "done":
            s["status"] = "done"
        elif i == target:
            s["status"] = "in_progress"
    return plan


def finish_all(plan: list[dict]) -> list[dict]:
    for s in plan:
        s["status"] = "done"
    return plan
