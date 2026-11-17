"""配置诊断端点：只读返回 doctor 报告。"""
from __future__ import annotations

from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..diagnostics import run_diagnostics
from ..diagnostic_probes import ProbeChanged, run_probe

router = APIRouter(prefix="/api/debug", tags=["diagnostics"])


@router.get("/config")
def debug_config(conversation_id: str | None = Query(default=None, max_length=64, pattern=r"^[\w-]+$")):
    return run_diagnostics(conversation_id)


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["llm", "embedding"]
    expected_config: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    conversation_id: str | None = Field(default=None, max_length=64, pattern=r"^[\w-]+$")
    run_id: str | None = Field(default=None, max_length=96,pattern=r'^[\w:.-]+$')
    source_revision_id: str | None = Field(default=None, max_length=64,pattern=r'^[a-f0-9]{32}$')


@router.post("/probe")
def debug_probe(payload: ProbeInput):
    from ..db import SessionLocal
    from .diagnostic_history import Context, validate_context
    from ..diagnostic_records import save
    with SessionLocal() as db:
        validate_context(db, Context(conversation_id=payload.conversation_id, run_id=payload.run_id,
                                     source_revision_id=payload.source_revision_id))
    try:
        result = run_probe(payload.service, payload.expected_config, payload.conversation_id)
        with SessionLocal() as db:
            try:
                validate_context(db, Context(conversation_id=payload.conversation_id, run_id=payload.run_id,
                                             source_revision_id=payload.source_revision_id))
            except HTTPException:
                return {**result, "diagnostic_record": None, "record_warning": "诊断范围已删除或变化，未重新建立旧会话记录；调用回执仍可核查。"}
            else:
                record = save(db, result, kind="probe", conversation_id=payload.conversation_id,
                              run_id=payload.run_id, source_revision_id=payload.source_revision_id)
                db.commit()
        return {**result, "diagnostic_record": record}
    except ProbeChanged as exc:
        raise HTTPException(409, str(exc)) from None
