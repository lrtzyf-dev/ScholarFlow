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
from .agent.checkpoint import AgentState, CheckpointNeedsReview, recovery_info, prepare_resume
from . import run_lifecycle
from . import approval_store, cancel_store, notifier, run_store
from .conversation_store import check_conversation_available, ensure_conversation
from .task_events import notify_tasks_changed
from .execution_mode import claim_reviewed_plan, reviewed_plan_text
from .agent.paper_scope import describe as describe_paper_scope
from .timeutil import utcnow
from .model_profiles import capture_run_config, ModelConfigurationError
from .runtime_context import public_runtime_config
from .source_store import SourceError

_running: dict[str, threading.Thread] = {}
_lock = threading.Lock()
_shutting_down = threading.Event()
DEFAULT_RESUME_STEPS = 6
MAX_RESUME_STEPS = 24


class TaskConflict(ValueError):
    def __init__(self, message: str, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def _now():
    return utcnow()


def running_task_ids():
    with _lock:
        return list(_running)


def _event(task, kind, text):
    task.events = [*(task.events or []), {"kind": kind, "text": text}][-40:]
    task.updated_at = _now()


def _task_recovery_info(snapshot):
    """Keep legacy completed budget summaries visible as resumable tasks."""
    info = recovery_info(snapshot)
    if info.get("available") or not isinstance(snapshot, dict):
        return info
    if snapshot.get("phase") != "completed":
        return info
    result = snapshot.get("result")
    if not isinstance(result, dict) or not result.get("budget_exhausted"):
        return info
    try:
        state = AgentState.model_validate(snapshot)
    except ValueError:
        return info
    return {
        **info,
        "available": True,
        "phase": state.phase,
        "step": state.step + 1,
        "completed_tools": sum(key.startswith("tool/") for key in state.operations),
        "pending_operation": state.pending_operation,
    }


def _unaccounted_tool_error(task, errors):
    """Only a durable executor receipt may classify a tool error as recovered/partial."""
    if not errors:
        return ""
    checkpoint, result = task.checkpoint or {}, task.result or {}
    saved_result = checkpoint.get("result") or {}
    committed = (checkpoint.get("phase") in ("deliver", "completed")
                 and bool(result.get("qa_id")) and result.get("qa_id") == saved_result.get("qa_id"))
    facts = {fact.get("id"): fact for fact in checkpoint.get("tool_failures", [])}
    incomplete = {step.get("failure_id") for step in result.get("incomplete_steps", [])}
    for failure_id, message in errors.items():
        fact = facts.get(failure_id)
        accounted = fact and (fact.get("status") == "resolved"
            or fact.get("status") == "open" and result.get("completed") is False and failure_id in incomplete)
        if not committed or not accounted:
            return message
    return ""


def _run_in_background(task_id, question, paper_id, skills, conversation_id):
    from .db import SessionLocal

    db = SessionLocal()
    source = None
    answer, failure = "", ""
    tool_errors = {}
    was_cancelled = False
    was_paused = False
    attempt = None
    try:
        task = db.get(TaskRecord, task_id)
        if not task:
            return
        checkpoint = deepcopy(task.checkpoint)
        attempt = run_lifecycle.current_attempt(db, task_id)
        binding = (checkpoint or {}).get("runtime_config") or task.runtime_config
        if binding is None:
            binding = capture_run_config(db, conversation_id)
        task.runtime_config = binding
        events_log = list(task.events or [])
        task.status = "running"
        task.updated_at = _now()
        db.commit()
        notify_tasks_changed()

        def persist(snapshot):
            if attempt is not None:
                snapshot = run_lifecycle.bind_checkpoint(snapshot, attempt)
            run_lifecycle.save_checkpoint(db, task_id, snapshot)
            current = db.get(TaskRecord, task_id)
            if current is None:
                raise RuntimeError("任务记录已不存在，停止继续执行")
            current.checkpoint = snapshot
            if snapshot.get("runtime_config"):
                current.runtime_config = deepcopy(snapshot["runtime_config"])
            if snapshot.get("todo"):
                checklist = snapshot["todo"]
                current.plan = ([] if snapshot.get("plan_source") == "requirements" else
                                deepcopy([step for step in checklist if not step.get("acceptance")]))
                done = sum(step.get("status") == "done" for step in checklist)
                current.progress = round(done / len(checklist), 3)
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
                           runtime_config=binding,
                           reviewed_plan_id=(task.payload or {}).get("reviewed_plan_id"))
        for kind, data in source:
            if attempt is not None and not kind.endswith("_delta"):
                run_lifecycle.assert_writer(db, task_id, attempt.id, attempt.writer_token, allow_terminal=True)
            if kind == "cancelled":
                was_cancelled = True
            elif kind == "paused":
                was_paused = True
            elif kind in ("plan", "todo_update"):
                task.plan = deepcopy(data.get("steps"))
                checklist = (task.checkpoint or {}).get("todo") or task.plan or []
                total = len(checklist)
                task.progress = round(sum(s.get("status") == "done" for s in checklist) / total, 3) if total else 0
            elif kind == "answer":
                answer = data.get("answer", "")
                task.result = deepcopy(data)
            elif kind == "error":
                message = data.get("text") or "任务执行失败"
                if data.get("source") == "tool" and isinstance(data.get("failure_id"), str):
                    tool_errors[data["failure_id"]] = message
                else:
                    failure = message  # A later fallback answer cannot erase an unknown/provider error.
            elif kind == "approval_request":
                notifier.notify("研序：需要你的批准",
                    f"任务「{task.title}」请求执行「{data.get('tool_name', '')}」，请回到应用查看。", level="guarded")
            if kind in ("cancelled", "paused", "error", "approval_request", "approval_result", "project_change", "project_command", "git_operation"):
                events_log.append({"kind": kind, **data})
            elif kind in ("commentary", "task_outcome", "provider_report"):
                events_log.append({"kind": kind, **deepcopy(data)})
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
        failure = failure or _unaccounted_tool_error(task, tool_errors)
        if was_cancelled:
            task.status = "interrupted" if _shutting_down.is_set() else "cancelled"
            task.error = "应用退出，已保留执行检查点" if _shutting_down.is_set() else ""
        elif failure:
            task.status, task.error = "failed", failure
        elif was_paused or (task.checkpoint or {}).get("phase") == "paused":
            if (task.checkpoint or {}).get("resource_pause"):
                pause_reason = task.checkpoint["resource_pause"].get("message") or "资源条件暂不满足，检查点和成果已保留"
            elif int((task.checkpoint or {}).get("no_progress_segments") or 0) >= 2:
                pause_reason = "连续执行阶段没有取得新结果，进度和已完成步骤已保存；调整任务后可继续"
            else:
                pause_reason = "本轮执行预算已用完，进度和已完成步骤已保存；可追加预算后继续"
            task.status, task.error = "paused", pause_reason
        elif (task.result or {}).get("budget_exhausted"):
            # Older checkpoints completed a partial summary before the task was
            # marked failed. Keep those records resumable during migration.
            task.status, task.error = "paused", "本轮执行预算已用完，进度和阶段性结果已保存；可追加预算后继续"
        elif answer or (task.checkpoint or {}).get("phase") == "completed":
            task.status, task.error = "done", ""
            if (task.result or {}).get("completed", True):
                task.progress = 1.0
            else:
                checklist = (task.checkpoint or {}).get("todo") or task.plan or []
                total = len(checklist)
                done = sum(step.get("status") == "done" for step in checklist)
                task.progress = round(done / total, 3) if total else 0.0
        else:
            task.status, task.error = "failed", "执行未返回完整结果，已保留可用的检查点"
        if attempt is not None:
            run_lifecycle.finish_attempt(db, task_id, attempt.id, attempt.writer_token,
                                         task.status, task.error)
        db.commit()
        notify_tasks_changed()
        if not _shutting_down.is_set():
            label = ("部分完成" if task.status == "done" and (task.result or {}).get("completed") is False
                     else {"done": "已完成", "failed": "失败", "cancelled": "已取消", "paused": "已暂停"}.get(task.status, "已中断"))
            notifier.notify(f"研序：后台任务{label}", f"「{task.title}」{label}，可在任务列表查看。",
                            level="error" if failure else "info")
    except run_lifecycle.StaleAttempt:
        db.rollback()  # A superseded worker must not mark the current attempt failed.
    except Exception as exc:
        db.rollback()
        task = db.get(TaskRecord, task_id)
        if task:
            task.status = "interrupted" if isinstance(exc, CheckpointNeedsReview) else "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.updated_at = _now()
            if attempt is not None:
                run_lifecycle.finish_attempt(db, task_id, attempt.id, attempt.writer_token,
                                             task.status, task.error)
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
                from .next_turn_dispatch import wake
                wake()


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
        attempt = run_lifecycle.current_attempt(db, task.id)
        if attempt is not None:
            run_lifecycle.finish_attempt(db, task.id, attempt.id, attempt.writer_token, "failed", task.error)
        db.commit()
        notify_tasks_changed()
        raise TaskConflict(task.error, 503) from exc
    return task_to_dict(task)


def start_agent_task(db: Session, question, paper_id, skills, conversation_id, title="", *,
                     execution_mode="execute", reviewed_plan_id=None, retry_plan_run_id=None,
                     project_id=None, file_paths=None, workspace_revision=0, source_revision_ids=None):
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        if _shutting_down.is_set():
            raise TaskConflict("应用正在退出，暂不能开始新任务", 503)
        describe_paper_scope(db, paper_id)
        check_conversation_available(db, conversation_id)
        try:
            run_lifecycle.assert_conversation_idle(db, conversation_id)
        except run_lifecycle.RunConflict as exc:
            raise TaskConflict(str(exc)) from exc
        from .workspace import capture
        scope = capture(db, conversation_id, project_id, file_paths, reviewed_plan_id, workspace_revision)
        task = TaskRecord(type="agent", title=title or question[:60], status="pending", progress=0,
            paper_id=paper_id, conversation_id=conversation_id or f"c_{uuid4().hex[:24]}",
            payload={"question": question, "paper_id": paper_id, "skills": skills or [],
                     "source_revision_ids": list(source_revision_ids or []),
                     "execution_mode": execution_mode, "reviewed_plan_id": reviewed_plan_id, "workspace": scope},
            events=[], plan=None, result=None, checkpoint=None)
        db.add(task)
        db.flush()
        run_lifecycle.admit_attempt(db, task.id, task.conversation_id, question, kind="background")
        claim_reviewed_plan(db, reviewed_plan_id, task.conversation_id, task.id, retry_plan_run_id)
        ensure_conversation(db, task.conversation_id, title or question)
        try:
            task.runtime_config = capture_run_config(db, task.conversation_id)
            from .source_runtime import freeze_sources, validate_plan_sources
            task.runtime_config = freeze_sources(db, task.conversation_id, source_revision_ids or [], task.runtime_config)
            from .history_revisions import bind_for_run
            task.runtime_config = bind_for_run(db, task.conversation_id, task.id, question, task.runtime_config)
            validate_plan_sources(db, task.conversation_id, reviewed_plan_id, source_revision_ids or [])
        except SourceError as exc:
            db.rollback()
            raise TaskConflict(str(exc)) from exc
        except ModelConfigurationError as exc:
            db.rollback()
            raise TaskConflict(str(exc), exc.status_code) from exc
        db.commit()
        db.refresh(task)
        notify_tasks_changed()
        return _launch(db, task)


def resume_task(db: Session, task_id: str, resolution: str | None = None,
                 additional_steps: int = DEFAULT_RESUME_STEPS, *, resume_request_id=None,
                 expected_run_revision=None):
    if type(additional_steps) is not int or not 1 <= additional_steps <= MAX_RESUME_STEPS:
        raise TaskConflict(f"追加轮数必须是 1 到 {MAX_RESUME_STEPS} 之间的整数", 422)
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
        if resume_request_id:
            if run_lifecycle.admitted_request(db, task_id, resume_request_id) is not None:
                db.rollback()
                return task_to_dict(task)
        try:
            run_lifecycle.assert_conversation_idle(db, task.conversation_id, exclude_run_id=task_id)
        except run_lifecycle.RunConflict as exc:
            raise TaskConflict(str(exc)) from exc
        info = recovery_info(task.checkpoint)
        if task.status == "done" and not info.get("available"):
            raise TaskConflict(info.get("reason") or "此任务已完成，请选择从头重新运行")
        snapshot = task.checkpoint
        legacy_budget_pause = (
            isinstance(snapshot, dict)
            and snapshot.get("phase") == "completed"
            and isinstance(snapshot.get("result"), dict)
            and bool(snapshot["result"].get("budget_exhausted"))
        )
        if not info["available"] and not legacy_budget_pause:
            raise TaskConflict(info.get("reason") or "此任务无法从检查点恢复")
        state = AgentState.model_validate(task.checkpoint)
        budget_pause = state.phase == "paused" or (
            state.phase == "completed" and bool((state.result or {}).get("budget_exhausted"))
        )
        payload = task.payload or {}
        if (state.question, state.conversation_id, state.paper_id, state.skills) != (
                payload.get("question", task.title), task.conversation_id,
                payload.get("paper_id", task.paper_id), payload.get("skills") or []):
            raise TaskConflict("检查点与原任务的问题、附件或会话不匹配")
        from .workspace import validate_scope
        validate_scope(db, task.conversation_id, state.workspace)
        if state.workspace != payload.get("workspace"):
            raise TaskConflict("检查点与任务的项目文件快照不匹配")
        if (state.execution_mode, state.reviewed_plan_id) != (
                payload.get("execution_mode", "execute"), payload.get("reviewed_plan_id")):
            raise TaskConflict("检查点的执行模式或计划来源不匹配")
        if state.reviewed_plan_id:
            reviewed_plan_text(db, state.reviewed_plan_id, task.conversation_id, task.id)
        from .agent.checkpoint import permit_durable_child_resume
        permit_durable_child_resume(state, db, task_id)
        if state.pending_operation and resolution not in ("retry", "skip") and not state.resolution:
            raise TaskConflict("上一步执行结果未确认，请检查后选择重试或跳过")
        if resolution and not state.pending_operation:
            raise TaskConflict("该步骤已不需要处理，请刷新任务状态")
        if budget_pause:
            if state.pending_operation:
                raise TaskConflict("预算暂停时存在未确认操作，无法安全追加预算")
        state, new_work = prepare_resume(state.snapshot(), additional_steps)
        try:
            binding = state.runtime_config or task.runtime_config or capture_run_config(db, task.conversation_id)
            run_lifecycle.validate_binding(db, binding)
            from .source_runtime import validate_frozen_sources
            validate_frozen_sources(db, task.conversation_id, binding, expected_ids=state.source_revision_ids)
            state.runtime_config = binding
        except SourceError as exc:
            raise TaskConflict(str(exc)) from exc
        except ModelConfigurationError as exc:
            raise TaskConflict(str(exc), exc.status_code) from exc
        try:
            attempt, created = run_lifecycle.admit_attempt(
                db, task_id, task.conversation_id, state.question, kind="background",
                checkpoint=task.checkpoint, resume=True, request_id=resume_request_id,
                expected_revision=expected_run_revision)
        except run_lifecycle.RunConflict as exc:
            raise TaskConflict(str(exc)) from exc
        if not created:
            db.rollback()
            return task_to_dict(task)
        state.attempt_id, state.writer_token = attempt.id, attempt.writer_token
        state.delivery_only = (attempt.checkpoint or {}).get("delivery_only", False)
        state.source_attempt_id = (attempt.checkpoint or {}).get("source_attempt_id")
        state.resolution = resolution or state.resolution
        task.checkpoint = state.snapshot()
        attempt.checkpoint = deepcopy(task.checkpoint)
        if budget_pause:
            task.result = None
        task.status, task.error = "pending", ""
        event_text = {"retry": "用户选择重试中断的步骤", "skip": "用户选择跳过中断的步骤"}.get(resolution)
        if event_text is None:
            event_text = (f"追加 {additional_steps} 轮预算，从已保存检查点继续" if budget_pause
                          else "保留已有成果，仅继续未完成要求" if new_work else "从已保存检查点继续")
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
                                source_revision_ids=(task.runtime_config or {}).get("source_revision_ids", []),
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
                task.result = (task.checkpoint or {}).get("result") or task.result
                if (task.result or {}).get("budget_exhausted"):
                    task.status = "paused"
                    task.error = "本轮执行预算已用完，进度和阶段性结果已保存；可追加预算后继续"
                else:
                    task.status = "done"
                    if (task.result or {}).get("completed", True):
                        task.progress = 1.0
                    else:
                        checklist = (task.checkpoint or {}).get("todo") or task.plan or []
                        total = len(checklist)
                        done = sum(step.get("status") == "done" for step in checklist)
                        task.progress = round(done / total, 3) if total else 0.0
                    task.error = ""
            elif info.get("phase") == "paused":
                task.status = "paused"
                state = AgentState.model_validate(task.checkpoint or {})
                task.error = (
                    "连续执行阶段没有取得新结果，进度已保存；调整任务后可继续"
                    if state.no_progress_segments >= 2
                    else "本轮执行预算已用完，进度已保存；可追加预算后继续"
                )
            else:
                task.status = "interrupted"
                task.error = info.get("reason") or "应用上次退出时任务未完成，可从检查点继续"
                _event(task, "interrupted", task.error)
            attempt = run_lifecycle.current_attempt(db, task.id)
            if attempt is not None:
                run_lifecycle.finish_attempt(db, task.id, attempt.id, attempt.writer_token,
                                             task.status, task.error)
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
    from sqlalchemy.orm import object_session
    session = object_session(task)
    lifecycle = run_lifecycle.public_run(session, task.id) if session is not None else None
    return {"id": task.id, "type": task.type, "title": task.title, "status": task.status,
        "goal_id": (lifecycle or {}).get("goal_id"), "run_revision": (lifecycle or {}).get("revision"),
        "attempt_id": (lifecycle or {}).get("attempt_id"), "attempt_number": (lifecycle or {}).get("attempt_number", 0),
        "runtime_config": public_runtime_config(task.runtime_config),
        "source_revision_ids": (task.runtime_config or {}).get("source_revision_ids", []),
        "execution_mode": (task.payload or {}).get("execution_mode", "execute"),
        "reviewed_plan_id": (task.payload or {}).get("reviewed_plan_id"),
        "executing": task.id in running_task_ids(),
        "progress": task.progress, "plan": task.plan, "events": task.events,
        "approvals": approval_store.list_for_run(task.id), "result": task.result, "error": task.error,
        "recovery": _task_recovery_info(task.checkpoint), "conversation_id": task.conversation_id,
        "outcome": run_lifecycle.result_outcome(task.result),
        "delivery_type": "planned" if (task.result or {}).get("outcome") == "planned" else "answer",
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
