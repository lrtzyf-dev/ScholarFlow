"""Durable foreground answer checkpoints and restart recovery."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from ..db import get_db
from .. import approval_store, run_store, task_events
from ..models import Conversation, DeletedConversation, ForegroundRun
from .desktop_state import DraftImage, MAX_TOTAL_IMAGE_BYTES
from ..execution_mode import ExecutionOptions, PlanConflict, claim_reviewed_plan
from .. import workspace

router = APIRouter(prefix="/api/foreground-runs", tags=["foreground-runs"])
RUN_ID = re.compile(r"^chat_[A-Za-z0-9_-]{8,64}$")
HEARTBEAT_TIMEOUT = timedelta(seconds=20)
MAX_ANSWER_CHARS = 300_000


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
    status: Literal["running", "interrupted", "cancelled", "failed"] | None = None
    error: str | None = Field(default=None, max_length=2000)


def _snapshot(run: ForegroundRun):
    return {
        "id": run.id, "conversation_id": run.conversation_id, "revision": run.revision,
        "question": run.question, "paper_id": run.paper_id, "skills": run.skills or [],
        "execution_mode": run.execution_mode, "reviewed_plan_id": run.reviewed_plan_id,
        "project_id": (run.workspace or {}).get("project_id"),
        "workspace_revision": (run.workspace or {}).get("workspace_revision", 0),
        "file_paths": [f["path"] for f in (run.workspace or {}).get("files", [])],
        "images": run.images or [], "partial_answer": run.partial_answer or "",
        "phase": run.phase, "status": run.status, "error": run.error or "",
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        "approvals": approval_store.list_for_run(run.id) if run.status == "running" else [],
    }


def _require_available(db: Session, conversation_id: str):
    if db.get(DeletedConversation, conversation_id):
        raise HTTPException(410, "该会话已删除，不能恢复前台轮次")


@router.post("")
def create_run(req: CreateRun, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        _require_available(db, req.conversation_id)
        conv = db.get(Conversation, req.conversation_id)
        if conv is not None and conv.archived_at is not None:
            raise HTTPException(409, "该会话已归档，请先恢复会话后继续")
        scope = workspace.capture(db, req.conversation_id, req.project_id, req.file_paths, req.reviewed_plan_id, req.workspace_revision)
        try:
            claim_reviewed_plan(db, req.reviewed_plan_id, req.conversation_id, req.id, req.retry_plan_run_id)
        except PlanConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        now = datetime.utcnow()
        result = db.execute(insert(ForegroundRun).values(
            id=req.id, conversation_id=req.conversation_id, revision=1,
            question=req.question, paper_id=req.paper_id, skills=req.skills,
            execution_mode=req.execution_mode, reviewed_plan_id=req.reviewed_plan_id,
            workspace=scope,
            images=[image.model_dump() for image in req.images], partial_answer="",
            phase="setup", status="running", error="", created_at=now, updated_at=now,
            heartbeat_at=now,
        ).on_conflict_do_nothing(index_elements=["id"]))
        if result.rowcount != 1:
            raise HTTPException(409, "前台轮次 ID 已使用")
        db.commit()
        task_events.notify_tasks_changed()
        return {"id": req.id, "revision": 1, "status": "running", "updated_at": now.isoformat()}
    except Exception:
        db.rollback()
        raise


@router.get("")
def list_runs(conversation_id: str = Query(min_length=1, max_length=64, pattern=r"^[\w-]+$"),
              db: Session = Depends(get_db)):
    _require_available(db, conversation_id)
    now = datetime.utcnow()
    cutoff = now - HEARTBEAT_TIMEOUT
    stale = db.query(ForegroundRun).filter(
        ForegroundRun.conversation_id == conversation_id,
        ForegroundRun.status == "running", ForegroundRun.heartbeat_at < cutoff,
    ).all()
    for run in stale:
        if run.id in run_store.active_runs():
            continue
        run.status = "interrupted"
        run.error = "前台连接已中断，保留了最后保存的部分回答。"
        run.updated_at = now
        run.heartbeat_at = now
        run.revision += 1
    if stale:
        db.commit()
        task_events.notify_tasks_changed()
    runs = db.query(ForegroundRun).filter(
        ForegroundRun.conversation_id == conversation_id,
        ForegroundRun.status.in_(("running", "interrupted", "cancelled", "failed")),
    ).order_by(ForegroundRun.created_at.asc()).limit(20).all()
    return {"runs": [_snapshot(run) for run in runs]}


@router.patch("/{run_id}")
def patch_run(run_id: str, req: PatchRun, db: Session = Depends(get_db)):
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(422, "前台轮次 ID 无效")
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        run = db.get(ForegroundRun, run_id)
        if run is None:
            raise HTTPException(404, "前台轮次不存在")
        _require_available(db, run.conversation_id)
        changes = req.model_dump(exclude={"revision"}, exclude_unset=True)
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
        run.updated_at = datetime.utcnow()
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
        run.updated_at = datetime.utcnow()
        run.revision += 1
        db.commit()
        task_events.notify_tasks_changed()
        return {"dismissed": run_id}
    except Exception:
        db.rollback()
        raise
