from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import run_center as service
from ..models import DesktopState
from ..timeutil import utcnow

router = APIRouter(prefix="/api/run-center", tags=["run-center"])


@router.get("")
def list_runs(view: Literal["active", "attention", "history", "all"] = "active",
              status: str | None = Query(default=None, max_length=32), limit: int = Query(default=40, ge=1, le=100),
              cursor: str | None = Query(default=None, max_length=2048), db: Session = Depends(get_db)):
    if status and status not in (*service.ACTIVE, "completed", "ended", "cancelled"):
        raise HTTPException(422, "运行筛选状态无效")
    try:
        return service.list_runs(db, view=view, status=status, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


class EventReceipt(BaseModel):
    item_id: str = Field(max_length=160)
    event_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReadInput(BaseModel):
    events: list[EventReceipt] = Field(max_length=100)


@router.post("/read")
def read(req: ReadInput, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    result = service.acknowledge(db, [event.model_dump() for event in req.events])
    db.commit()
    return {"acknowledged": result}


class Preferences(BaseModel):
    muted: bool


class NotificationInput(BaseModel):
    item_id: str = Field(min_length=1, max_length=160)


@router.post("/notification")
def claim_notification(req: NotificationInput, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    item = service.claim_notification(db, req.item_id)
    db.commit()
    return {"item": item}


@router.put("/preferences")
def preferences(req: Preferences, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    row = db.get(DesktopState, "run-center:preferences")
    if row is None:
        row = DesktopState(key="run-center:preferences", value=req.model_dump())
        db.add(row)
    else:
        row.value, row.revision, row.updated_at = req.model_dump(), row.revision + 1, utcnow()
    db.commit()
    return req.model_dump()
