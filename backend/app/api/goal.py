"""持久目标：查看/设置当前目标，暂停、恢复、完成、取消，列出历史。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import goal_store

router = APIRouter(prefix="/api/goal", tags=["goal"])


def _payload() -> dict:
    return {
        "current": goal_store.get_current_goal(),
        "goals": list(reversed(goal_store.load_goals())),
    }


@router.get("")
def get_goal() -> dict:
    return _payload()


class GoalIn(BaseModel):
    description: str


@router.post("")
def set_goal(body: GoalIn) -> dict:
    try:
        goal_store.set_goal(body.description)
    except goal_store.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()


@router.post("/pause")
def pause() -> dict:
    try:
        goal_store.pause_goal()
    except goal_store.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()


@router.post("/resume")
def resume() -> dict:
    try:
        goal_store.resume_goal()
    except goal_store.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()


@router.post("/complete")
def complete() -> dict:
    try:
        goal_store.complete_goal()
    except goal_store.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()


@router.post("/cancel")
def cancel() -> dict:
    try:
        goal_store.cancel_goal()
    except goal_store.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()
