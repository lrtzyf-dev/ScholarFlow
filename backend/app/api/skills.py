"""技能库：列出（含层级/禁用状态）、查看正文、禁用/启用、新建/删除用户技能。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..skill_store import (
    create_user_skill,
    delete_user_skill,
    list_skills,
    load_skill,
    set_disabled,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
def get_skills() -> dict:
    return {"skills": list_skills()}


@router.get("/{name}")
def get_skill(name: str) -> dict:
    data = load_skill(name, explicit=True)
    if "error" in data:
        raise HTTPException(status_code=404, detail=data["error"])
    return data


class DisabledIn(BaseModel):
    name: str
    disabled: bool


@router.post("/disabled")
def toggle_disabled(payload: DisabledIn) -> dict:
    return set_disabled(payload.name, payload.disabled)


class CreateSkillIn(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    content: str = ""


@router.post("")
def create_skill(payload: CreateSkillIn) -> dict:
    try:
        return create_user_skill(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{name}")
def remove_skill(name: str) -> dict:
    try:
        delete_user_skill(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": name}
