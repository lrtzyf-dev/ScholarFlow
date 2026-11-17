"""Explicit per-conversation memory edits; never mutate original messages."""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from .. import memory_governance as memory

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("/{conversation_id}/items")
def list_items(conversation_id: str, offset: int = Query(0, ge=0),
               limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
    return memory.items(db, conversation_id, offset=offset, limit=limit)


class MemoryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    action: Literal["edit", "include", "exclude", "delete"]
    text: str | None = Field(default=None, max_length=12000)


@router.post("/{conversation_id}/items/{record_id}")
def edit_item(conversation_id: str, record_id: str, req: MemoryEdit, db: Session = Depends(get_db)):
    return memory.mutate(db, conversation_id, record_id, expected_revision=req.expected_revision,
                         action=req.action, replacement=req.text)
