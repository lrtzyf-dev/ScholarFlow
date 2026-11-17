"""学术翻译 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy import select, text

from ..db import get_db
from ..translate.sources import fixed_translation_source
from ..translate.parallel import translate_parallel
from ..translate.translator import translate_text
from .. import research_runs, run_store
from ..runtime_models import RuntimeRun
from ..goal_models import Goal
from ..source_runtime import conversation_scope
from .writing import ResearchResume, ResearchScope, _research_errors

router = APIRouter(prefix="/api", tags=["translate"])


class TranslationSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    locator_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1_000_000)
    source: str = Field(default="Chinese", pattern=r"^[A-Za-z][A-Za-z -]{0,63}$")
    target: str = Field(default="English", pattern=r"^[A-Za-z][A-Za-z -]{0,63}$")
    conversation_id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    source_ref: TranslationSource | None = None

    @model_validator(mode="after")
    def source_requires_scope(self):
        if self.source_ref is not None and self.conversation_id is None:
            raise ValueError("固定来源翻译须提供原会话范围")
        if not self.text.strip():
            raise ValueError("请输入需要翻译的原文")
        return self


def _evidence(db, req):
    if req.source_ref is None:
        return []
    return [fixed_translation_source(db, conversation_id=req.conversation_id,
        revision_id=req.source_ref.revision_id, locator_id=req.source_ref.locator_id, text=req.text)]


def _translate(req, db, *, parallel=False):
    evidence = _evidence(db, req)
    # Close the read transaction before a paid request. Authorization and fixed
    # locator integrity are rechecked after the response, before disclosure.
    db.rollback()
    result = (translate_parallel(req.text, source=req.source, target=req.target) if parallel else
              translate_text(req.text, req.source, req.target))
    evidence = _evidence(db, req) if evidence else []
    return {**result, "source_evidence": evidence,
        "provenance_status": "fixed_source" if evidence else "user_text",
        "semantic_verification": "not_assessed", "model_selection": "global_default"}


class TranslationRunRequest(TranslateRequest):
    run_id: str = Field(pattern=r"^research_[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,96}$")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    display_mode: str = Field(default="whole", pattern=r"^(whole|parallel)$")


@router.post("/translation/runs")
def create_translation_run(req: TranslationRunRequest, db: Session = Depends(get_db)):
    with _research_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _, _, fresh=research_runs.create(db,kind="translation",run_id=req.run_id,conversation_id=req.conversation_id,
            inputs=req.model_dump(exclude={"run_id","request_id","conversation_id"}),request_id=req.request_id)
        db.commit()
        if fresh:research_runs.start(req.run_id)
        return {**research_runs.public(db,req.run_id,req.conversation_id),"replayed":not fresh}


@router.get("/translation/runs")
def list_translation_runs(conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"),
    limit: int = Query(default=20,ge=1,le=50),db: Session = Depends(get_db)):
    with _research_errors(db):
        conversation_scope(db,conversation_id,writing=False)
        rows=db.execute(select(RuntimeRun.id,RuntimeRun.status,RuntimeRun.outcome,RuntimeRun.updated_at,Goal.description)
            .outerjoin(Goal,Goal.id==RuntimeRun.goal_id).where(RuntimeRun.kind=="translation",RuntimeRun.conversation_id==conversation_id)
            .order_by(RuntimeRun.created_at.desc(),RuntimeRun.id.desc()).limit(limit))
        return {"items":[{"run_id":r.id,"status":r.status,"outcome":r.outcome,"title":(r.description or "学术翻译")[:180],
            "updated_at":r.updated_at.isoformat()+"Z"} for r in rows]}


@router.get("/translation/runs/{run_id}")
def get_translation_run(run_id: str,conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"),db: Session = Depends(get_db)):
    with _research_errors(db):
        return research_runs.public(db,run_id,conversation_id,expected_kind="translation")


@router.post("/translation/runs/{run_id}/resume")
def resume_translation_run(run_id: str,req: ResearchResume,db: Session = Depends(get_db)):
    with _research_errors(db),run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        get_translation_run(run_id,req.conversation_id,db)
        _,_,fresh=research_runs.resume(db,run_id,conversation_id=req.conversation_id,request_id=req.request_id,
            expected_revision=req.expected_revision,retry_unknown=req.retry_unknown)
        db.commit()
        if fresh:research_runs.start(run_id)
        return {**research_runs.public(db,run_id,req.conversation_id),"replayed":not fresh}


@router.post("/translation/runs/{run_id}/cancel")
def cancel_translation_run(run_id: str,req: ResearchScope,db: Session = Depends(get_db)):
    with _research_errors(db),run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        get_translation_run(run_id,req.conversation_id,db)
        status=research_runs.cancel(db,run_id,req.conversation_id);db.commit()
        return {**research_runs.public(db,run_id,req.conversation_id),"cancel_status":status}


@router.post("/translate", deprecated=True)
def translate_api(req: TranslateRequest, db: Session = Depends(get_db)):
    return _translate(req, db)


@router.post("/translate-parallel", deprecated=True)
def translate_parallel_api(req: TranslateRequest, db: Session = Depends(get_db)):
    return _translate(req, db, parallel=True)
