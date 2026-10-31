"""一键审我的稿件端点。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..agent.reviewer import review_draft

router = APIRouter(prefix="/api/review", tags=["review"])


class ReviewRequest(BaseModel):
    draft: str


@router.post("")
def review(req: ReviewRequest, db: Session = Depends(get_db)):
    if not req.draft.strip():
        return {"overall": "稿件为空", "issues": [], "strengths": [],
                "verdict": "undetermined", "mode": "mechanical"}
    return review_draft(db, req.draft)
