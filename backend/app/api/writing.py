"""固定来源论文框架 API。"""
from __future__ import annotations

from contextlib import contextmanager
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db import get_db
from ..writing.framework import build_framework
from .. import research_runs, run_store, run_lifecycle
from ..model_profiles import ModelConfigurationError
from ..runtime_models import RuntimeRun
from ..goal_models import Goal
from ..source_runtime import conversation_scope
from .sources import _source_errors

router = APIRouter(prefix="/api/writing", tags=["writing"])


class ResearchFrameworkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^research_[a-f0-9]{32}$")
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    idea: str = Field(min_length=1, max_length=10000)
    max_sources: int = Field(default=8, ge=1, le=20, strict=True)


class ResearchResume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=1, strict=True)
    retry_unknown: bool = False


class ResearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")


class ResearchQualityRequest(ResearchScope):
    run_id: str = Field(pattern=r"^research_[a-f0-9]{32}$")
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    paper_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@contextmanager
def _research_errors(db):
    try:
        with _source_errors(db): yield
    except (run_lifecycle.RunConflict, ModelConfigurationError) as exc:
        db.rollback()
        raise HTTPException(getattr(exc, "status_code", 409), str(exc)) from None


@router.post("/framework/runs")
def create_framework_run(req: ResearchFrameworkRequest, db: Session = Depends(get_db)):
    with _research_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _, _, fresh = research_runs.create(db, kind="framework", run_id=req.run_id,
            conversation_id=req.conversation_id, inputs={"idea": req.idea, "max_sources": req.max_sources}, request_id=req.request_id)
        db.commit()
        if fresh: research_runs.start(req.run_id)
        return {**research_runs.public(db, req.run_id, req.conversation_id, expected_kind="framework"), "replayed": not fresh}


@router.get("/framework/runs")
def list_framework_runs(conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"),
                        limit: int = Query(default=20, ge=1, le=50), db: Session = Depends(get_db)):
    with _research_errors(db):
        conversation_scope(db, conversation_id, writing=False)
        rows = db.execute(select(RuntimeRun.id, RuntimeRun.status, RuntimeRun.outcome, RuntimeRun.updated_at,
            Goal.description).outerjoin(Goal, Goal.id == RuntimeRun.goal_id).where(RuntimeRun.kind == "framework",
            RuntimeRun.conversation_id == conversation_id).order_by(RuntimeRun.created_at.desc(), RuntimeRun.id.desc()).limit(limit))
        return {"items": [{"run_id": row.id, "status": row.status, "outcome": row.outcome,
            "title": (row.description or "论文框架")[:180], "updated_at": row.updated_at.isoformat()+"Z"} for row in rows]}


@router.get("/framework/runs/{run_id}")
def get_framework_run(run_id: str, conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"), db: Session = Depends(get_db)):
    with _research_errors(db): return research_runs.public(db, run_id, conversation_id, expected_kind="framework")


@router.post("/framework/runs/{run_id}/resume")
def resume_framework_run(run_id: str, req: ResearchResume, db: Session = Depends(get_db)):
    with _research_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        research_runs.public(db, run_id, req.conversation_id, expected_kind="framework")
        _, _, fresh = research_runs.resume(db, run_id, conversation_id=req.conversation_id,
            request_id=req.request_id, expected_revision=req.expected_revision, retry_unknown=req.retry_unknown)
        db.commit()
        if fresh: research_runs.start(run_id)
        return {**research_runs.public(db, run_id, req.conversation_id), "replayed": not fresh}


@router.post("/framework/runs/{run_id}/cancel")
def cancel_framework_run(run_id: str, req: ResearchScope, db: Session = Depends(get_db)):
    with _research_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        research_runs.public(db, run_id, req.conversation_id, expected_kind="framework")
        status = research_runs.cancel(db, run_id, req.conversation_id)
        db.commit()
        return {**research_runs.public(db, run_id, req.conversation_id), "cancel_status": status}


@router.post("/quality/runs")
def create_quality_run(req: ResearchQualityRequest, db: Session = Depends(get_db)):
    with _research_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _, _, fresh = research_runs.create(db, kind="quality", run_id=req.run_id,
            conversation_id=req.conversation_id, inputs={"paper_id": req.paper_id}, request_id=req.request_id)
        db.commit()
        if fresh: research_runs.start(req.run_id)
        return {**research_runs.public(db, req.run_id, req.conversation_id, expected_kind="quality"), "replayed": not fresh}


@router.get("/quality/runs")
def list_quality_runs(conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"),
                      limit: int = Query(default=20, ge=1, le=50), db: Session = Depends(get_db)):
    with _research_errors(db):
        conversation_scope(db, conversation_id, writing=False)
        rows = db.execute(select(RuntimeRun.id, RuntimeRun.status, RuntimeRun.outcome, RuntimeRun.updated_at,
            Goal.description).outerjoin(Goal, Goal.id == RuntimeRun.goal_id).where(RuntimeRun.kind == "quality",
            RuntimeRun.conversation_id == conversation_id).order_by(RuntimeRun.updated_at.desc(), RuntimeRun.id).limit(limit))
        return {"items": [{"run_id": r.id, "status": r.status, "outcome": r.outcome,
            "updated_at": r.updated_at.isoformat() + "Z", "title": r.description or "引用讨论分析"} for r in rows]}


@router.get("/quality/runs/{run_id}")
def get_quality_run(run_id: str, conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"), db: Session = Depends(get_db)):
    with _research_errors(db): return research_runs.public(db, run_id, conversation_id, expected_kind="quality")


@router.post("/quality/runs/{run_id}/resume")
def resume_quality_run(run_id: str, req: ResearchResume, db: Session = Depends(get_db)):
    with _research_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        research_runs.public(db, run_id, req.conversation_id, expected_kind="quality")
        _, _, fresh = research_runs.resume(db, run_id, conversation_id=req.conversation_id,
            request_id=req.request_id, expected_revision=req.expected_revision, retry_unknown=req.retry_unknown)
        db.commit()
        if fresh: research_runs.start(run_id)
        return {**research_runs.public(db, run_id, req.conversation_id), "replayed": not fresh}


@router.post("/quality/runs/{run_id}/cancel")
def cancel_quality_run(run_id: str, req: ResearchScope, db: Session = Depends(get_db)):
    with _research_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        research_runs.public(db, run_id, req.conversation_id, expected_kind="quality")
        status = research_runs.cancel(db, run_id, req.conversation_id)
        db.commit()
        return {**research_runs.public(db, run_id, req.conversation_id), "cancel_status": status}


class FrameworkRequest(BaseModel):
    idea: str
    max_sources: int = 8
    conversation_id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")


@router.post("/framework", deprecated=True)
def framework_api(req: FrameworkRequest, db: Session = Depends(get_db)):
    idea = req.idea.strip()
    if not idea:
        raise HTTPException(400, "研究想法不能为空")
    if not 1 <= req.max_sources <= 20:
        raise HTTPException(400, "文献数量须在 1–20 之间")
    try:
        with _source_errors(db):
            return build_framework(db, idea, max_sources=req.max_sources, conversation_id=req.conversation_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
