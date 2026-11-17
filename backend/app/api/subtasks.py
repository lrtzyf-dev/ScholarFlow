"""Inspectable durable child tree; recovery remains owned by the parent."""
from typing import Literal
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import subtask_inspection as service, run_store, task_events

router = APIRouter(prefix='/api/subtasks', tags=['subtasks'])


@router.get('/runs/{run_id}')
def children(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100), db: Session = Depends(get_db)):
    return service.tree(db, run_id, offset=offset, limit=limit)


@router.get('/runs/{run_id}/operations')
def operations(run_id: str, db: Session = Depends(get_db)):
    from ..operation_inspection import listing
    return listing(db, run_id)


class OperationResolution(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[\w-]{8,80}$')
    expected_parent_revision: int = Field(ge=1, strict=True)
    expected_attempt_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    operation_key: str = Field(min_length=1, max_length=160)
    signature: str = Field(pattern=r'^[a-f0-9]{64}$')
    action: Literal['executed', 'not_executed', 'unknown']
    reference: str = Field(min_length=1, max_length=2000)


@router.post('/runs/{run_id}/operations/reconcile')
def reconcile_operation(run_id: str, body: OperationResolution, db: Session = Depends(get_db)):
    from ..operation_inspection import reconcile
    with run_store.coordinated():
        db.execute(text('BEGIN IMMEDIATE'))
        try:
            result = reconcile(db, run_id, body.model_dump())
            db.commit()
        except Exception:
            db.rollback()
            raise
    task_events.notify_tasks_changed()
    return result


@router.get('/nodes/{node_id}/attempts')
def attempts(node_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return service.attempts(db, node_id, offset=offset, limit=limit)


@router.get('/nodes/{node_id}/calls')
def calls(node_id: str, attempt: int | None = Query(None, ge=1), offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    return service.calls(db, node_id, attempt=attempt, offset=offset, limit=limit)


class Resolution(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[\w-]{8,80}$')
    expected_parent_revision: int = Field(ge=1, strict=True)
    expected_attempt: int = Field(ge=1, strict=True)
    operation_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    action: Literal['retry', 'skip']
    reference: str = Field(min_length=1, max_length=2000)


@router.post('/nodes/{node_id}/reconcile')
def reconcile(node_id: str, body: Resolution, db: Session = Depends(get_db)):
    with run_store.coordinated():
        db.execute(text('BEGIN IMMEDIATE'))
        try:
            result = service.reconcile(db, node_id, body.model_dump())
            db.commit()
        except Exception:
            db.rollback()
            raise
    task_events.notify_tasks_changed()
    return result
