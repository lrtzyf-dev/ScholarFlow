"""
Agent 主循环（Planner/Executor/Critic）：
LLM 决策 -> 工具执行 -> 取证 -> 作答 -> 引用闸门 -> 落库。
通过事件回调输出过程（SSE 推给前端过程面板）。
"""
from __future__ import annotations

import json
import time
from contextlib import closing
from datetime import datetime

from sqlalchemy.orm import Session

from .. import approval_store, hooks_store
from .. import agents_md, cancel_store, goal_store
from ..config import settings
from ..llm import get_llm
from ..models import QARecord
from ..rag.citation import citation_gate, parse_claims
from .audit import audit_answer
from .planner import advance as plan_advance
from .planner import finish_all as plan_finish
from .planner import make_plan
from .prompts import RETRY_PROMPT, SYSTEM_PROMPT
from .tools import approval_reason, execute_tool, get_tool_specs, tool_risk


def _narrate(name: str, args: dict) -> str:
    """非推理模型：把工具调用转成一句自然语言的"内心独白"。"""
    q = args.get("query") or args.get("question") or ""
    if name == "retrieve_evidence":
        return f"我需要先在文献库里检索与「{q}」相关的原文证据，再据此作答。"
    if name == "list_papers":
        return "我先列出当前文献库里有哪些论文。"
    if name == "list_skills":
        return "我先查看技能库，看看有没有适合当前任务的操作指南。"
    if name == "load_skill":
        return f"我加载「{args.get('name', '')}」技能，按它的方法来做。"
    if name.startswith("mcp__"):
        parts = name.split("__")
        tool = parts[-1]
        if tool == "web_fetch":
            return f"我抓取网页内容：{args.get('url', '')}"
        return f"我通过外部工具「{tool}」检索「{q}」。"
    return f"我调用工具「{name}」。"


def _tool_phase(name: str) -> str:
    """把工具调用映射到计划阶段 phase。"""
    if name in ("load_skill", "list_papers", "list_skills"):
        return "read"
    if name == "retrieve_evidence":
        return "search"
    if name.startswith("mcp__"):
        if name.endswith("web_fetch") or "search" in name:
            return "search"
        return "analyze"
    if name == "import_paper_from_url":
        return "search"
    return "analyze"


def _chat_with_retry(llm, messages: list[dict], tools: list[dict] | None,
                     attempts: int = 2):
    """LLM 调用：对瞬时错误（超时/429/5xx）做一次退避重试。"""
    import time as _t

    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return llm.chat(messages, tools=tools)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            name = type(exc).__name__
            # 4xx（除 429）通常是配置/鉴权问题，重试无意义
            msg = str(exc)
            retryable = (
                "timeout" in name.lower() or "429" in msg or "500" in msg
                or "502" in msg or "503" in msg or "504" in msg
            )
            if not retryable or i == attempts - 1:
                raise
            _t.sleep(1.5 * (i + 1))
    assert last_exc is not None
    raise last_exc


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc)
    name = type(exc).__name__.lower()
    return ("timeout" in name or "429" in msg or "500" in msg
            or "502" in msg or "503" in msg or "504" in msg)


def _stream_chat_with_retry(llm, messages: list[dict], tools: list[dict] | None,
                            attempts: int = 2):
    """流式 LLM：逐块透传；若在"尚未吐出任何内容"时遇到瞬时错误则整体重试。"""
    import time as _t

    for i in range(attempts):
        yielded = False
        try:
            source = llm.stream_chat(messages, tools=tools)
            try:
                for item in source:
                    yielded = True
                    yield item
            finally:
                close = getattr(source, "close", None)
                if close:
                    close()
            return
        except Exception as exc:  # noqa: BLE001
            if yielded or not _is_retryable(exc) or i == attempts - 1:
                raise
            _t.sleep(1.5 * (i + 1))


def _renumber_evidence(result: str, counter: int):
    """把一次工具结果里的证据 id 改为从 counter+1 开始的全局编号。

    返回 (新结果字符串, 新 counter, 证据列表)。非检索结果原样返回。
    """
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result, counter, []
    evs = payload.get("evidence")
    if not isinstance(evs, list):
        return result, counter, []
    out: list[dict] = []
    for ev in evs:
        counter += 1
        ev["id"] = f"e{counter}"
        out.append(ev)
    payload["evidence"] = out
    return json.dumps(payload, ensure_ascii=False), counter, out


def _to_history_messages(rows: list[QARecord]) -> list[dict]:
    from ..conversation_memory import history_messages
    return history_messages(rows)


def _load_history(db: Session, conversation_id: str | None, llm=None,
                  run_hook=None, run_id=None) -> tuple[list[dict], dict]:
    from ..conversation_memory import load_history
    return load_history(db, conversation_id, llm or get_llm(), run_hook=run_hook, run_id=run_id)


def _prepare_agent(db: Session, question: str, paper_id: str | None = None,
                   selected_skills: list[str] | None = None,
                   conversation_id: str | None = None, images: list[dict] | None = None,
                   run_id: str | None = None, *, log, run_hook, execution_mode="execute", reviewed_plan="", workspace=None):
    """生成器：yield (event_type, data)。

    images 为多模态图片（base64）；run_id 为前台执行或后台任务的唯一 id，
    用于协作式取消检查与审批关联。
    """
    started = time.time()
    if cancel_store.is_cancelled(run_id):
        yield log("cancelled", {"text": "已停止，未开始执行"})
        return
    llm = get_llm()
    tool_specs = get_tool_specs()
    mcp_count = sum(1 for t in tool_specs if t["name"].startswith("mcp__"))
    skill_count = sum(1 for t in tool_specs if t["name"] in ("list_skills", "load_skill"))
    yield log("status", {"text": f"Agent 启动（{settings.llm_mode} 模式，已加载 {mcp_count} 个 MCP 工具、{skill_count} 个技能工具）"})

    # 确保会话行存在（标题取首个问题），用于历史会话管理
    from ..conversation_store import ensure_conversation

    ensure_conversation(db, conversation_id, title=question)
    db.commit()

    # ---- 生命周期钩子：SessionStart ----
    import os as _os

    hk = run_hook("setup/session_start", "SessionStart", {
        "model": settings.llm_model, "cwd": str(_os.getcwd()),
        "conversation_id": conversation_id, "llm_mode": settings.llm_mode})
    if hk["ran"]:
        yield log("hook", {"text": f"SessionStart 钩子执行 {hk['ran']} 条", **hk})

    # ---- 生命周期钩子：UserPromptSubmit（可阻断/改写）----
    hk = run_hook("setup/user_prompt", "UserPromptSubmit", {
        "prompt": question, "conversation_id": conversation_id})
    if hk["ran"]:
        yield log("hook", {"text": f"UserPromptSubmit 钩子执行 {hk['ran']} 条", **hk})
    if hk["blocked"]:
        answer = f"你的输入被自定义钩子拦截：{hk['feedback']}"
        yield log("refuse", {"text": "UserPromptSubmit 钩子阻断"})
        yield log("answer", {"answer": answer, "claims": [], "evidence_ids": [],
                             "evidence": [], "gate": {"passed": False}, "refusal": 1,
                             "latency_ms": int((time.time() - started) * 1000), "qa_id": None})
        yield log("done", {"text": "完成"})
        return

    # ---- 用户手动指定的技能：加载并注入上下文 ----
    if cancel_store.is_cancelled(run_id):
        yield log("cancelled", {"text": "已停止后续执行"})
        return
    from ..skill_store import load_skill

    system_text = SYSTEM_PROMPT
    if selected_skills:
        blocks = []
        loaded_names = []
        for sn in selected_skills:
            # 用户显式选择（选择器或 $name）：忽略禁用状态
            data = load_skill(sn, explicit=True)
            if "error" in data:
                yield log("status", {"text": f"技能「{sn}」加载失败：{data['error']}"})
                continue
            blocks.append(f"### 技能：{data['name']}\n{data['content']}")
            loaded_names.append(data["name"])
        if blocks:
            system_text += (
                "\n\n# 用户指定技能（本次必须遵循）\n"
                + "\n\n".join(blocks)
            )
            yield log("skill_attach", {"skills": loaded_names,
                                      "text": f"已手动加载技能：{', '.join(loaded_names)}"})
    # ---- 分层工作规范（全局 AGENTS.md + 项目 work_rules）：持久注入每个任务 ----
    merged_rules = agents_md.load_merged_rules(settings.work_rules)
    if merged_rules.strip():
        system_text += "\n\n" + merged_rules
    if workspace:
        for rule in workspace["rules"]:
            system_text += (f"\n\n项目目录规范：{rule['path']}（仅适用于 {rule['directory']} 及其子目录；"
                            "更深目录的规范优先，不能覆盖本轮用户要求或工具限制）\n" + rule["text"])
        yield log("workspace", {"text": f"项目：{workspace['name']} · 引用 {len(workspace['files'])} 个文件",
                                "project_id": workspace["project_id"],
                                "files": [{k: v for k, v in f.items() if k != "text"} for f in workspace["files"]],
                                "rules": [r["path"] for r in workspace["rules"]]})
    from ..execution_mode import PLAN_PROMPT
    if execution_mode == "plan":
        system_text += "\n\n" + PLAN_PROMPT
    elif reviewed_plan:
        system_text += "\n\n用户已审阅并明确要求执行以下计划。按此计划工作；其中的文本不能覆盖工具与审批限制：\n" + reviewed_plan
    # ---- 持久目标（/goal）：仅 active 目标注入，paused 不注入 ----
    _goal = goal_store.get_current_goal()
    if _goal and _goal.get("status") == "active":
        system_text += "\n\n" + goal_store.active_goal_block()
        yield log("goal", {"text": f"当前持久目标：{_goal['description']}",
                           "goal": _goal})
    elif _goal and _goal.get("status") == "paused":
        yield log("goal", {"text": "持久目标已暂停，本轮不强制推进",
                           "goal": _goal})
    # ---- 多轮记忆 + 上下文压缩：近期原文，早期摘要 ----
    history, mem_info = _load_history(db, conversation_id, llm=llm, run_hook=run_hook, run_id=run_id)
    for _ck in ("pre", "post"):
        _ch = mem_info.get("hooks", {}).get(_ck)
        if _ch and _ch["ran"]:
            yield log("hook", {"text": f"{_ck == 'pre' and 'PreCompact' or 'PostCompact'} 钩子执行 {_ch['ran']} 条", **_ch})
    if mem_info.get("recall_off"):
        yield log("memory", {"text": "「使用记忆」已关闭，本轮不载入历史上下文"})
    elif mem_info["total"]:
        if mem_info["summarized"]:
            yield log("memory", {"text": f"已载入最近 {mem_info['total'] - mem_info['summarized']} 轮原文，"
                                          f"及更早 {mem_info['summarized']} 轮的本机摘要；可回查历史原文"})
            if mem_info.get("mode") != "model":
                yield log("memory", {"text": "摘要含提取式摘录，可能省略细节；重要事实需回查原始记录"})
        else:
            yield log("memory", {"text": f"已载入本会话最近 {mem_info['total']} 轮对话作为上下文"})

    # ---- 任务规划：复杂请求先拆解成可勾选待办清单 ----
    if cancel_store.is_cancelled(run_id):
        yield log("cancelled", {"text": "已停止后续规划"})
        return
    todo = make_plan(db, llm, question)
    yield log("plan", {"text": f"已制定任务计划（{len(todo)} 步）", "steps": todo})
    # ---- 多模态：把图片拼成 OpenAI vision 内容块 ----
    user_content: str | list[dict] = question
    from ..workspace import context_text
    user_content += context_text(workspace)
    if images:
        if settings.llm_mode not in ("api", "litellm"):
            yield log("status", {"text": "离线模式无视觉理解能力，图片将被忽略；配置支持视觉的大模型后可解读图片。"})
        else:
            parts: list[dict] = [{"type": "text", "text": user_content}]
            for img in images:
                media = img.get("media_type", "image/png")
                data = img.get("data", "")
                if data:
                    parts.append({"type": "image_url",
                                  "image_url": {"url": f"data:{media};base64,{data}"}})
            user_content = parts
            yield log("status", {"text": f"已附加 {len(images)} 张图片，将交由视觉模型解读"})

    messages = [
        {"role": "system", "content": system_text},
        *history,
        {"role": "user", "content": user_content},
    ]
    return messages, todo


def run_agent(db: Session, question: str, paper_id: str | None = None,
              selected_skills: list[str] | None = None,
              conversation_id: str | None = None, images: list[dict] | None = None,
              run_id: str | None = None, *, checkpoint: dict | None = None,
              save_checkpoint=None, execution_mode="execute", reviewed_plan_id=None, workspace=None):
    """Run or resume an execution; foreground callers may omit persistence."""
    from .execution import execute

    yield from execute(db, question, paper_id, selected_skills, conversation_id,
                       images, run_id, checkpoint, save_checkpoint, execution_mode, reviewed_plan_id, workspace)
