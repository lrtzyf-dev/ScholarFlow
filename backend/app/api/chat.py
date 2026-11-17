"""Stream foreground work independently of the browser's connection lifetime."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from contextlib import closing
from copy import deepcopy

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from .. import cancel_store, run_store, chat_events, run_lifecycle
from ..agent.loop import run_agent
from ..conversation_store import ConversationArchived, ConversationDeleted, check_conversation_available
from sqlalchemy import text
from ..db import SessionLocal
from ..foreground_run_store import finish as finish_foreground_run, save_agent_checkpoint
from ..models import DeletedConversation, ForegroundRun
from ..schemas import ChatRequest
from ..execution_mode import PlanConflict, claim_reviewed_plan
from .. import workspace
from ..agent.paper_scope import PaperScopeError, describe as describe_paper_scope
from ..timeutil import utcnow
from ..agent.checkpoint import AgentState, recovery_info, prepare_resume
from ..model_profiles import capture_run_config, ModelConfigurationError
from ..source_runtime import freeze_sources, validate_frozen_sources
from ..source_store import SourceError

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _capture_binding(db, conversation_id):
    try:
        return capture_run_config(db, conversation_id)
    except ModelConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str = Path(pattern=r"^chat_[A-Za-z0-9_-]{8,64}$")):
    return {"run_id": run_id, "status": run_store.cancel(run_id)}


def _event_frame(event: dict) -> str:
    sequence = event.get("seq", event.get("cursor"))
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    return prefix + f"event: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _observe(channel, observer, *, snapshot=None, initial_run=False):
    """An observer owns no model or execution lifecycle."""
    cursor = snapshot["cursor"] if snapshot is not None else 0
    try:
        if initial_run:
            yield _event_frame({"kind": "run", "run_id": channel.state["run_id"],
                                "attempt_id": channel.state.get("attempt_id"), "goal_id": channel.state.get("goal_id")})
        if snapshot is not None:
            yield _event_frame(snapshot)
            if snapshot["terminal"]:
                yield _event_frame({"kind": "stream_end", "run_id": snapshot["run_id"],
                                    "seq": snapshot["cursor"], "status": snapshot["status"], "terminal": True})
                return
        while True:
            try:
                event = await asyncio.to_thread(observer.get, True, 0.25)
            except queue.Empty:
                state = channel.snapshot()
                if state["terminal"]:
                    yield _event_frame(state)
                    yield _event_frame({"kind": "stream_end", "run_id": state["run_id"],
                                        "seq": state["cursor"], "status": state["status"], "terminal": True})
                    return
                yield ": keep-alive\n\n"
                continue
            if event is chat_events.RESYNC:
                event = channel.snapshot()
            # A producer may record an event immediately before subscribe()
            # captures it, then broadcast it after registration. Its content is
            # already in the initial snapshot; do not append that delta twice.
            sequence = event.get("seq", event.get("cursor", 0))
            if event["kind"] != "snapshot" and sequence <= cursor:
                continue
            cursor = max(cursor, sequence)
            yield _event_frame(event)
            if event["kind"] == "stream_end" or (event["kind"] == "snapshot" and event["terminal"]):
                return
    finally:
        channel.unsubscribe(observer)


@router.get("/runs/{run_id}/events")
def observe_run(run_id: str = Path(pattern=r"^chat_[A-Za-z0-9_-]{8,64}$"),
                conversation_id: str = Query(min_length=1, max_length=64, pattern=r"^[\w-]+$"),
                after: int = Query(default=0, ge=0)):
    """Reattach via a full public snapshot followed by strictly newer events.

    ``after`` is a client deduplication cursor, not an instruction to execute or
    replay tools. A full snapshot is always sent, including when a slow observer
    fell behind a compacted buffer. Its cursor atomically separates future events.
    """
    # Run admission publishes its observer channel under the same lock. A view
    # arriving between the DB commit and channel registration must wait for
    # publication rather than mistake a new live run for an orphaned worker.
    with run_store.coordinated(), SessionLocal() as db:
        if db.get(DeletedConversation, conversation_id):
            raise HTTPException(410, "该会话已删除，不能观察旧轮次")
        run = db.get(ForegroundRun, run_id)
        if run is None or run.conversation_id != conversation_id or run.status == "dismissed":
            raise HTTPException(404, "此会话中没有可观察的轮次")
        channel = chat_events.get(run_id)
        if channel is not None and channel.state["conversation_id"] == conversation_id:
            snapshot, observer = channel.subscribe()
            source = _observe(channel, observer, snapshot=snapshot)
        else:
            snapshot = chat_events.display_snapshot(run)
            if snapshot["status"] == "running":
                # No worker in this process can continue a pre-restart stream.
                snapshot.update(status="interrupted", terminal=True,
                                error="执行进程已停止，显示最后保存的输出与检查点。")

            async def saved_stream():
                yield _event_frame(snapshot)
                yield _event_frame({"kind": "stream_end", "run_id": run_id, "seq": snapshot["cursor"],
                                    "status": snapshot["status"], "terminal": True})
            source = saved_stream()
    return StreamingResponse(source, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": run_id,
    })


def _admitted_response(db, attempt, conversation_id):
    """A delayed retry observes its own admission, never launches another one."""
    from ..runtime_models import RuntimeRun
    logical = db.get(RuntimeRun, attempt.run_id)
    if logical is not None and logical.current_attempt_id == attempt.id:
        return observe_run(attempt.run_id, conversation_id, 0)
    result = attempt.result or {}
    status = "completed" if attempt.status == "ended" else attempt.status
    saved = {"result": result or None, "partial_answer": result.get("answer", "")}
    snapshot = chat_events.initial_snapshot(attempt.run_id, conversation_id, saved,
        status=status, phase="completed" if result else "interrupted", error=attempt.error,
        attempt_id=attempt.id, goal_id=logical.goal_id if logical else None)
    admitted_run_id, admitted_attempt_id = attempt.run_id, attempt.id

    async def saved_stream():
        yield _event_frame(snapshot)
        yield _event_frame({"kind": "stream_end", "run_id": admitted_run_id,
                            "attempt_id": admitted_attempt_id, "seq": snapshot["cursor"],
                            "status": status, "terminal": True})

    return StreamingResponse(saved_stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": admitted_run_id,
    })


@router.post("/stream")
def chat_stream(req: ChatRequest):
    run_id = req.run_id or f"chat_{uuid.uuid4().hex}"
    with run_store.coordinated():
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            try:
                check_conversation_available(db, req.conversation_id)
            except ConversationDeleted as exc:
                raise HTTPException(410, str(exc)) from exc
            except ConversationArchived as exc:
                raise HTTPException(409, str(exc)) from exc
            try:
                # Keep the selected identity valid without requiring a ready
                # index for ordinary conversation. Actual retrieval stays strict.
                describe_paper_scope(db, req.paper_id)
            except PaperScopeError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            checkpoint = db.get(ForegroundRun, run_id)
            from ..next_turn_dispatch import validate_child_admission
            validate_child_admission(db, run_id)
            if checkpoint is not None and checkpoint.status == "dismissed":
                raise HTTPException(409, "此前台轮次已移除，请新建一轮请求")
            display_seed = checkpoint.display_snapshot if checkpoint else None
            binding = checkpoint.runtime_config if checkpoint else None
            try:
                if checkpoint:
                    validate_frozen_sources(db, req.conversation_id, binding, expected_ids=req.source_revision_ids)
                elif req.source_revision_ids:
                    # Validate authorization before preparing any model attempt.
                    freeze_sources(db, req.conversation_id, req.source_revision_ids, {})
            except SourceError as exc:
                raise HTTPException(409, str(exc)) from exc
            agent_checkpoint = None
            workspace.check_revision(db, req.conversation_id, req.workspace_revision)
            if checkpoint:
                scope = checkpoint.workspace
                workspace.validate_scope(db, req.conversation_id, scope)
                if (scope or {}).get("project_id") != req.project_id or (
                    not req.reviewed_plan_id and [f["path"] for f in (scope or {}).get("files", [])] != list(dict.fromkeys(req.file_paths))):
                    raise HTTPException(409, "项目或引用文件与前台检查点不匹配")
            else:
                scope = workspace.capture(db, req.conversation_id, req.project_id, req.file_paths, req.reviewed_plan_id, req.workspace_revision)
            if req.reviewed_plan_id and checkpoint is None:
                raise HTTPException(409, "执行已审阅计划前必须创建前台检查点")
            if checkpoint and (checkpoint.execution_mode, checkpoint.reviewed_plan_id, checkpoint.conversation_id) != (
                    req.execution_mode, req.reviewed_plan_id, req.conversation_id):
                raise HTTPException(409, "执行模式、计划来源或会话与前台检查点不匹配")
            if checkpoint and (req.question, req.paper_id, req.conversation_id,
                    req.skills or [], req.images or []) != (checkpoint.question, checkpoint.paper_id,
                    checkpoint.conversation_id, checkpoint.skills or [], checkpoint.images or []):
                raise HTTPException(409, "问题或输入附件与已保存检查点不匹配")
            # A lost resume response is an observer retry, never a second
            # executor. Resolve its durable request ID before inspecting the
            # now-advanced checkpoint or changing its budget again.
            if req.resume_checkpoint and req.resume_request_id:
                previous = run_lifecycle.admitted_request(db, run_id, req.resume_request_id)
                if previous is not None:
                    db.rollback()
                    return _admitted_response(db, previous, req.conversation_id)
            if req.resume_checkpoint:
                if (checkpoint is None or not checkpoint.agent_checkpoint
                        or checkpoint.status not in ("paused", "interrupted", "cancelled", "failed", "completed")):
                    raise HTTPException(409, "此轮没有可继续的检查点，请刷新任务状态")
                if run_id in run_store.active_runs():
                    raise HTTPException(409, "此轮仍在执行，请重新连接查看进度")
                resume_base = checkpoint.agent_checkpoint
                try:
                    state = AgentState.model_validate(checkpoint.agent_checkpoint)
                except ValueError as exc:
                    raise HTTPException(409, "执行检查点格式不兼容，无法安全继续") from exc
                if (not recovery_info(checkpoint.agent_checkpoint).get("available") or state.question != req.question
                        or state.conversation_id != req.conversation_id or state.paper_id != req.paper_id
                        or state.skills != (req.skills or [])
                        or state.source_revision_ids != req.source_revision_ids
                        or state.execution_mode != req.execution_mode
                        or state.reviewed_plan_id != req.reviewed_plan_id or state.workspace != scope):
                    raise HTTPException(409, "执行检查点与当前会话、项目或计划不匹配")
                from ..agent.checkpoint import permit_durable_child_resume
                permit_durable_child_resume(state, db, run_id)
                if state.pending_operation and not state.resolution:
                    raise HTTPException(409, "上一步操作结果待确认，不能直接续跑")
                state, _ = prepare_resume(state.snapshot(), req.additional_steps)
                binding = state.runtime_config or binding or _capture_binding(db, req.conversation_id)
                state.runtime_config = binding
                agent_checkpoint = state.snapshot()
                checkpoint.agent_checkpoint = agent_checkpoint
                checkpoint.phase = state.phase
                checkpoint.status = "running"
                checkpoint.error = ""
                checkpoint.updated_at = utcnow()
                checkpoint.heartbeat_at = checkpoint.updated_at
                checkpoint.revision += 1
            elif checkpoint:
                if checkpoint.status != "running" or checkpoint.started_at is not None:
                    detail = "此轮有已保存的暂停检查点，请选择继续当前任务" if checkpoint.status == "paused" else "此轮次已启动或结束，请明确选择重新提问"
                    raise HTTPException(409, detail)
                checkpoint.started_at = utcnow()
            elif req.resume_checkpoint:
                raise HTTPException(409, "找不到可继续的前台检查点")
            if binding is None:
                binding = _capture_binding(db, req.conversation_id)
                try:
                    binding = freeze_sources(db, req.conversation_id, req.source_revision_ids, binding)
                except SourceError as exc:
                    raise HTTPException(409, str(exc)) from exc
            try:
                validate_frozen_sources(db, req.conversation_id, binding, expected_ids=req.source_revision_ids)
            except SourceError as exc:
                raise HTTPException(409, str(exc)) from exc
            if req.resume_checkpoint or checkpoint is not None:
                try:
                    run_lifecycle.validate_binding(db, binding)
                except ModelConfigurationError as exc:
                    raise HTTPException(exc.status_code, str(exc)) from exc
            if checkpoint:
                checkpoint.runtime_config = binding
            from ..history_revisions import bind_for_run, HistoryRevisionError
            try:
                binding = bind_for_run(db, req.conversation_id, run_id, req.question, binding)
            except HistoryRevisionError as exc:
                raise HTTPException(exc.status, str(exc)) from None
            if checkpoint:
                checkpoint.runtime_config = binding
            if agent_checkpoint is not None:
                agent_checkpoint["runtime_config"] = deepcopy(binding)
            try:
                claim_reviewed_plan(db, req.reviewed_plan_id, req.conversation_id, run_id, req.retry_plan_run_id)
            except PlanConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            try:
                attempt, created = run_lifecycle.admit_attempt(
                    db, run_id, req.conversation_id, req.question,
                    checkpoint=resume_base if req.resume_checkpoint else None,
                    resume=req.resume_checkpoint, request_id=req.resume_request_id,
                    expected_revision=req.expected_run_revision)
            except run_lifecycle.RunConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            attempt_id, writer_token = attempt.id, attempt.writer_token
            identity = run_lifecycle.public_run(db, run_id)
            if agent_checkpoint is not None:
                agent_checkpoint = run_lifecycle.bind_checkpoint(agent_checkpoint, attempt)
                agent_checkpoint.update(delivery_only=(attempt.checkpoint or {}).get("delivery_only", False),
                                        source_attempt_id=(attempt.checkpoint or {}).get("source_attempt_id"))
                checkpoint.agent_checkpoint = agent_checkpoint
                attempt.checkpoint = deepcopy(agent_checkpoint)
            if not created:
                db.rollback()
                return _admitted_response(db, attempt, req.conversation_id)
            try:
                run_lifecycle.assert_conversation_idle(db, req.conversation_id, exclude_run_id=run_id)
            except run_lifecycle.RunConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            if not run_store.begin(run_id, req.conversation_id, attempt_id=attempt_id):
                raise HTTPException(409, "执行 ID 已使用，请新建一轮请求")
            try:
                db.commit()
            except Exception:
                run_store.finish(run_id, "failed", attempt_id=attempt_id)
                raise

        channel = chat_events.begin(run_id, req.conversation_id,
                                    display_seed,
                                    runtime_config=binding, durable=checkpoint is not None,
                                    reset_answer=req.resume_checkpoint, attempt_id=attempt_id,
                                    goal_id=identity["goal_id"])
        _, observer = channel.subscribe()
    progress_write_lock = threading.Lock()
    saved_progress_version = -1

    def persist_progress(db, *, final: bool = False) -> None:
        nonlocal saved_progress_version
        # Serialize heartbeat and final flushes so a delayed heartbeat cannot
        # overwrite a newer final buffer. The executor's private checkpoint is
        # deliberately untouched by this UI-only progress write.
        with progress_write_lock:
            run_lifecycle.assert_writer(db, run_id, attempt_id, writer_token, allow_terminal=True, lock=True)
            display = channel.snapshot()
            version, answer, phase = display["cursor"], display["partial_answer"], display["phase"]
            statuses = ("running", "paused", "cancelled", "failed", "completed", "interrupted") if final else ("running",)
            changes = {"heartbeat_at": utcnow()}
            if version != saved_progress_version:
                changes.update(partial_answer=answer, phase=phase,
                               display_snapshot=display,
                               updated_at=utcnow(), revision=ForegroundRun.revision + 1)
            updated = db.query(ForegroundRun).filter(
                ForegroundRun.id == run_id, ForegroundRun.status.in_(statuses),
            ).update(changes, synchronize_session=False)
            db.commit()
            if updated:
                saved_progress_version = version

    def worker():
        status = "done"
        heartbeat_stop = threading.Event()

        def heartbeat():
            last_heartbeat = 0.0
            while not heartbeat_stop.wait(0.5):
                changed = channel.state["cursor"] != saved_progress_version
                if not changed and time.monotonic() - last_heartbeat < 5:
                    continue
                try:
                    with SessionLocal() as heartbeat_db:
                        persist_progress(heartbeat_db)
                    last_heartbeat = time.monotonic()
                except Exception:
                    continue

        heartbeat_thread = threading.Thread(target=heartbeat, name=f"{run_id}-heartbeat", daemon=True)
        try:
            heartbeat_thread.start()
            with SessionLocal() as db:
                def display_event_sink(kind, event):
                    # Public progress can arrive between synchronous audit
                    # batches. It uses the same observer/snapshot path and
                    # the same admitted writer as yielded agent events.
                    run_lifecycle.assert_writer(db, run_id, attempt_id, writer_token, allow_terminal=True)
                    channel.broadcast(channel.record(kind, event))

                with closing(run_agent(
                    db, req.question, req.paper_id, selected_skills=req.skills,
                    conversation_id=req.conversation_id, images=req.images, run_id=run_id,
                    checkpoint=agent_checkpoint,
                    save_checkpoint=lambda snapshot: save_agent_checkpoint(db, run_id, snapshot),
                    execution_mode=req.execution_mode, reviewed_plan_id=req.reviewed_plan_id,
                    workspace=scope,
                    runtime_config=binding,
                    display_event_sink=display_event_sink,
                )) as source:
                    for kind, event in source:
                        if not kind.endswith("_delta"):
                            run_lifecycle.assert_writer(db, run_id, attempt_id, writer_token, allow_terminal=True)
                        public_event = channel.record(kind, event)
                        if kind == "cancelled":
                            status = "cancelled"
                        elif kind == "paused":
                            status = "paused"
                        elif kind == "error":
                            status = "failed"
                        # Losing an observer is not a user cancellation. Keep
                        # consuming the same executor and let its own durable
                        # checkpoints/answer commit finish normally.
                        if kind in ("answer", "done", "paused", "cancelled"):
                            persist_progress(db, final=True)
                        channel.broadcast(public_event)
            if cancel_store.is_cancelled(run_id):
                status = "cancelled"
        except run_lifecycle.StaleAttempt:
            status = "superseded"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            event = {"kind": "error", "text": f"{type(exc).__name__}: {exc}"}
            channel.broadcast(channel.record("error", event))
        finally:
            heartbeat_stop.set()
            if heartbeat_thread.ident is not None:
                heartbeat_thread.join(timeout=1)
            # run_agent marks successful delivery as "completed" inside its
            # finalize stage. This fallback only closes runs still left in
            # "running" by an early cancellation or failure; finish() ignores
            # any row whose status is no longer "running".
            try:
                with SessionLocal() as db:
                    persist_progress(db, final=True)
                    run = db.get(ForegroundRun, run_id)
                    if run is not None and run.status == "completed":
                        if status in ("cancelled", "failed") and (run.agent_checkpoint or {}).get("phase") != "completed":
                            run.status = status
                            run.error = "回答已保存，但交付后的操作未完成，已保留检查点等待核对。"
                        else:
                            status = "done"
                    elif run is not None and run.status == "paused":
                        status = "paused"
                    elif run is not None and run.status == "running" and status == "done":
                        status = "failed"
                    if status in ("cancelled", "failed"):
                        finish_foreground_run(
                            db, run_id, status,
                            "任务执行在提交最终回答前结束，已保留检查点。" if status == "failed" else "",
                            attempt_id=attempt_id, writer_token=writer_token)
                    run_lifecycle.finish_attempt(db, run_id, attempt_id, writer_token, status)
                    db.commit()
                    public_status = "completed" if status == "done" else status
                    ending = channel.end(public_status, run.error if run is not None else "")
                    persist_progress(db, final=True)
                    channel.broadcast(ending)
            except Exception:
                if channel.closed_at is None:
                    channel.broadcast(channel.end("failed", "最终进度保存失败，请核对会话记录。"))
            run_store.finish(run_id, status, attempt_id=attempt_id)
            from ..next_turn_dispatch import wake
            wake()
            chat_events.release_finished()

    # Admit and start work before sending success headers. Once the caller
    # receives 200 it may detach its observer without cancelling an unstarted
    # request or launching the same work again.
    try:
        threading.Thread(target=worker, name=run_id, daemon=True).start()
    except Exception:
        try:
            with SessionLocal() as db:
                finish_foreground_run(db, run_id, "failed", "任务执行线程启动失败，请重试。",
                                      attempt_id=attempt_id, writer_token=writer_token)
                run_lifecycle.finish_attempt(db, run_id, attempt_id, writer_token, "failed", "执行线程启动失败")
                db.commit()
        except Exception:
            pass
        finally:
            run_store.finish(run_id, "failed", attempt_id=attempt_id)
            channel.broadcast(channel.end("failed", "任务执行线程启动失败，请重试。"))
            channel.unsubscribe(observer)
            chat_events.release_finished()
        raise

    return StreamingResponse(_observe(channel, observer, initial_run=True), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": run_id,
    })
