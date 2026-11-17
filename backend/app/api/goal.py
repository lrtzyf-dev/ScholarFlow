"""持久目标：查看/设置当前目标，暂停、恢复、完成、取消，列出历史。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import goal_store
from ..db import get_db
from .. import goal_ledger
from .. import goal_contracts

router = APIRouter(prefix="/api/goal", tags=["goal"])


def _payload() -> dict:
    try:
        return {"current": goal_store.get_current_goal(), "goals": list(reversed(goal_store.load_goals()))}
    except goal_store.GoalError as exc:
        _fail(exc)


def _fail(exc):
    code = 409 if isinstance(exc, goal_store.GoalConflict) else 503 if isinstance(exc, goal_store.GoalMigrationError) else 400
    raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get("")
def get_goal() -> dict:
    return _payload()


class GoalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    source_message_id: str | None = None


@router.post("")
def set_goal(body: GoalIn) -> dict:
    try:
        goal_store.set_goal(body.description, source_message_id=body.source_message_id)
    except goal_store.GoalError as exc:
        _fail(exc)
    return _payload()


class GoalActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int | None = Field(default=None, strict=True, ge=1)
    source_message_id: str | None = None


@router.post("/pause")
def pause(body: GoalActionIn | None = None) -> dict:
    try:
        goal_store.pause_goal(**(body.model_dump() if body else {}))
    except goal_store.GoalError as exc:
        _fail(exc)
    return _payload()


@router.post("/resume")
def resume(body: GoalActionIn | None = None) -> dict:
    try:
        goal_store.resume_goal(**(body.model_dump() if body else {}))
    except goal_store.GoalError as exc:
        _fail(exc)
    return _payload()


@router.post("/complete")
def complete(body: GoalActionIn | None = None) -> dict:
    try:
        goal_store.complete_goal(**(body.model_dump() if body else {}))
    except goal_store.GoalError as exc:
        _fail(exc)
    return _payload()


@router.post("/cancel")
def cancel(body: GoalActionIn | None = None) -> dict:
    try:
        goal_store.cancel_goal(**(body.model_dump() if body else {}))
    except goal_store.GoalError as exc:
        _fail(exc)
    return _payload()


class GoalRevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)
    source_message_id: str = Field(min_length=1, max_length=160)
    action: str
    requirements: list[dict] | None = None
    requirement_ids: list[str] | None = None
    reason: str = ""


@router.get("/{goal_id}")
def scoped_goal(goal_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return goal_ledger.snapshot_goal(db, goal_id)
    except goal_store.GoalError as exc:
        _fail(exc)


@router.post("/{goal_id}/revisions")
def revise(goal_id: str, body: GoalRevisionIn, db: Session = Depends(get_db)) -> dict:
    try:
        result = goal_ledger.revise_goal(db, goal_id, source="user", **body.model_dump())
        db.commit()
        return result
    except goal_store.GoalError as exc:
        db.rollback()
        _fail(exc)


class CriteriaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    expected_revision: int = Field(strict=True, ge=1)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,96}$")
    action: str
    title: str | None = Field(default=None, max_length=4000)
    contract: dict | None = None
    requirement_id: str | None = None
    candidate_id: str | None = None
    reason: str = Field(default="", max_length=4000)


def _own_goal(db, goal_id, conversation_id, *, write=False):
    from ..model_profiles import require_conversation, ModelConfigurationError
    try:
        require_conversation(db, conversation_id, write=write)
    except ModelConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    from ..goal_models import Goal
    goal = db.get(Goal, goal_id, populate_existing=True)
    if goal is None or goal.conversation_id != conversation_id:
        raise HTTPException(404, "该会话中没有此任务目标")
    if write:
        from ..runtime_models import RuntimeRun
        run = db.get(RuntimeRun, goal.run_id) if goal.run_id else None
        if run is None or run.kind not in ("foreground", "background"):
            raise HTTPException(409, "请在此科研任务页调整输入；当前要求编辑适用于会话和后台 Agent 任务")


@router.get("/{goal_id}/requirements")
def task_requirements(goal_id: str, conversation_id: str, db: Session = Depends(get_db)):
    try:
        _own_goal(db, goal_id, conversation_id)
        return goal_ledger.snapshot_goal(db, goal_id)
    except goal_store.GoalError as exc:
        _fail(exc)


@router.post("/{goal_id}/requirements")
def change_requirements(goal_id: str, body: CriteriaInput, db: Session = Depends(get_db)):
    from sqlalchemy import text
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        _own_goal(db, goal_id, body.conversation_id, write=True)
        result = goal_contracts.user_change(db, goal_id, **body.model_dump(exclude={"conversation_id"}))
        db.commit()
        return result
    except goal_store.GoalError as exc:
        db.rollback()
        _fail(exc)
