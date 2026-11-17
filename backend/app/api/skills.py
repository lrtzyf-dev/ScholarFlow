"""技能库：列出（含层级/禁用状态）、查看正文、禁用/启用、新建/删除用户技能。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Project

from ..skill_store import (
    create_user_skill,
    delete_user_skill,
    list_skills,
    load_skill,
    set_disabled,
    inspect_skill, trust_skill, legacy_skills, migrate_legacy, update_skill,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


def scope(project_id: str | None = None, db: Session = Depends(get_db)):
    if project_id and not db.get(Project, project_id):
        raise HTTPException(404, '项目不存在')
    return project_id


@router.get("")
def get_skills(project_id=Depends(scope)) -> dict:
    return {"skills": list_skills(project_id=project_id), 'legacy': legacy_skills()}


class ReviewIn(BaseModel):
    revision: str


@router.post('/legacy/{name}/migrate')
def migrate(name: str, payload: ReviewIn, project_id=Depends(scope)):
    if not project_id:
        raise HTTPException(400, '迁移必须明确选择所属项目')
    try:
        return migrate_legacy(name, payload.revision, project_id=project_id)
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(409, str(exc))


@router.get("/{name}")
def get_skill(name: str, project_id=Depends(scope)) -> dict:
    try:
        return inspect_skill(name, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post('/{name}/trust')
def trust(name: str, payload: ReviewIn, project_id=Depends(scope)):
    try:
        return trust_skill(name, payload.revision, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


class DisabledIn(BaseModel):
    name: str
    disabled: bool


@router.post("/disabled")
def toggle_disabled(payload: DisabledIn, project_id=Depends(scope)) -> dict:
    try:
        return set_disabled(payload.name, payload.disabled, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class CreateSkillIn(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    content: str = ""


class UpdateSkillIn(BaseModel):
    expected_revision: str
    title: str = ''
    description: str = ''
    content: str


@router.put('/{name}')
def update(name: str, payload: UpdateSkillIn, project_id=Depends(scope)):
    try:
        return update_skill(name, payload.model_dump(), project_id=project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("")
def create_skill(payload: CreateSkillIn, project_id=Depends(scope)) -> dict:
    try:
        return create_user_skill(payload.model_dump(), project_id=project_id)
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{name}")
def remove_skill(name: str, project_id=Depends(scope)) -> dict:
    try:
        delete_user_skill(name, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": name}
