"""产物自检端点。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..agent.final_check import final_check

router = APIRouter(prefix="/api/final-check", tags=["final-check"])


class FinalCheckRequest(BaseModel):
    answer: str
    evidence: list[dict]


@router.post("")
def check(req: FinalCheckRequest, db: Session = Depends(get_db)):
    return final_check(db, req.answer, req.evidence)
