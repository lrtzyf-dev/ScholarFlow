"""arXiv 追踪与推荐 API：订阅管理、立即刷新、推荐与点赞踩反馈。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Recommendation, Subscription
from ..tracking.scheduler import TrackingScheduler
from ..tracking.scoring import feedback_adjust
from ..tracking.service import run_subscription

router = APIRouter(prefix="/api/tracking", tags=["tracking"])

MIN_INTERVAL, MAX_INTERVAL = 1, 168


class SubscriptionCreate(BaseModel):
    topic: str
    interval_hours: int = 24


class FeedbackRequest(BaseModel):
    feedback: str  # up / down


def _rec_out(rec: Recommendation) -> dict:
    return {
        "id": rec.id,
        "arxiv_id": rec.arxiv_id,
        "title": rec.title,
        "authors": rec.authors,
        "abstract": rec.abstract,
        "published_at": rec.published_at,
        "url": rec.url,
        "github_url": rec.github_url,
        "score": rec.score,
        "display_score": feedback_adjust(rec.score, rec.feedback),
        "reasons": rec.reasons,
        "feedback": rec.feedback,
    }


def _sub_out(sub: Subscription) -> dict:
    recs = sorted(
        sub.recommendations,
        key=lambda r: feedback_adjust(r.score, r.feedback),
        reverse=True,
    )
    return {
        "id": sub.id,
        "topic": sub.topic,
        "interval_hours": sub.interval_hours,
        "active": bool(sub.active),
        "last_run_at": sub.last_run_at.isoformat() if sub.last_run_at else None,
        "last_status": sub.last_status,
        "recommendations": [_rec_out(r) for r in recs],
    }


@router.post("/subscriptions")
def create_subscription(req: SubscriptionCreate, db: Session = Depends(get_db)):
    # 确定性 gate：规则校验，不调模型
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(400, "订阅主题不能为空")
    if not MIN_INTERVAL <= req.interval_hours <= MAX_INTERVAL:
        raise HTTPException(400, f"刷新间隔须在 {MIN_INTERVAL}–{MAX_INTERVAL} 小时之间")
    sub = Subscription(topic=topic, interval_hours=req.interval_hours, active=1)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return _sub_out(sub)


@router.get("/subscriptions")
def list_subscriptions(db: Session = Depends(get_db)):
    subs = db.query(Subscription).order_by(Subscription.created_at.desc()).all()
    return [_sub_out(s) for s in subs]


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: str, db: Session = Depends(get_db)):
    sub = db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404, "订阅不存在")
    db.delete(sub)
    db.commit()
    return {"deleted": sub_id}


@router.post("/subscriptions/{sub_id}/toggle")
def toggle_subscription(sub_id: str, db: Session = Depends(get_db)):
    sub = db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404, "订阅不存在")
    sub.active = 0 if sub.active else 1
    db.commit()
    return _sub_out(sub)


@router.post("/subscriptions/{sub_id}/refresh")
def refresh_subscription(sub_id: str, db: Session = Depends(get_db)):
    sub = db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404, "订阅不存在")
    result = run_subscription(db, sub)
    return {"result": result, "subscription": _sub_out(sub)}


@router.post("/recommendations/{rec_id}/feedback")
def give_feedback(rec_id: str, req: FeedbackRequest, db: Session = Depends(get_db)):
    if req.feedback not in ("up", "down"):
        raise HTTPException(400, "反馈类型须为 up 或 down")
    rec = db.get(Recommendation, rec_id)
    if not rec:
        raise HTTPException(404, "推荐不存在")
    rec.feedback = req.feedback
    db.commit()
    return _rec_out(rec)
