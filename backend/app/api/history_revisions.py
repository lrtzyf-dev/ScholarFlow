from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from ..db import get_db
from ..history_models import HistoryRevision
from ..history_revisions import create_revision, public_revision, HistoryRevisionError
from ..source_store import SourceError
from ..conversation_store import ConversationDeleted, ConversationArchived
from .. import task_events

router = APIRouter(prefix="/api/conversations", tags=["history-revisions"])


class RevisionInput(BaseModel):
    record_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=100000)
    mode: Literal["answer_only", "new_task"] = "answer_only"
    request_id: str = Field(min_length=8, max_length=96)


@router.post("/{conversation_id}/revisions")
def create(conversation_id: str, req: RevisionInput, db: Session = Depends(get_db)):
    if not req.question.strip():
        raise HTTPException(422, "修订后的问题不能为空")
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        result = create_revision(db, conversation_id, req.record_id, req.question.strip(), req.mode, req.request_id)
        db.commit()
    except HistoryRevisionError as exc:
        db.rollback()
        raise HTTPException(exc.status, str(exc)) from None
    except (SourceError, ConversationDeleted, ConversationArchived) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    task_events.notify_tasks_changed()
    return result


@router.get("/{conversation_id}/revision-origin")
def origin(conversation_id: str, db: Session = Depends(get_db)):
    row = db.scalar(select(HistoryRevision).where(HistoryRevision.branch_conversation_id == conversation_id))
    return {"revision": public_revision(row) if row else None}
