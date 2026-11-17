"""Explicit CSV interpretations and version-bound table computations."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictFloat
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db import get_db
from .. import source_store as store, source_documents as documents
from ..source_models import SourceCaptureReceipt, SourceRevision
from ..source_parsing import extract
from ..table_extraction import preview_csv
from .sources import _scope, _source_errors, _metadata, _CID, _RID

router = APIRouter(prefix="/api/sources", tags=["tables"])


class TableCompute(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    conversation_id: str = Field(pattern=_CID)
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    sheet: str = Field(min_length=1, max_length=128)
    cell_range: str = Field(min_length=2, max_length=32)
    absolute_tolerance: StrictFloat = Field(default=1e-9, ge=0, le=1)
    relative_tolerance: StrictFloat = Field(default=1e-9, ge=0, le=1)


@router.post("/revisions/{revision_id}/table-computations")
def compute_table(revision_id: str, req: TableCompute, db: Session = Depends(get_db)):
    from ..table_analysis import compute
    with _source_errors(db):
        scope = _scope(db, req.conversation_id, writing=True)
        value = compute(db, revision_id, scope=scope, request_id=req.request_id, expected_fingerprint=req.expected_fingerprint,
            config=req.model_dump(exclude={"conversation_id", "request_id", "expected_fingerprint"}))
        db.commit()
        return value


@router.get("/revisions/{revision_id}/table-computations")
def table_history(revision_id: str, conversation_id: str = Query(pattern=_CID), db: Session = Depends(get_db)):
    from ..table_analysis import history
    with _source_errors(db):
        return history(db, revision_id, scope=_scope(db, conversation_id))


@router.get("/revisions/{revision_id}/table-computations/{computation_id}/bundle")
def table_bundle(revision_id: str, computation_id: str, conversation_id: str = Query(pattern=_CID), db: Session = Depends(get_db)):
    from ..table_analysis import bundle
    with _source_errors(db):
        raw = bundle(db, revision_id, computation_id, scope=_scope(db, conversation_id))
        return Response(raw, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="table-reproduction.zip"'})


class CsvOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=_CID)
    encoding: Literal["auto", "utf-8-sig", "utf-16", "gb18030", "cp1252"] = "auto"
    delimiter: Literal["auto", "comma", "semicolon", "tab", "pipe"] = "auto"
    header_row: int = Field(default=1, ge=1, le=100000, strict=True)


class CsvInterpret(CsvOptions):
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    expected_version: int = Field(ge=1, strict=True)


@router.post("/revisions/{revision_id}/csv-preview")
def csv_preview(revision_id: str, req: CsvOptions, db: Session = Depends(get_db)):
    with _source_errors(db):
        record = store.read_revision(db, revision_id, scope=_scope(db, req.conversation_id), include_raw=True)
        if record["media_type"] != "text/csv":
            raise HTTPException(422, "此来源不是 CSV 原件")
        db.rollback()
        result = preview_csv(record["raw_bytes"], req.model_dump(exclude={"conversation_id"}))
        return {**result, "revision_id": revision_id, "raw_sha256": record["raw_sha256"]}


@router.post("/revisions/{revision_id}/csv-interpret")
def csv_interpret(revision_id: str, req: CsvInterpret, db: Session = Depends(get_db)):
    with _source_errors(db):
        scope = _scope(db, req.conversation_id, writing=True)
        record = store.read_revision(db, revision_id, scope=scope, include_raw=True)
        if record["media_type"] != "text/csv":
            raise HTTPException(422, "此来源不是 CSV 原件")
        options = req.model_dump(exclude={"conversation_id", "request_id", "expected_version"})
        if record["version"] != req.expected_version:
            raise store.SourceConflict("请在当前固定版本上调整解析选项；旧版本只用于核对")
        db.rollback()
        extraction = extract(record["raw_bytes"], "text/csv", table_options=options)
        digest = hashlib.sha256(json.dumps(["csv-interpret/1", revision_id, req.expected_version, record["raw_sha256"], options],
                                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        db.execute(text("BEGIN IMMEDIATE"))
        scope = _scope(db, req.conversation_id, writing=True)
        # Recheck the immutable original after parsing, and before accepting a new interpretation.
        current_record = store.read_revision(db, revision_id, scope=scope)
        if current_record["raw_sha256"] != record["raw_sha256"]:
            raise store.SourceCorruption("CSV 固定原件已损坏，未保存新解释")
        prior = db.scalar(select(SourceCaptureReceipt).where(SourceCaptureReceipt.scope_type == scope.scope_type,
            SourceCaptureReceipt.scope_id == scope.scope_id, SourceCaptureReceipt.request_id == req.request_id))
        if prior:
            if prior.payload_sha256 != digest:
                raise store.SourceConflict("此解析请求已绑定其他选项，请使用新请求编号")
            result = store.read_revision(db, prior.revision_id, scope=scope)
            if db.get(SourceRevision, prior.revision_id).fingerprint != prior.revision_fingerprint:
                raise store.SourceCorruption("CSV 解析回执与固定版本不一致")
        else:
            result = store.capture_source(db, scope=scope, authorization=store.SourceAuthorization(scope,
                "csv-interpret:" + req.request_id, record["kind"]), kind=record["kind"], title=record["title"],
                raw_bytes=record["raw_bytes"], text=extraction.text, media_type="text/csv", origin_ref=record["origin_ref"],
                text_origin="parsed_text", parser=extraction.parser, source_id=record["source_id"], expected_version=req.expected_version)
            documents.save_extraction(db, result["revision_id"], scope=scope, extraction=extraction)
            db.add(SourceCaptureReceipt(scope_type=scope.scope_type, scope_id=scope.scope_id, request_id=req.request_id,
                payload_sha256=digest, revision_id=result["revision_id"], revision_fingerprint=db.get(SourceRevision, result["revision_id"]).fingerprint))
        metadata = _metadata(result, db)
        db.commit()
        return metadata
