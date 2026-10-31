"""Git inspection and explicitly reviewed manual mutations."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from .. import project_git, git_operations as operations, workspace
from ..db import get_db
from ..models import GitOperation

router = APIRouter(prefix='/api/projects/{project_id}/git', tags=['project-git'])


def scope(db, project_id):
    return workspace.project_scope(workspace.get_project(db, project_id))


@router.get('/status')
def status(project_id: str, offset: int = Query(default=0, ge=0), snapshot: str | None = Query(default=None, pattern=r'^[a-f0-9]{64}$'), db: Session = Depends(get_db)):
    return project_git.status(scope(db, project_id), offset, snapshot)


@router.get('/diff')
def diff(project_id: str, path: str = Query(max_length=1024), version: str = Query(),
         offset: int = Query(default=0, ge=0), snapshot: str | None = Query(default=None, pattern=r'^[a-f0-9]{64}$'), db: Session = Depends(get_db)):
    return project_git.diff(scope(db, project_id), path, version, offset, snapshot)


@router.get('/history')
def history(project_id: str, db: Session = Depends(get_db)):
    return {'commits': project_git.history(scope(db, project_id))}


class Decision(BaseModel):
    revision: int = Field(ge=1)


@router.post('/operations')
def prepare(project_id: str, req: operations.Request, db: Session = Depends(get_db)):
    return operations.prepare(scope(db, project_id), req)


@router.get('/operations')
def operation_history(project_id: str, offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    workspace.get_project(db, project_id)
    query = db.query(GitOperation).filter_by(project_id=project_id)
    total = query.count()
    rows = query.order_by(GitOperation.created_at.desc(), GitOperation.id.desc()).offset(offset).limit(40).all()
    return {'operations': [operations.summary(row) for row in rows], 'total': total,
            'next_offset': offset + 40 if offset + 40 < total else None}


@router.get('/operations/{operation_id}')
def operation_detail(project_id: str, operation_id: str, db: Session = Depends(get_db)):
    return operations.summary(operations.get(db, project_id, operation_id), detail=True)


@router.post('/operations/{operation_id}/apply')
def apply(project_id: str, operation_id: str, req: Decision):
    return operations.decide(project_id, operation_id, req.revision)


@router.post('/operations/{operation_id}/cancel')
def cancel(project_id: str, operation_id: str, req: Decision):
    return operations.decide(project_id, operation_id, req.revision, cancel=True)
