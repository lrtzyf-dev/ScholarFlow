"""Durable foreground answer checkpoints and restart recovery."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, or_, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from ..db import get_db
from .. import approval_store, run_store, task_events
from ..models import Conversation, DeletedConversation, ForegroundRun
from ..agent.checkpoint import AgentState, recovery_info
from ..agent.paper_scope import PaperScopeError, describe as describe_paper_scope
from .desktop_state import DraftImage, MAX_TOTAL_IMAGE_BYTES
from ..execution_mode import ExecutionOptions, PlanConflict, claim_reviewed_plan
from .. import workspace
from ..timeutil import utcnow
from ..model_profiles import capture_run_config, ModelConfigurationError
from ..runtime_context import public_runtime_config
from ..source_runtime import freeze_sources, validate_plan_sources
from ..source_store import SourceError

router = APIRouter(prefix="/api/foreground-runs", tags=["foreground-runs"])
RUN_ID = re.compile(r"^chat_[A-Za-z0-9_-]{8,64}$")
HEARTBEAT_TIMEOUT = timedelta(seconds=20)
MAX_ANSWER_CHARS = 300_000


def _resume_available(run: ForegroundRun) -> bool:
    if run.status not in ("paused", "interrupted", "cancelled", "failed", "completed") or not run.agent_checkpoint:
        return False
    try:
        state = AgentState.model_validate(run.agent_checkpoint)
    except (TypeError, ValueError):
        return False
    info = recovery_info(run.agent_checkpoint)
    return (
        info.get("available", False)
        and (state.pending_operation is None or bool((info.get("automatic_child_recovery") or {}).get("safe")))
        and state.source_revision_ids == (run.runtime_config or {}).get("source_revision_ids", [])
        and (state.question, state.conversation_id, state.paper_id, state.skills,
             state.execution_mode, state.reviewed_plan_id, state.workspace)
        == (run.question, run.conversation_id, run.paper_id, run.skills or [],
            run.execution_mode, run.reviewed_plan_id, run.workspace)
    )


class CreateRun(ExecutionOptions):
    id: str = Field(pattern=r"^chat_[A-Za-z0-9_-]{8,64}$")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    question: str = Field(min_length=1, max_length=200_000)
    paper_id: str | None = Field(default=None, max_length=128)
    skills: list[str] = Field(default_factory=list, max_length=32)
    images: list[DraftImage] = Field(default_factory=list, max_length=4)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value

    @model_validator(mode="after")
    def validate_payload(self):
        if any(len(skill) > 128 for skill in self.skills):
            raise ValueError("技能名过长")
        if sum(len(base64.b64decode(image.data)) for image in self.images) > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("图片合计不能超过 8 MB")
        return self


class PatchRun(BaseModel):
    revision: int = Field(ge=1)
    partial_answer: str | None = Field(default=None, max_length=MAX_ANSWER_CHARS)
    phase: str | None = Field(default=None, max_length=32)
    status: Literal["running", "paused", "interrupted", "cancelled", "failed"] | None = None
    error: str | None = Field(default=None, max_length=2000)


def _snapshot(run: ForegroundRun):
    from ..chat_events import display_snapshot
    display = display_snapshot(run)
    from sqlalchemy.orm import object_session
    from ..run_lifecycle import public_run, result_outcome
    session = object_session(run)
    lifecycle = public_run(session, run.id) if session is not None else None
    info = recovery_info(run.agent_checkpoint)
    return {
        "id": run.id, "conversation_id": run.conversation_id, "revision": run.revision,
        "question": run.question, "paper_id": run.paper_id, "skills": run.skills or [],
        "execution_mode": run.execution_mode, "reviewed_plan_id": run.reviewed_plan_id,
        "project_id": (run.workspace or {}).get("project_id"),
        "workspace_revision": (run.workspace or {}).get("workspace_revision", 0),
        "file_paths": [f["path"] for f in (run.workspace or {}).get("files", [])],
        "source_revision_ids": (run.runtime_config or {}).get("source_revision_ids", []),
        "images": run.images or [], "partial_answer": run.partial_answer or "",
        "phase": run.phase, "status": run.status, "error": run.error or "",
        "resume_available": _resume_available(run),
        "resume_kind": info.get("kind"), "resume_reason": info.get("reason", ""),
        "resource_pause": info.get("resource_pause"),
        "outcome": (lifecycle or {}).get("outcome", result_outcome((run.agent_checkpoint or {}).get("result"))),
        "delivery_type": (lifecycle or {}).get("delivery_type", "planned" if run.execution_mode == "plan" else "answer"),
        "run_revision": (lifecycle or {}).get("revision"),
        "attempt_id": (lifecycle or {}).get("attempt_id"),
        "attempt_number": (lifecycle or {}).get("attempt_number", 0),
        "goal_id": (lifecycle or {}).get("goal_id"),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        "approvals": approval_store.list_for_run(run.id) if run.status == "running" else [],
        "runtime_config": public_runtime_config(run.runtime_config),
        "display_snapshot": display,
        "event_cursor": display["cursor"],
    }


def _require_available(db: Session, conversation_id: str):
    if db.get(DeletedConversation, conversation_id):
        raise HTTPException(410, "该会话已删除，不能恢复前台轮次")


@router.post("")
def create_run(req: CreateRun, db: Session = Depends(get_db)):
    with run_store.coordinated():
        return _create_run(req, db)


def _create_run(req: CreateRun, db: Session):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        _require_available(db, req.conversation_id)
        conv = db.get(Conversation, req.conversation_id)
        if conv is not None and conv.archived_at is not None:
            raise HTTPException(409, "该会话已归档，请先恢复会话后继续")
        from ..run_lifecycle import assert_conversation_idle, RunConflict
        try:
            assert_conversation_idle(db, req.conversation_id)
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        describe_paper_scope(db, req.paper_id)
        scope = workspace.capture(db, req.conversation_id, req.project_id, req.file_paths, req.reviewed_plan_id, req.workspace_revision)
        try:
            claim_reviewed_plan(db, req.reviewed_plan_id, req.conversation_id, req.id, req.retry_plan_run_id)
        except PlanConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        now = utcnow()
        binding = capture_run_config(db, req.conversation_id)
        binding = freeze_sources(db, req.conversation_id, req.source_revision_ids, binding)
        validate_plan_sources(db, req.conversation_id, req.reviewed_plan_id, req.source_revision_ids)
        result = db.execute(insert(ForegroundRun).values(
            id=req.id, conversation_id=req.conversation_id, revision=1,
            question=req.question, paper_id=req.paper_id, skills=req.skills,
            execution_mode=req.execution_mode, reviewed_plan_id=req.reviewed_plan_id,
            workspace=scope,
            runtime_config=binding,
            images=[image.model_dump() for image in req.images], partial_answer="",
            phase="setup", status="running", error="", created_at=now, updated_at=now,
            heartbeat_at=now,
        ).on_conflict_do_nothing(index_elements=["id"]))
        if result.rowcount != 1:
            raise HTTPException(409, "前台轮次 ID 已使用")
        db.commit()
        task_events.notify_tasks_changed()
        return {"id": req.id, "revision": 1, "status": "running", "updated_at": now.isoformat(),
                "runtime_config": public_runtime_config(binding)}
    except SourceError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (ModelConfigurationError, PaperScopeError) as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.get("")
def list_runs(conversation_id: str = Query(min_length=1, max_length=64, pattern=r"^[\w-]+$"),
              db: Session = Depends(get_db)):
    with run_store.coordinated():
        return _list_runs(conversation_id, db)


def _list_runs(conversation_id: str, db: Session):
    _require_available(db, conversation_id)
    now = utcnow()
    cutoff = now - HEARTBEAT_TIMEOUT
    stale = db.query(ForegroundRun).filter(
        ForegroundRun.conversation_id == conversation_id,
        ForegroundRun.status == "running", ForegroundRun.heartbeat_at < cutoff,
    ).all()
    for run in stale:
        if run.id in run_store.active_runs():
            continue
        if run.agent_checkpoint and run.agent_checkpoint.get("phase") == "paused":
            run.status = "paused"
            try:
                state = AgentState.model_validate(run.agent_checkpoint)
            except (TypeError, ValueError):
                state = None
            run.error = (
                "连续阶段没有取得新结果，已保留完整检查点，可调整任务后继续。"
                if state and state.no_progress_segments >= 2
                else "本段执行预算已用完，已保留完整检查点，可继续当前任务。"
            )
        else:
            run.status = "interrupted"
            run.error = "前台连接已中断，保留了最后一次保存的部分回答。"
        run.updated_at = now
        run.heartbeat_at = now
        run.revision += 1
        from ..run_lifecycle import current_attempt, finish_attempt
        attempt = current_attempt(db, run.id)
        if attempt is not None:
            finish_attempt(db, run.id, attempt.id, attempt.writer_token, run.status, run.error)
    if stale:
        db.commit()
        task_events.notify_tasks_changed()
    runs = db.query(ForegroundRun).filter(
        ForegroundRun.conversation_id == conversation_id,
        or_(ForegroundRun.status.in_(("running", "paused", "interrupted", "cancelled", "failed")),
            and_(ForegroundRun.status == "completed",
                 or_(ForegroundRun.agent_checkpoint["result"]["completed"].as_boolean().is_(False),
                     ForegroundRun.agent_checkpoint["phase"].as_string() == "deliver"))),
    ).order_by(ForegroundRun.created_at.desc(), ForegroundRun.id.desc()).limit(20).all()
    # Keep the UI's chronological presentation while ensuring recent paused
    # checkpoints are not hidden by a long history of dismissed-in-place runs.
    runs.reverse()
    return {"runs": [_snapshot(run) for run in runs]}


@router.patch("/{run_id}")
def patch_run(run_id: str, req: PatchRun, db: Session = Depends(get_db)):
    # Match run admission's lock order: registry first, then the SQLite write
    # transaction. Otherwise an old browser patch can deadlock admission while
    # checking whether the server owns progress for this run.
    with run_store.coordinated():
        return _patch_run(run_id, req, db)


def _patch_run(run_id: str, req: PatchRun, db: Session):
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(422, "前台轮次 ID 无效")
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        run = db.get(ForegroundRun, run_id)
        if run is None:
            raise HTTPException(404, "前台轮次不存在")
        _require_available(db, run.conversation_id)
        changes = req.model_dump(exclude={"revision"}, exclude_unset=True)
        if run_id in run_store.active_runs():
            raise HTTPException(409, {
                "message": "此轮任务正在服务端执行，进度由服务端保存，请刷新状态。",
                "current": _snapshot(run),
            })
        if req.revision != run.revision:
            if all(getattr(run, field) == value for field, value in changes.items()):
                db.rollback()
                return _snapshot(run)
            raise HTTPException(409, {"message": "前台轮次已被更新", "current": _snapshot(run)})
        if run.status in ("completed", "dismissed"):
            raise HTTPException(409, {"message": "前台轮次已结束", "current": _snapshot(run)})
        if run.status != "running" and changes.get("status") == "running":
            raise HTTPException(409, {"message": "已中断轮次不能恢复为运行中", "current": _snapshot(run)})
        for field, value in changes.items():
            setattr(run, field, value)
        run.updated_at = utcnow()
        run.heartbeat_at = run.updated_at
        run.revision += 1
        db.commit()
        task_events.notify_tasks_changed()
        return _snapshot(run)
    except Exception:
        db.rollback()
        raise


@router.delete("/{run_id}")
def dismiss_run(run_id: str, db: Session = Depends(get_db)):
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(422, "前台轮次 ID 无效")
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        run = db.get(ForegroundRun, run_id)
        if run is None:
            raise HTTPException(404, "前台轮次不存在")
        if run.status == "running":
            raise HTTPException(409, "前台轮次仍在运行，不能移除")
        run.status = "dismissed"
        run.updated_at = utcnow()
        run.revision += 1
        db.commit()
        task_events.notify_tasks_changed()
        return {"dismissed": run_id}
    except Exception:
        db.rollback()
        raise
