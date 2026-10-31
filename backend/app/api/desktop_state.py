"""Port-independent desktop drafts and reading state, with optimistic concurrency."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DeletedConversation, DesktopState
from ..project_search import SearchOptions

router = APIRouter(prefix="/api/desktop-state", tags=["desktop-state"])
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024


class Selection(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=64, pattern=r"^[\w-]+$")


class SidebarState(BaseModel):
    sort: Literal["updated_at", "created_at"] = "updated_at"


class SidebarWrite(BaseModel):
    revision: int = Field(ge=0)
    value: SidebarState


class GitDraft(BaseModel):
    message: str = Field(default='', max_length=10000)
    pending: dict | None = None

    @field_validator('pending')
    @classmethod
    def valid_pending(cls, value):
        if value is None:
            return None
        from ..git_operations import Request
        return Request.model_validate(value).model_dump()


class GitDraftWrite(BaseModel):
    revision: int = Field(ge=0)
    value: GitDraft


class WorktreeDraft(BaseModel):
    name: str = Field(default='', max_length=120)
    ref: str = Field(default='HEAD', max_length=256)
    branch: str = Field(default='', max_length=256)
    pending: dict | None = None

    @field_validator('pending')
    @classmethod
    def valid_pending(cls, value):
        from ..worktrees import Create
        return Create.model_validate(value).model_dump() if value is not None else None


class WorktreeWrite(BaseModel):
    revision: int = Field(ge=0)
    value: WorktreeDraft


class SearchWrite(BaseModel):
    revision: int = Field(ge=0)
    value: SearchOptions


class DraftImage(BaseModel):
    media_type: str = Field(pattern=r"^image/(png|jpeg|webp|gif)$")
    data: str = Field(max_length=((MAX_IMAGE_BYTES + 2) // 3) * 4)

    @field_validator("data")
    @classmethod
    def valid_base64(cls, value):
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片编码无效")
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片不能超过 5 MB")
        return value


class ReadingPosition(BaseModel):
    following: bool = True
    top: float = Field(default=0, ge=0, le=1e9, allow_inf_nan=False)
    anchor_id: str | None = Field(default=None, max_length=64)
    offset: float = Field(default=0, ge=-1e9, le=1e9, allow_inf_nan=False)


class EditorDraft(BaseModel):
    project_id: str = Field(max_length=64)
    path: str = Field(max_length=1024)
    operation: Literal['update', 'create'] = 'update'
    sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    content: str = Field(default='', max_length=262144)


class ConversationState(BaseModel):
    workspace_revision: int = Field(default=0, ge=0)
    editor: EditorDraft | None = None
    file_paths: list[str] = Field(default_factory=list, max_length=8)
    text: str = Field(default="", max_length=200_000)
    execution_mode: Literal["execute", "plan"] = "execute"
    paper_id: str | None = Field(default=None, max_length=128)
    skills: list[str] = Field(default_factory=list, max_length=32)
    images: list[DraftImage] = Field(default_factory=list, max_length=4)
    reading: ReadingPosition = Field(default_factory=ReadingPosition)

    @field_validator("file_paths")
    @classmethod
    def valid_paths(cls, value):
        if any(len(path) > 1024 for path in value):
            raise ValueError("文件相对路径过长")
        return list(dict.fromkeys(value))

    @field_validator("skills")
    @classmethod
    def valid_skills(cls, value):
        if any(len(name) > 128 for name in value):
            raise ValueError("技能名过长")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def bounded_images(self):
        if sum(len(base64.b64decode(image.data)) for image in self.images) > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("图片合计不能超过 8 MB")
        return self


class SelectionWrite(BaseModel):
    revision: int = Field(ge=0)
    value: Selection


class ConversationWrite(BaseModel):
    revision: int = Field(ge=0)
    value: ConversationState


def _id(value: str):
    import re
    if not re.fullmatch(r"[\w-]{1,64}", value):
        raise HTTPException(422, "会话 ID 无效")


def _available(db: Session, cid: str | None):
    if cid and db.get(DeletedConversation, cid):
        raise HTTPException(410, "该会话已删除，不能恢复旧草稿")


def _read(db: Session, key: str, default: BaseModel):
    row = db.get(DesktopState, key)
    return {"revision": row.revision, "value": row.value} if row else {
        "revision": 0, "value": default.model_dump(),
    }


def _write(db: Session, key: str, revision: int, value: BaseModel, cid: str | None):
    # Serialize the tombstone check and write across desktop processes as well.
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        _available(db, cid)
        payload = value.model_dump()
        if isinstance(value, ConversationState):
            from ..workspace import binding_revision
            if value.workspace_revision != binding_revision(db, cid):
                current_default = ConversationState(workspace_revision=binding_revision(db, cid))
                raise HTTPException(409, {"message": "会话工作目录已切换，请核对草稿；原文件引用不会自动带入新目录", **_read(db, key, current_default)})
        now = datetime.utcnow()
        if revision == 0:
            result = db.execute(insert(DesktopState).values(
                key=key, revision=1, value=payload, updated_at=now,
            ).on_conflict_do_nothing(index_elements=["key"]))
        else:
            result = db.execute(update(DesktopState).where(
                DesktopState.key == key, DesktopState.revision == revision,
            ).values(revision=revision + 1, value=payload, updated_at=now))
        if result.rowcount != 1:
            raise HTTPException(409, {"message": "另一个窗口已更新此草稿", **_read(db, key, value)})
        db.commit()
        return {"revision": revision + 1, "value": payload}
    except Exception:
        db.rollback()
        raise


@router.get("/selection")
def get_selection(db: Session = Depends(get_db)):
    return _read(db, "selection", Selection())


@router.put("/selection")
def put_selection(req: SelectionWrite, db: Session = Depends(get_db)):
    return _write(db, "selection", req.revision, req.value, req.value.conversation_id)


@router.get("/sidebar")
def get_sidebar(db: Session = Depends(get_db)):
    return _read(db, "sidebar", SidebarState())


@router.put("/sidebar")
def put_sidebar(req: SidebarWrite, db: Session = Depends(get_db)):
    return _write(db, "sidebar", req.revision, req.value, None)


@router.get("/conversations/{conversation_id}")
def get_conversation_state(conversation_id: str, db: Session = Depends(get_db)):
    _id(conversation_id)
    _available(db, conversation_id)
    from ..workspace import binding_revision
    return _read(db, f"conversation:{conversation_id}", ConversationState(workspace_revision=binding_revision(db, conversation_id)))


@router.get('/projects/{project_id}/git')
def get_git_draft(project_id: str, db: Session = Depends(get_db)):
    from ..workspace import get_project
    get_project(db, project_id)
    return _read(db, f'git:{project_id}', GitDraft())


@router.get('/projects/{project_id}/worktrees')
def get_worktree_draft(project_id: str, db: Session = Depends(get_db)):
    from ..workspace import get_project
    get_project(db, project_id)
    return _read(db, f'worktrees:{project_id}', WorktreeDraft())


@router.put('/projects/{project_id}/worktrees')
def put_worktree_draft(project_id: str, req: WorktreeWrite, db: Session = Depends(get_db)):
    from ..workspace import get_project
    get_project(db, project_id); db.rollback()
    return _write(db, f'worktrees:{project_id}', req.revision, req.value, None)


@router.get('/projects/{project_id}/search')
def get_search_draft(project_id: str, db: Session = Depends(get_db)):
    from ..workspace import get_project
    get_project(db, project_id)
    return _read(db, f'search:{project_id}', SearchOptions())


@router.put('/projects/{project_id}/search')
def put_search_draft(project_id: str, req: SearchWrite, db: Session = Depends(get_db)):
    from ..workspace import get_project
    get_project(db, project_id); db.rollback()
    return _write(db, f'search:{project_id}', req.revision, req.value, None)


@router.put('/projects/{project_id}/git')
def put_git_draft(project_id: str, req: GitDraftWrite, db: Session = Depends(get_db)):
    from ..workspace import get_project
    get_project(db, project_id)
    # _write owns its transaction; finish the existence check's read transaction.
    db.rollback()
    return _write(db, f'git:{project_id}', req.revision, req.value, None)


@router.put("/conversations/{conversation_id}")
def put_conversation_state(conversation_id: str, req: ConversationWrite, db: Session = Depends(get_db)):
    from ..task_events import notify_tasks_changed

    _id(conversation_id)
    result = _write(db, f"conversation:{conversation_id}", req.revision, req.value, conversation_id)
    notify_tasks_changed()
    return result
