from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import product_journey as journey
from ..source_store import SourceError

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.get("")
def state(conversation_id: str | None = Query(default=None, max_length=64), db: Session = Depends(get_db)):
    return journey.snapshot(db, conversation_id)


class Transition(BaseModel):
    action: Literal["start", "skip", "reopen", "complete", "demo_seen"]
    expected_revision: int = Field(ge=0)
    conversation_id: str | None = Field(default=None, max_length=64)


@router.post("/state")
def transition(req: Transition, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        journey.transition(db, req.action, req.expected_revision, req.conversation_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    return journey.snapshot(db, req.conversation_id)


class CitationVisit(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)
    locator_id: str = Field(min_length=1, max_length=64)


@router.post("/citation-visit")
def citation_visit(req: CitationVisit, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        observed = journey.citation_opened(db, req.conversation_id, req.revision_id, req.locator_id)
        db.commit()
    except (ValueError, SourceError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    return {"observed": observed, "meaning": "source_view_opened_not_fact_verified"}
