"""Background execution with durable checkpoints and explicit resume/restart."""
from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from .models import DeletedConversation, TaskRecord
from .agent.loop import run_agent
from .agent.checkpoint import AgentState, CheckpointNeedsReview, recovery_info
from . import approval_store, cancel_store, notifier, run_store
from .conversation_store import check_conversation_available, ensure_conversation
from .task_events import notify_tasks_changed
from .execution_mode import claim_reviewed_plan, reviewed_plan_text

_running: dict[str, threading.Thread] = {}
_lock = threading.Lock()
_shutting_down = threading.Event()


class TaskConflict(ValueError):
    def __init__(self, message: str, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def _now():
    return datetime.utcnow()


def running_task_ids():
    with _lock:
        return list(_running)


def _event(task, kind, text):
    task.events = [*(task.events or []), {"kind": kind, "text": text}][-40:]
    task.updated_at = _now()


def _run_in_background(task_id, question, paper_id, skills, conversation_id):
    from .db import SessionLocal

    db = SessionLocal()
    source = None
    answer, failure = "", ""
    was_cancelled = False
    try:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        checkpoint = deepcopy(task.checkpoint)
        events_log = list(task.events or [])
        task.status = "running"
        task.updated_at = _now()
        db.commit()
        notify_tasks_changed()

        def persist(snapshot):
            current = db.get(TaskRecord, task_id)
            if current is None:
                raise RuntimeError("任务记录已不存在，停止继续执行")
            current.checkpoint = snapshot
            if snapshot.get("todo"):
                current.plan = deepcopy(snapshot["todo"])
                done = sum(step.get("status") == "done" for step in current.plan)
                current.progress = round(done / len(current.plan), 3)
            if snapshot.get("result"):
                current.result = deepcopy(snapshot["result"])
            current.updated_at = _now()
            db.commit()  # Final QA and delivery checkpoint share this transaction.
            notify_tasks_changed()

        source = run_agent(db, question, paper_id, selected_skills=skills,
                           conversation_id=conversation_id, run_id=task_id,
                           checkpoint=checkpoint, save_checkpoint=persist,
                           execution_mode=(task.payload or {}).get("execution_mode", "execute"),
                           workspace=(task.payload or {}).get("workspace"),
                           reviewed_plan_id=(task.payload or {}).get("reviewed_plan_id"))
        for kind, data in source:
            if kind == "cancelled":
                was_cancelled = True
            elif kind in ("plan", "todo_update"):
                task.plan = deepcopy(data.get("steps"))
                total = len(task.plan or [])
                task.progress = round(sum(s.get("status") == "done" for s in task.plan or []) / total, 3) if total else 0
            elif kind == "answer":
                answer = data.get("answer", "")
                task.result = deepcopy(data)
            elif kind == "error":
                failure = data.get("text") or "任务执行失败"
            elif kind == "approval_request":
                notifier.notify("研序：需要你的批准",
                    f"任务「{task.title}」请求执行「{data.get('tool_name', '')}」，请回到应用查看。", level="guarded")
            if kind in ("cancelled", "error", "approval_request", "approval_result", "project_change", "project_command", "git_operation"):
                events_log.append({"kind": kind, **data})
            elif kind in ("status", "tool_call", "tool_result", "subagents"):
                events_log.append({"kind": kind, "text": data.get("text", ""), "name": data.get("name", "")})
            elif kind not in ("plan", "todo_update", "answer"):
                continue
            events_log = events_log[-40:]
            task.events = list(events_log)
            task.updated_at = _now()
            db.commit()
            notify_tasks_changed()
            if was_cancelled:
                break

        task.updated_at = _now()
        if was_cancelled:
            task.status = "interrupted" if _shutting_down.is_set() else "cancelled"
            task.error = "应用退出，已保留执行检查点" if _shutting_down.is_set() else ""
        elif failure:
            task.status, task.error = "failed", failure
        elif answer or (task.checkpoint or {}).get("phase") == "completed":
            task.status, task.progress, task.error = "done", 1.0, ""
        else:
            task.status, task.error = "failed", "执行未返回完整结果，已保留可用的检查点"
        db.commit()
        notify_tasks_changed()
        if not _shutting_down.is_set():
            label = {"done": "已完成", "failed": "失败", "cancelled": "已取消"}.get(task.status, "已中断")
            notifier.notify(f"研序：后台任务{label}", f"「{task.title}」{label}，可在任务列表查看。",
                            level="error" if failure else "info")
    except Exception as exc:
        db.rollback()
        task = db.get(TaskRecord, task_id)
        if task:
            task.status = "interrupted" if isinstance(exc, CheckpointNeedsReview) else "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.updated_at = _now()
            db.commit()
            notify_tasks_changed()
            if not _shutting_down.is_set():
                notifier.notify("研序：后台任务失败", f"「{task.title}」：{task.error[:120]}", level="error")
    finally:
        try:
            if source is not None and hasattr(source, "close"):
                source.close()
        finally:
            try:
                db.close()
            finally:
                approval_store.discard_for_run(task_id)
                cancel_store.clear(task_id)
                with _lock:
                    _running.pop(task_id, None)
                notify_tasks_changed()


def _launch(db, task):
    payload = task.payload or {}
    cancel_store.clear(task.id)
    thread = threading.Thread(target=_run_in_background,
        args=(task.id, payload.get("question", task.title), payload.get("paper_id"),
              payload.get("skills") or [], task.conversation_id), daemon=True, name=f"task_{task.id}")
    with _lock:
        _running[task.id] = thread
    try:
        thread.start()
    except Exception as exc:
        with _lock:
            _running.pop(task.id, None)
        task.status, task.error = "failed", f"任务线程无法启动：{exc}"
        db.commit()
        notify_tasks_changed()
        raise TaskConflict(task.error, 503) from exc
    return task_to_dict(task)


def start_agent_task(db: Session, question, paper_id, skills, conversation_id, title="", *,
                     execution_mode="execute", reviewed_plan_id=None, retry_plan_run_id=None,
                     project_id=None, file_paths=None, workspace_revision=0):
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        if _shutting_down.is_set():
            raise TaskConflict("应用正在退出，暂不能开始新任务", 503)
        check_conversation_available(db, conversation_id)
        from .workspace import capture
        scope = capture(db, conversation_id, project_id, file_paths, reviewed_plan_id, workspace_revision)
        task = TaskRecord(type="agent", title=title or question[:60], status="pending", progress=0,
            paper_id=paper_id, conversation_id=conversation_id or f"c_{uuid4().hex[:24]}",
            payload={"question": question, "paper_id": paper_id, "skills": skills or [],
                     "execution_mode": execution_mode, "reviewed_plan_id": reviewed_plan_id, "workspace": scope},
            events=[], plan=None, result=None, checkpoint=None)
        db.add(task)
        db.flush()
        claim_reviewed_plan(db, reviewed_plan_id, task.conversation_id, task.id, retry_plan_run_id)
        ensure_conversation(db, task.conversation_id, title or question)
        db.commit()
        db.refresh(task)
        notify_tasks_changed()
        return _launch(db, task)


def resume_task(db: Session, task_id: str, resolution: str | None = None):
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        if _shutting_down.is_set():
            raise TaskConflict("应用正在退出，暂不能恢复任务", 503)
        task = db.get(TaskRecord, task_id)
        if not task:
            return {"error": "任务不存在"}
        check_conversation_available(db, task.conversation_id)
        if task.id in running_task_ids():
            return task_to_dict(task)
        info = recovery_info(task.checkpoint)
        if task.status == "done" or not info["available"]:
            raise TaskConflict(info.get("reason") or "此任务已完成，请选择从头重新运行")
        state = AgentState.model_validate(task.checkpoint)
        payload = task.payload or {}
        from .workspace import validate_scope
        validate_scope(db, task.conversation_id, state.workspace)
        if state.workspace != payload.get("workspace"):
            raise TaskConflict("检查点与任务的项目文件快照不匹配")
        if (state.execution_mode, state.reviewed_plan_id) != (
                payload.get("execution_mode", "execute"), payload.get("reviewed_plan_id")):
            raise TaskConflict("检查点的执行模式或计划来源不匹配")
        if state.reviewed_plan_id:
            reviewed_plan_text(db, state.reviewed_plan_id, task.conversation_id, task.id)
        if state.pending_operation and resolution not in ("retry", "skip"):
            raise TaskConflict("上一步执行结果未确认，请检查后选择重试或跳过")
        if resolution and not state.pending_operation:
            raise TaskConflict("该步骤已不需要处理，请刷新任务状态")
        state.resolution = resolution
        task.checkpoint = state.snapshot()
        task.status, task.error = "pending", ""
        event_text = {"retry": "用户选择重试中断的步骤", "skip": "用户选择跳过中断的步骤"}.get(resolution, "从已保存检查点继续")
        _event(task, "resume", event_text)
        db.commit()
        notify_tasks_changed()
        return _launch(db, task)


def restart_task(db: Session, task_id: str):
    with run_store.coordinated():
        task = db.get(TaskRecord, task_id)
        if not task:
            return {"error": "任务不存在"}
        if task.id in running_task_ids():
            raise TaskConflict("请先停止当前任务，再从头重新运行")
        payload = task.payload or {}
        return start_agent_task(db, payload.get("question", task.title), payload.get("paper_id"),
                                payload.get("skills") or [], task.conversation_id, task.title,
                                execution_mode=payload.get("execution_mode", "execute"),
                                project_id=(payload.get("workspace") or {}).get("project_id"),
                                workspace_revision=(payload.get("workspace") or {}).get("workspace_revision", 0),
                                file_paths=[f["path"] for f in (payload.get("workspace") or {}).get("files", [])],
                                reviewed_plan_id=payload.get("reviewed_plan_id"),
                                retry_plan_run_id=task.id if payload.get("reviewed_plan_id") else None)


def cancel_task(db: Session, task_id: str):
    with run_store.coordinated():
        return _cancel_task(db, task_id)


def _cancel_task(db: Session, task_id: str):
    task = db.get(TaskRecord, task_id)
    if not task:
        return {"error": "任务不存在"}
    if task.status in ("done", "failed", "cancelled", "interrupted") and task.id not in running_task_ids():
        return task_to_dict(task)
    if task.id in running_task_ids():
        cancel_store.request_cancel(task_id)
        approval_store.resolve_for_run(task_id, "deny")
    else:
        task.status = "cancelled"
        task.updated_at = _now()
        db.commit()
        notify_tasks_changed()
    return task_to_dict(task)


def recover_interrupted_tasks(db: Session):
    """At startup, show orphaned tasks without replaying external operations."""
    with run_store.coordinated():
        _shutting_down.clear()
        active = running_task_ids()
        tasks = db.query(TaskRecord).filter(TaskRecord.type == "agent",
            TaskRecord.status.in_(("pending", "running"))).all()
        for task in tasks:
            if task.id in active:
                continue
            info = recovery_info(task.checkpoint)
            if info.get("phase") == "completed":
                task.status, task.progress = "done", 1.0
                task.result = (task.checkpoint or {}).get("result") or task.result
                task.error = ""
            else:
                task.status = "interrupted"
                task.error = info.get("reason") or "应用上次退出时任务未完成，可从检查点继续"
                _event(task, "interrupted", task.error)
        if tasks:
            db.commit()
            notify_tasks_changed()


def request_shutdown():
    with run_store.coordinated():
        _shutting_down.set()
        for task_id in running_task_ids():
            cancel_store.request_cancel(task_id)
            approval_store.resolve_for_run(task_id)


def shutdown_background_tasks(timeout=5.0):
    from .db import SessionLocal
    request_shutdown()
    deadline = time.monotonic() + timeout
    with _lock:
        threads = list(_running.items())
    for _, thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    remaining = running_task_ids()
    if remaining:
        with SessionLocal() as db:
            for task in db.query(TaskRecord).filter(TaskRecord.id.in_(remaining)).all():
                task.status = "interrupted"
                task.error = "退出时当前操作未结束，请在下次启动后检查该步骤的执行结果"
                _event(task, "interrupted", task.error)
            db.commit()
        notify_tasks_changed()


def task_to_dict(task: TaskRecord):
    return {"id": task.id, "type": task.type, "title": task.title, "status": task.status,
        "execution_mode": (task.payload or {}).get("execution_mode", "execute"),
        "reviewed_plan_id": (task.payload or {}).get("reviewed_plan_id"),
        "executing": task.id in running_task_ids(),
        "progress": task.progress, "plan": task.plan, "events": task.events,
        "approvals": approval_store.list_for_run(task.id), "result": task.result, "error": task.error,
        "recovery": recovery_info(task.checkpoint), "conversation_id": task.conversation_id,
        "paper_id": task.paper_id, "created_at": task.created_at.isoformat() if task.created_at else "",
        "updated_at": task.updated_at.isoformat() if task.updated_at else ""}


def _task_query(db: Session, type_: str = "agent"):
    q = db.query(TaskRecord)
    if type_:
        q = q.filter(TaskRecord.type == type_)
    active = or_(TaskRecord.status.in_(("pending", "running")), TaskRecord.id.in_(running_task_ids()))
    return q, active


def task_history(db: Session, limit: int = 30, before: tuple[datetime, str] | None = None):
    import base64
    import json

    q, active = _task_query(db)
    timestamp = func.coalesce(TaskRecord.updated_at, TaskRecord.created_at, datetime(1970, 1, 1))
    q = q.filter(~active)
    total = q.count()
    if before:
        at, task_id = before
        q = q.filter(or_(timestamp < at, (timestamp == at) & (TaskRecord.id < task_id)))
    rows = q.order_by(timestamp.desc(), TaskRecord.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if has_more:
        last = rows[-1]
        value = [(last.updated_at or last.created_at or datetime(1970, 1, 1)).isoformat(), last.id]
        cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    return {"tasks": [task_to_dict(row) for row in rows], "next_cursor": cursor, "history_total": total}


def task_snapshot(db: Session, conversation_mode="full"):
    from . import task_events
    from .conversation_activity import conversation_summaries

    q, active = _task_query(db)
    active_rows = q.filter(active).order_by(TaskRecord.created_at.desc(), TaskRecord.id.desc()).all()
    history = task_history(db)
    # A task can finish between the active and history queries.
    combined = {row.id: task_to_dict(row) for row in active_rows}
    combined.update({task["id"]: task for task in history["tasks"]})
    deleted_count, deleted_at = db.query(func.count(DeletedConversation.id), func.max(DeletedConversation.deleted_at)).one()
    epoch = f"{deleted_count}:{deleted_at.isoformat() if deleted_at else ''}"
    result = {**history, "tasks": list(combined.values()), "history_epoch": epoch,
              "conversation_epoch": task_events.conversation_epoch()}
    if conversation_mode == "full":
        result["conversations"] = conversation_summaries(db, scope="all")
    return result


def list_tasks(db: Session, type_: str = "agent"):
    if type_ == "agent":
        return task_snapshot(db)["tasks"]
    q, active = _task_query(db, type_)
    rows = q.filter(active).order_by(TaskRecord.created_at.desc()).all()
    rows += q.filter(~active).order_by(TaskRecord.created_at.desc()).limit(30).all()
    return [task_to_dict(row) for row in rows]


def get_task(db: Session, task_id: str):
    task = db.get(TaskRecord, task_id)
    return task_to_dict(task) if task else None
