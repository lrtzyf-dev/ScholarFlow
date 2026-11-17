"""Local data retention review, content-free receipts, and capacity policy."""
from fastapi import APIRouter,Depends,Query
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import storage_policy as service

router=APIRouter(prefix="/api/storage",tags=["storage"])


class Policy(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_revision:StrictInt=Field(ge=0)
    backup_reminder_days:StrictInt=Field(ge=1,le=3650)
    archived_review_days:StrictInt=Field(ge=1,le=3650)
    backup_review_days:StrictInt=Field(ge=1,le=3650)
    capacity_warning_bytes:StrictInt=Field(ge=1024**2,le=1024**5)
    cleanup_receipt_review_days:StrictInt=Field(ge=1,le=3650)


@router.get("")
def status(db:Session=Depends(get_db)):return service.status(db)


@router.put("/policy")
def update(req:Policy,db:Session=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    result=service.update_policy(db,req.model_dump(exclude={"expected_revision"}),req.expected_revision)
    db.commit();return result


@router.get("/cleanup-receipts")
def receipts(cursor:str|None=Query(default=None,max_length=512),db:Session=Depends(get_db)):
    return service.receipts(db,cursor)
