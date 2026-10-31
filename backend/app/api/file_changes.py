from typing import Literal
import difflib

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import project_changes as changes, workspace
from ..db import get_db
from ..models import ProjectChange

router = APIRouter(prefix='/api', tags=['file-changes'])


class PrepareRequest(BaseModel):
    workspace_revision: int = Field(default=0, ge=0)
    conversation_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')
    edit: changes.EditRequest


class Decision(BaseModel):
    revision: int = Field(ge=1)


class UndoRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)


@router.get('/projects/{project_id}/editable-file')
def editable(project_id: str, path: str = Query(max_length=1024), db: Session = Depends(get_db)):
    scope = workspace.project_scope(workspace.get_project(db, project_id))
    value, digest = changes.read_original(scope, path)
    return {'path': workspace.relative_path(path), 'content': value.removeprefix('\ufeff') if value is not None else None, 'sha256': digest}


@router.post('/projects/{project_id}/changes')
def prepare(project_id: str, req: PrepareRequest, db: Session = Depends(get_db)):
    scope = workspace.project_scope(workspace.get_project(db, project_id))
    scope['workspace_revision'] = req.workspace_revision
    return changes.prepare(scope, req.conversation_id, req.edit.model_dump(), operation_key='manual:' + req.request_id)


@router.get('/projects/{project_id}/changes')
def history(project_id: str, offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    workspace.get_project(db, project_id)
    query = db.query(ProjectChange).filter_by(project_id=project_id)
    total = query.count()
    rows = query.order_by(ProjectChange.created_at.desc(), ProjectChange.id.desc()).offset(offset).limit(40).all()
    return {'changes': [changes.summary(row) for row in rows], 'total': total,
            'next_offset': offset + 40 if offset + 40 < total else None}


@router.get('/project-changes/{change_id}')
def detail(change_id: str, offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    row = changes.get(db, change_id)
    return {**changes.summary(row), 'diff': changes.diff(row, offset), 'run_active': changes.run_active(row)}


@router.get('/project-changes/{change_id}/content')
def content(change_id: str, version: Literal['before', 'after', 'current'], db: Session = Depends(get_db)):
    row = changes.get(db, change_id)
    value = row.before_text if version == 'before' else row.after_text if version == 'after' else changes.read_original(row.scope, row.path)[0]
    return {'content': value, 'version': version}


@router.get('/project-changes/{change_id}/patch')
def patch(change_id: str, db: Session = Depends(get_db)):
    row = changes.get(db, change_id)
    lines = difflib.unified_diff((row.before_text or '').splitlines(keepends=True), (row.after_text or '').splitlines(keepends=True),
                fromfile='a/' + row.path if row.before_text is not None else '/dev/null', tofile='b/' + row.path if row.after_text is not None else '/dev/null')
    value = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)
    return Response(value, media_type='text/plain', headers={'Content-Disposition': f'attachment; filename="change-{change_id}.patch"'})


@router.post('/project-changes/{change_id}/apply')
def apply(change_id: str, req: Decision):
    return changes.apply(change_id, revision=req.revision)


@router.post('/project-changes/{change_id}/reject')
def reject(change_id: str, req: Decision, db: Session = Depends(get_db)):
    row = changes.get(db, change_id)
    if changes.run_active(row):
        workspace.fail('此修改仍在等待任务处理，请使用任务的拒绝按钮', 409)
    return changes.reject(change_id, req.revision)


@router.post('/project-changes/{change_id}/undo')
def undo(change_id: str, req: UndoRequest):
    return changes.undo(change_id, req.conversation_id)
