"""Explicit library parse-version choices and recoverable index builds."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session
from typing import Literal

from ..db import get_db
from .. import library_index_store as store, library_index_service as service
from ..library_index_models import LibraryIndexOperation, LibraryIndexPreparedBatch
from ..source_runtime import conversation_scope
from ..source_store import SourceConflict
from ..timeutil import utcnow
from .sources import _source_errors

router = APIRouter(prefix="/api/library-index", tags=["library-index"])


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_version: int = Field(ge=0, strict=True)
    expected_primary_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Build(Choice):
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    set_primary: bool = False
    index_mode: Literal["text", "semantic"] = "text"


class Resume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    expected_version: int = Field(ge=0, strict=True)
    expected_primary_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Scoped(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")


@router.get("/{paper_id}/state")
def get_state(paper_id: str, conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"), db: Session = Depends(get_db)):
    with _source_errors(db):
        conversation_scope(db, conversation_id, writing=False)
        result = store.state(db, paper_id)
        result["operations"] = [store.operation_output(op) for op in db.scalars(select(LibraryIndexOperation).where(
            LibraryIndexOperation.paper_id == paper_id, LibraryIndexOperation.conversation_id == conversation_id)
            .order_by(LibraryIndexOperation.created_at.desc()).limit(10))]
        return result


@router.post("/{paper_id}/primary")
def select_primary(paper_id: str, req: Choice, db: Session = Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        result = store.select_primary(db, paper_id, req.revision_id, conversation_id=req.conversation_id,
            expected_version=req.expected_version, expected_primary_id=req.expected_primary_id)
        db.commit(); return result


@router.post("/{paper_id}/builds")
def start_build(paper_id: str, req: Build, db: Session = Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        op, created = store.create_operation(db, paper_id, req.revision_id, conversation_id=req.conversation_id,
            request_id=req.request_id, expected_version=req.expected_version, expected_primary_id=req.expected_primary_id,
            set_primary=req.set_primary, index_mode=req.index_mode)
        result = store.operation_output(op); db.commit()
        if created: service.start(result["id"])
        return {**result, "replayed": not created}


@router.get("/builds/{ident}")
def get_build(ident: str, conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"), db: Session = Depends(get_db)):
    with _source_errors(db):
        op = store.checked_operation(db, ident, conversation_id)
        from ..call_ledger import list_calls, summarize_calls
        result = store.operation_output(op)
        result["prepared_batches"] = len(list(db.scalars(select(LibraryIndexPreparedBatch.start).where(LibraryIndexPreparedBatch.operation_id == ident))))
        result["calls"] = list_calls(run_id=op.run_id, limit=100, offset=0, include_synthetic=False, db=db)
        result["usage"] = summarize_calls(run_id=op.run_id, include_synthetic=False, db=db)
        return result


@router.post("/builds/{ident}/resume")
def resume_build(ident: str, req: Resume, db: Session = Depends(get_db)):
    with _source_errors(db):
        if ident in service.active(): raise SourceConflict("索引任务仍在执行，请等待当前准备结束")
        db.execute(text("BEGIN IMMEDIATE"))
        op = store.checked_operation(db, ident, req.conversation_id, writing=True)
        if op.status == "done": return store.operation_output(op)
        if op.status == "cancelled": raise SourceConflict("此任务已取消，请新建索引请求")
        store._cas(db, op.paper_id, req.expected_version, req.expected_primary_id)
        store.authorize(db, op, check_cas=False)
        op.expected_version, op.expected_primary_id = req.expected_version, req.expected_primary_id
        op.status = "prepared" if op.status == "prepared" else "queued"
        op.error, op.updated_at = "", utcnow()
        result = store.operation_output(op); db.commit(); service.start(ident); return result


@router.post("/builds/{ident}/cancel")
def cancel_build(ident: str, req: Scoped, db: Session = Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        op = store.checked_operation(db, ident, req.conversation_id)
        if op.status == "done": raise SourceConflict("索引已经原子提交；可另选旧版本重建，不能取消已完成结果")
        op.status, op.error, op.updated_at = "cancelled", "用户取消；原索引保持不变，已发生的供应商用量仍按实际回执显示", utcnow()
        op.vectors, op.prepared = None, {}
        db.execute(delete(LibraryIndexPreparedBatch).where(LibraryIndexPreparedBatch.operation_id == ident))
        result = store.operation_output(op); db.commit(); return result
