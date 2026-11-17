"""Durable, server-owned FIFO dispatch. Reading an inbox never executes work.

An input owns one deterministic child identity. The launch intent is committed
before admission; an uncertain process crash is shown for review, never retried
as a second model request. Browser observers own no part of this lifecycle.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import logging
import threading

from sqlalchemy import select, text
from fastapi import HTTPException

from .db import SessionLocal
from .models import Conversation, DeletedConversation, ForegroundRun, Paper, TaskRecord
from .runtime_models import RuntimeRun, RunAttempt
from .run_inputs import RunInput, RunInputQueue, _advance, _public, publish_input_change
from .timeutil import utcnow

logger = logging.getLogger(__name__)
_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None
_lifecycle_lock = threading.Lock()


def wake():
    _wake.set()


def child_run_id(input_id: str) -> str:
    return "chat_next_" + hashlib.sha256(input_id.encode()).hexdigest()[:48]


def _complete_reason(db, run_id: str) -> str:
    """Only a committed full success unlocks the next input, never a UI answer."""
    from . import approval_store, run_store
    from .goal_ledger import snapshot_goal
    if approval_store.list_for_run(run_id):
        return "前一轮等待审批，下一轮尚未启动"
    if db.scalar(select(RunInput.input_id).where(RunInput.run_id == run_id,
            RunInput.kind == "steer", RunInput.status == "queued")):
        return "前一轮仍有尚未生效的补充，下一轮已保留，请核对这些补充"
    if run_id in run_store.active_runs():
        return "等待前一轮完成并提交结果"
    foreground = db.get(ForegroundRun, run_id)
    task = None if foreground else db.get(TaskRecord, run_id)
    if foreground is None and task is None:
        return "前一轮执行记录不存在，请核对后重新发送"
    status = foreground.status if foreground else task.status
    if foreground and status == "running" and foreground.started_at:
        return "前一轮执行进程已中断，排队输入已保留，请核对执行记录"
    if status not in ("completed", "done"):
        return {"running": "等待前一轮完成并提交结果", "pending": "等待前一轮完成并提交结果",
                "paused": "前一轮已暂停，下一轮已保留", "cancelled": "前一轮已停止，下一轮已保留",
                "failed": "前一轮失败，下一轮已保留", "interrupted": "前一轮中断，下一轮已保留"}.get(
                    status, "前一轮尚未完整完成，下一轮已保留")
    logical = db.get(RuntimeRun, run_id)
    attempt = db.get(RunAttempt, logical.current_attempt_id) if logical and logical.current_attempt_id else None
    result = attempt.result if attempt else ((foreground.agent_checkpoint or {}).get("result")
                                             if foreground else task.result)
    if (logical and (logical.status != "ended" or logical.outcome != "completed")) or (
            not isinstance(result, dict) or result.get("completed") is not True):
        return "前一轮仅完成部分内容或结果待核实，下一轮已保留"
    if logical and logical.goal_id:
        goal = snapshot_goal(db, logical.goal_id)
        if goal.get("control_state") != "active" or goal.get("incomplete_requirements"):
            return "前一轮仍有未完成要求，下一轮已保留"
    return ""


def _binding_reason(db, row: RunInput) -> str:
    from . import workspace
    from .model_profiles import options_snapshot
    from .run_lifecycle import validate_binding
    conv = db.get(Conversation, row.conversation_id)
    if db.get(DeletedConversation, row.conversation_id) or conv is None:
        return "会话已删除，排队输入不会执行"
    if conv.archived_at:
        return "会话已归档，排队输入已保留"
    binding = row.runtime_config
    if not binding or row.dispatch_options is None:
        return "该排队输入缺少接收时的模型与范围快照，请核对后重新发送"
    try:
        from .source_runtime import validate_frozen_sources
        validate_frozen_sources(db, row.conversation_id, binding,
            expected_ids=row.dispatch_options.get("source_revision_ids", []))
        workspace.validate_scope(db, row.conversation_id, row.workspace)
        current_scope = workspace.capture(db, row.conversation_id,
            (row.workspace or {}).get("project_id"),
            [f["path"] for f in (row.workspace or {}).get("files", [])],
            workspace_revision=row.workspace_revision)
        if current_scope != row.workspace:
            return "引用文件或项目规范已变化，请核对后重新发送排队输入"
        options = options_snapshot(db, row.conversation_id)
        effective = options.get("effective_model") or {}
        if options.get("model_error"):
            return "排队模型当前不可用，请核对模型配置"
        if (options["model_profile_id"] != binding.get("profile_id") or
                options["revision"] != binding.get("policy_revision") or
                options["approval_mode"] != binding.get("approval_mode") or
                any(effective.get(current) != binding.get(saved) for current, saved in (
                    ("version", "profile_version"), ("base_url", "base_url"), ("model_id", "model_id"),
                    ("reasoning_effort", "reasoning_effort")))):
            return "模型或审批设置已变化，请核对后重新发送排队输入"
        validate_binding(db, binding)
        paper_id = row.dispatch_options.get("paper_id")
        if paper_id:
            paper = db.get(Paper, paper_id)
            if paper is None or paper.content_hash != row.dispatch_options.get("paper_content_hash"):
                return "所选文献已删除或替换，请核对后重新发送排队输入"
    except Exception:
        # Provider/credential exceptions can contain private transport details.
        return "排队模型凭据或引用范围已不可用，请核对后重新发送"
    return ""


def _change(db, row, *, reason: str, status: str | None = None):
    if row.reason == reason and (status is None or row.status == status):
        return None
    queue = db.get(RunInputQueue, row.run_id, populate_existing=True)
    revision = _advance(db, row.run_id, row.conversation_id, queue.revision if queue else 0)
    row.reason, row.revision, row.updated_at = reason, revision, utcnow()
    if status is not None:
        row.status = status
    db.flush()
    return {"run_id": row.run_id, "conversation_id": row.conversation_id,
            "revision": revision, "input": _public(row)}


def validate_child_admission(db, run_id: str) -> None:
    """Recheck the enqueue binding inside chat_stream's write transaction.

    Ordinary foreground runs have no linked inbox row. A dispatched child can
    only start while the exact received model, policy and source scope survive.
    """
    rows = db.scalars(select(RunInput).where(RunInput.child_run_id == run_id,
                                            RunInput.status == "dispatched")).all()
    if not rows:
        return
    if len(rows) != 1:
        raise HTTPException(409, "排队输入的执行关联不唯一，请核对后重新发送")
    reason = _binding_reason(db, rows[0])
    if reason:
        raise HTTPException(409, reason)


def _launch(req):
    """One admission creates one worker. The daemon drains only its observer."""
    from .api.chat import chat_stream
    response = chat_stream(req)

    def observe():
        async def drain():
            try:
                async for _ in response.body_iterator:
                    pass
            finally:
                close = getattr(response.body_iterator, "aclose", None)
                if close:
                    await close()
        try:
            asyncio.run(drain())
        except Exception:
            logger.warning("Next-turn observer ended; executor remains server-owned")
    threading.Thread(target=observe, name=f"{req.run_id}-observer", daemon=True).start()


def dispatch_once() -> int:
    """Dispatch at most the first pending input per conversation; safe on restart."""
    from . import run_store
    from .schemas import ChatRequest
    admitted = 0
    with SessionLocal() as scan:
        ids = scan.scalars(select(RunInput.input_id).where(
            RunInput.kind == "next_turn", RunInput.status.in_(("queued", "dispatched")))
            .order_by(RunInput.created_at, RunInput.revision, RunInput.input_id)).all()
    seen_conversations = set()
    for input_id in ids:
        event, req = None, None
        with run_store.coordinated(), SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = db.get(RunInput, input_id)
            if row is None or row.status not in ("queued", "dispatched"):
                continue
            if row.status == "dispatched":
                child = db.get(ForegroundRun, row.child_run_id) if row.child_run_id else None
                # Admission or an attempt proves the request reached the worker.
                # A previously committed intent without that proof is uncertain.
                if row.dispatch_started_at and (child is None or child.started_at is None):
                    event = _change(db, row, reason="启动结果待核实；为避免重复请求，未自动重试，请查看执行记录")
                db.commit()
                if event:
                    publish_input_change(event)
                continue
            if row.conversation_id in seen_conversations:
                continue
            seen_conversations.add(row.conversation_id)
            earlier = db.scalars(select(RunInput).where(
                RunInput.run_id == row.run_id, RunInput.kind == "next_turn",
                RunInput.status == "dispatched").order_by(RunInput.created_at, RunInput.revision)).all()
            previous = earlier[-1].child_run_id if earlier else row.run_id
            reason = _complete_reason(db, previous) if previous else "前一轮执行身份缺失，请核对"
            if not reason:
                from .run_lifecycle import assert_conversation_idle, RunConflict
                try:
                    assert_conversation_idle(db, row.conversation_id,
                                             exclude_run_id=child_run_id(row.input_id))
                except RunConflict:
                    reason = "该会话仍有任务执行，等待其完成后按顺序启动"
            reason = reason or _binding_reason(db, row)
            if reason:
                event = _change(db, row, reason=reason)
            else:
                child_id = child_run_id(row.input_id)
                child = db.get(ForegroundRun, child_id)
                if child is not None:
                    row.child_run_id = child_id
                    event = _change(db, row, status="dispatched", reason="已关联原执行记录，未重复启动")
                else:
                    options, scope = row.dispatch_options, row.workspace or {}
                    req = ChatRequest(run_id=child_id, question=row.content, conversation_id=row.conversation_id,
                        paper_id=options.get("paper_id"), skills=options.get("skills") or [],
                        workspace_revision=row.workspace_revision, project_id=scope.get("project_id"),
                        file_paths=[f["path"] for f in scope.get("files", [])],
                        source_revision_ids=options.get("source_revision_ids", []),
                        execution_mode=options.get("execution_mode", "execute"))
                    db.add(ForegroundRun(id=child_id, conversation_id=row.conversation_id, question=row.content,
                        paper_id=req.paper_id, skills=req.skills, images=[], workspace=deepcopy(row.workspace),
                        runtime_config=deepcopy(row.runtime_config), execution_mode=req.execution_mode))
                    row.child_run_id, row.dispatch_started_at = child_id, utcnow()
                    event = _change(db, row, status="dispatched", reason="已创建下一轮执行；正在启动")
            db.commit()
            if event:
                publish_input_change(event)
            if req is not None:
                try:
                    _launch(req)
                    admitted += 1
                except Exception as exc:
                    # Do not retry a request whose worker admission is uncertain.
                    with SessionLocal() as failed:
                        current = failed.get(RunInput, input_id)
                        child = failed.get(ForegroundRun, current.child_run_id)
                        not_admitted = child and child.started_at is None and failed.get(RuntimeRun, child.id) is None
                        if isinstance(exc, HTTPException) and not_admitted:
                            # A definite local admission rejection sent nothing
                            # to a provider. Keep the original input cancellable.
                            failed.delete(child)
                            current.child_run_id, current.dispatch_started_at = None, None
                            event = _change(failed, current, status="queued",
                                reason="下一轮尚未获准启动，原输入已保留；正在重新核对模型、权限与引用范围")
                        else:
                            event = _change(failed, current, reason="下一轮启动结果待核实，未自动重试；请查看执行记录")
                        if not isinstance(exc, HTTPException) and not_admitted:
                            child.status, child.error = "interrupted", "自动启动未获确认，请核对后明确恢复"
                        failed.commit()
                    if event:
                        publish_input_change(event)
    return admitted


def _worker():
    from .maintenance import activity, MaintenanceBusy
    while not _stop.is_set():
        _wake.clear()
        try:
            with activity():
                dispatch_once()
        except MaintenanceBusy:
            pass
        except Exception:
            logger.exception("Next-turn dispatcher pass failed; durable inbox retained")
        _wake.wait(1.0)


def start():
    global _thread
    with _lifecycle_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _wake.set()
        _thread = threading.Thread(target=_worker, name="next-turn-dispatcher", daemon=True)
        _thread.start()


def stop():
    global _thread
    with _lifecycle_lock:
        _stop.set()
        _wake.set()
        worker = _thread
    if worker is not None:
        worker.join(timeout=2)
    with _lifecycle_lock:
        if _thread is worker and (worker is None or not worker.is_alive()):
            _thread = None
