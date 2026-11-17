"""Conversation-scoped, CAS-protected user input inbox for foreground and agent tasks."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..db import get_db
from .. import run_inputs

router = APIRouter(prefix="/api/chat/runs", tags=["run-inputs"])


class SubmitRunInput(BaseModel):
    input_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    conversation_id: str = Field(min_length=1, max_length=64)
    kind: Literal["steer", "next_turn"]
    content: str = Field(min_length=1, max_length=200_000)
    expected_revision: int = Field(ge=0)
    workspace_revision: int = Field(ge=0)
    # Omitted means inherit the run's frozen source binding. An explicit []
    # remains the deliberate way to clear attachments for this input.
    source_revision_ids: list[str] | None = Field(default=None, max_length=16)

    @field_validator("source_revision_ids")
    @classmethod
    def fixed_sources(cls, value):
        from ..source_runtime import revision_ids
        return None if value is None else revision_ids(value)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("输入不能为空或包含空字符")
        return value


@router.get("/{run_id}/inputs")
def list_run_inputs(run_id: str, conversation_id: str = Query(min_length=1, max_length=64),
                    db: Session = Depends(get_db)):
    try:
        return run_inputs.list_inputs(db, run_id, conversation_id)
    except run_inputs.RunInputError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/inputs")
def list_conversation_run_inputs(conversation_id: str = Query(min_length=1, max_length=64),
                                 db: Session = Depends(get_db)):
    try:
        return run_inputs.list_conversation_inputs(db, conversation_id)
    except run_inputs.RunInputError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/{run_id}/inputs")
def submit_run_input(run_id: str, req: SubmitRunInput, db: Session = Depends(get_db)):
    try:
        # Serialize reception with the executor's final answer transaction.
        # Re-read run.status only after owning the write reservation: otherwise
        # a cached "running" read could queue input after completion committed.
        db.execute(text("BEGIN IMMEDIATE"))
        result = run_inputs.submit_input(db, run_id, **req.model_dump())
        db.commit()
    except run_inputs.RunInputError as exc:
        db.rollback()
        raise HTTPException(exc.status, str(exc)) from exc
    run_inputs.publish_input_change(result)
    from ..next_turn_dispatch import wake
    wake()
    return result


@router.delete("/{run_id}/inputs/{input_id}")
def cancel_run_input(run_id: str, input_id: str,
                     conversation_id: str = Query(min_length=1, max_length=64),
                     expected_revision: int = Query(ge=0), db: Session = Depends(get_db)):
    try:
        result = run_inputs.cancel_input(db, run_id, input_id, conversation_id, expected_revision)
        db.commit()
    except run_inputs.RunInputError as exc:
        db.rollback()
        raise HTTPException(exc.status, str(exc)) from exc
    run_inputs.publish_input_change(result)
    from ..next_turn_dispatch import wake
    wake()
    return result
