"""Conversation-authorized original uploads and immutable source reading.

Uploads/captures retain fixed originals and local text/structure; import into
the literature library requires a separate explicit selection. Document text
never authorizes actions, and extraction never certifies model understanding.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Conversation, DeletedConversation
from ..source_models import Source, SourceCaptureReceipt, SourceLibraryImport, SourceRevision, SourceSnapshot
from .. import source_store as store
from .. import source_documents as documents
from ..source_parsing import MEDIA, extract


router = APIRouter(prefix="/api/sources", tags=["sources"])
MAX_UPLOAD_BYTES = store.MAX_RAW_BYTES
_CID = r"^[\w-]{1,64}$"
_RID = r"^[a-f0-9]{32}$"
_MEDIA = MEDIA


def _scope(db, conversation_id, *, writing=False):
    # Local desktop authorization is the existing conversation boundary, not a
    # model-supplied SourceScope or an invented project membership.
    if db.get(DeletedConversation, conversation_id) is not None:
        raise HTTPException(410, "该会话已删除，不能访问其来源")
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "会话不存在，请先新建会话")
    if writing and conversation.archived_at is not None:
        raise HTTPException(409, "会话已归档，请恢复后再添加资料或定位")
    return store.SourceScope("conversation", conversation_id)


@contextmanager
def _source_errors(db):
    try:
        yield
    except store.SourceError as exc:
        db.rollback()
        status = (404 if isinstance(exc, (store.SourceNotFound, store.SourceAccessDenied)) else
                  409 if isinstance(exc, (store.SourceConflict, store.SourceCorruption)) else 422)
        raise HTTPException(status, str(exc)) from None


def _metadata(record, db=None):
    result = {key: value for key, value in record.items() if key not in ("text", "raw_bytes")}
    media = record["media_type"]
    result["available_views"] = ["download"] + (["text"] if record.get("text") is not None else []) + (
        ["pdf_page"] if media == "application/pdf" else [])
    result["parse_status"] = ("text_available" if record.get("text") is not None else
                              "pdf_on_demand" if media == "application/pdf" else "original_only")
    result["library_import_performed"] = False
    if db is not None:
        from ..source_models import SourceExtraction, SourceLibraryImport
        extraction = db.get(SourceExtraction, record["revision_id"])
        if extraction is not None:
            result["document_format"] = extraction.document.get("format")
            result["available_views"].append("document")
            result["extraction_fingerprint"] = extraction.fingerprint
            result["parse_status"] = "text_available" if extraction.document.get("text") else "original_available"
        receipt = db.get(SourceLibraryImport, record["revision_id"])
        if receipt:
            result.update(library_import_performed=True, library_paper_id=receipt.paper_id)
    return result


def _upload_content(file):
    title = file.filename or ""
    if (not title or len(title) > 255 or title != title.strip() or any(c in title for c in '/\\')
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in title)):
        raise HTTPException(422, "请提供有效的文件名，不能包含路径或控制字符")
    extension = title.rsplit(".", 1)[-1].casefold() if "." in title else ""
    if extension not in _MEDIA:
        raise HTTPException(415, "此来源入口支持 PDF、TXT、Markdown、CSV 和 Office 原件")
    raw = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(422, "文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "单个来源原件超过大小限制")
    extracted = None
    if extension in ("txt", "md", "markdown"):
        try:
            extracted = raw.decode("utf-8-sig")
        except UnicodeError:
            raise HTTPException(422, "文本资料须使用 UTF-8 编码，原件未保存") from None
        if "\x00" in extracted:
            raise HTTPException(422, "该文件包含二进制空字符，无法作为文本资料")
        if len(extracted.encode("utf-8")) > store.MAX_TEXT_BYTES:
            raise HTTPException(413, "文本提取结果超过大小限制")
    return title, _MEDIA[extension], raw, extracted


class EnsureConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=_CID)


@router.post("/ensure-conversation")
def ensure_source_conversation(req: EnsureConversation, db: Session = Depends(get_db)):
    from ..conversation_store import ensure_conversation, ConversationDeleted, ConversationArchived
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        conversation = ensure_conversation(db, req.conversation_id)
        db.commit()
        return {"conversation_id": conversation.id}
    except (ConversationDeleted, ConversationArchived) as exc:
        db.rollback()
        raise HTTPException(410 if isinstance(exc, ConversationDeleted) else 409, str(exc)) from None


@router.post("/upload")
def upload_source(file: UploadFile = File(),
                  conversation_id: str = Form(pattern=_CID),
                  request_id: str = Form(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$"),
                  source_id: str | None = Form(default=None, pattern=_RID),
                  expected_version: int | None = Form(default=None, ge=1),
                  csv_encoding: str | None = Form(default=None), csv_delimiter: str | None = Form(default=None),
                  csv_header_row: int | None = Form(default=None, ge=1), db: Session = Depends(get_db)):
    if (source_id is None) != (expected_version is None):
        raise HTTPException(422, "更新来源须同时指定来源ID和已读取的版本号")
    # Read bounded bytes before acquiring a write reservation; never open the
    # uploaded filename as a path. Recheck authorization after owning the lock.
    title, media, raw, extracted = _upload_content(file)
    options = {key: value for key, value in {"encoding": csv_encoding, "delimiter": csv_delimiter, "header_row": csv_header_row}.items() if value is not None}
    if options and media != "text/csv":
        raise HTTPException(422, "CSV 解析选项只能用于 CSV 原件")
    if options:
        from ..table_extraction import csv_options
        try:
            options = csv_options(options)
        except store.SourceError as exc:
            raise HTTPException(422, str(exc)) from None
    # Parse local bytes before taking the write reservation. A corrupt Office
    # original is still retained honestly; it cannot manufacture evidence.
    extraction, parse_error = None, None
    try:
        extraction = extract(raw, media, table_options=options or None)
        extracted = extraction.text
    except store.SourceError as exc:
        parse_error = str(exc)
    identity = [title, media, hashlib.sha256(raw).hexdigest(), source_id, expected_version]
    if options:
        identity.append(options)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        scope = _scope(db, conversation_id, writing=True)
        prior = db.scalar(select(SourceCaptureReceipt).where(
            SourceCaptureReceipt.scope_type == scope.scope_type, SourceCaptureReceipt.scope_id == scope.scope_id,
            SourceCaptureReceipt.request_id == request_id))
        if prior is not None:
            if prior.payload_sha256 != digest:
                raise HTTPException(409, "此上传请求编号已绑定其他资料，请使用新的请求编号")
            result = store.read_revision(db, prior.revision_id, scope=scope)
            bound_revision = db.get(SourceRevision, prior.revision_id)
            if (not prior.revision_fingerprint or bound_revision.fingerprint != prior.revision_fingerprint
                    or result["kind"] != "upload" or result["authorization_id"] != "upload:" + request_id
                    or result["raw_sha256"] != hashlib.sha256(raw).hexdigest() or result["title"] != title
                    or result["media_type"] != media or (source_id is not None and
                    (result["source_id"] != source_id or result["version"] != expected_version + 1))):
                raise store.SourceCorruption("上传回执与固定版本不一致，请检查备份")
            result["replayed"] = True
        else:
            result = store.capture_source(db, scope=scope, authorization=store.SourceAuthorization(
                scope, "upload:" + request_id, "upload"), kind="upload", title=title,
                raw_bytes=raw, text=extracted, media_type=media, origin_ref=title,
                text_origin="parsed_text" if extracted is not None else "none",
                parser=extraction.parser if extraction else ("utf8-sig/1" if extracted is not None else None),
                source_id=source_id, expected_version=expected_version)
            if extraction is not None:
                documents.save_extraction(db, result["revision_id"], scope=scope, extraction=extraction)
            db.add(SourceCaptureReceipt(scope_type=scope.scope_type, scope_id=scope.scope_id,
                request_id=request_id, payload_sha256=digest, revision_id=result["revision_id"],
                revision_fingerprint=db.get(SourceRevision, result["revision_id"]).fingerprint))
            result["replayed"] = False
        metadata = _metadata(result, db)
        if parse_error:
            metadata["parse_error"] = parse_error
        if source_id:
            from ..source_update_store import consume_if_matching
            consume_if_matching(db,scope,source_id,request_id,result["raw_sha256"])
        db.commit()
    return metadata


@router.get("")
def list_sources(conversation_id: str = Query(pattern=_CID), offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    scope = _scope(db, conversation_id)
    # SQL length/metadata only: opening a source list must not load every blob.
    rows = db.execute(select(Source, SourceRevision, SourceSnapshot.media_type,
        func.length(SourceSnapshot.raw_bytes).label("raw_size"), SourceSnapshot.text.is_not(None).label("has_text"),
        SourceLibraryImport.paper_id)
        .join(SourceRevision, (SourceRevision.id == Source.current_revision_id) & (SourceRevision.source_id == Source.id))
        .join(SourceSnapshot, SourceSnapshot.id == SourceRevision.snapshot_id)
        .outerjoin(SourceLibraryImport, SourceLibraryImport.revision_id == SourceRevision.id)
        .where(Source.scope_type == scope.scope_type, Source.scope_id == scope.scope_id,
               SourceSnapshot.scope_type == scope.scope_type, SourceSnapshot.scope_id == scope.scope_id)
        .order_by(Source.created_at.desc(), Source.id.desc()).offset(offset).limit(limit + 1)).all()
    items = [{"source_id": source.id, "revision_id": revision.id, "title": revision.title_at_capture,
              "kind": source.kind, "version": revision.version, "media_type": media,
              "current_version": source.version, "current_revision_id": source.current_revision_id,
              "origin_ref": revision.origin_ref, "created_at": revision.created_at.isoformat() + "Z",
              "library_paper_id": paper, "library_import_performed": bool(paper),
              "raw_size": size, "has_text": has_text, "integrity_verified": False,
              "trust": "untrusted_source_data", "authorizes_actions": False}
             for source, revision, media, size, has_text, paper in rows[:limit]]
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


@router.get("/{source_id}/history")
def source_revision_history(source_id: str, conversation_id: str = Query(pattern=_CID),
                            before_version: int | None = Query(default=None, ge=1),
                            limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    with _source_errors(db):
        return store.revision_history(db, source_id, scope=_scope(db, conversation_id),
                                      before_version=before_version, limit=limit)


@router.get("/revisions/{revision_id}")
def get_source_revision(revision_id: str, conversation_id: str = Query(pattern=_CID), db: Session = Depends(get_db)):
    with _source_errors(db):
        return _metadata(store.read_revision(db, revision_id, scope=_scope(db, conversation_id)), db)


@router.get("/revisions/{revision_id}/file")
def download_source_revision(revision_id: str, conversation_id: str = Query(pattern=_CID), db: Session = Depends(get_db)):
    with _source_errors(db):
        record = store.read_revision(db, revision_id, scope=_scope(db, conversation_id), include_raw=True)
    return Response(record["raw_bytes"], media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(record["title"], safe=""),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        "ETag": '"' + record["raw_sha256"] + '"',
    })


@router.get("/revisions/{revision_id}/text")
def read_source_text(revision_id: str, conversation_id: str = Query(pattern=_CID),
                     line_start: int = Query(default=1, ge=1), line_limit: int = Query(default=80, ge=1, le=200),
                     db: Session = Depends(get_db)):
    with _source_errors(db):
        record = store.read_revision(db, revision_id, scope=_scope(db, conversation_id))
    if record["text"] is None:
        raise HTTPException(409, "此版本尚无文本提取结果，可下载原件；PDF可按真实页面读取")
    lines = record["text"].splitlines(keepends=True)
    if line_start > max(1, len(lines)):
        raise HTTPException(422, "起始行超出此固定版本范围")
    end = min(len(lines), line_start + line_limit - 1)
    return {"revision_id": revision_id, "text_sha256": record["text_sha256"],
            "line_start": line_start, "line_end": end, "line_index_base": 1, "total_lines": len(lines),
            "text": "".join(lines[line_start - 1:end]), "next_line": end + 1 if end < len(lines) else None,
            "trust": "untrusted_source_data", "authorizes_actions": False, "model_understanding_verified": False}


class CreateLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    conversation_id: str = Field(pattern=_CID)
    kind: Literal["text_lines", "pdf_page", "word_paragraphs", "word_block", "sheet_cells"]
    line_start: int | None = Field(default=None, ge=1, strict=True)
    line_end: int | None = Field(default=None, ge=1, strict=True)
    page_index: int | None = Field(default=None, ge=0, strict=True)
    bbox: list[StrictFloat] | None = Field(default=None, min_length=4, max_length=4)
    paragraph_start: int | None = Field(default=None, ge=1, strict=True)
    paragraph_end: int | None = Field(default=None, ge=1, strict=True)
    block_id: str | None = Field(default=None, pattern=r"^wb_[a-f0-9]{32}$")
    sheet: str | None = Field(default=None, min_length=1, max_length=128)
    cell_range: str | None = Field(default=None, min_length=2, max_length=32)

    @model_validator(mode="after")
    def only_applicable_fields(self):
        applicable = {"text_lines": {"line_start", "line_end"}, "pdf_page": {"page_index", "bbox"},
                      "word_paragraphs": {"paragraph_start", "paragraph_end"}, "word_block": {"block_id"}, "sheet_cells": {"sheet", "cell_range"}}
        fields = {k for k in ("line_start", "line_end", "page_index", "bbox", "paragraph_start", "paragraph_end", "block_id", "sheet", "cell_range")
                  if getattr(self, k) is not None}
        required = applicable[self.kind] - ({"bbox"} if self.kind == "pdf_page" else set())
        if fields - applicable[self.kind] or not required <= fields:
            raise ValueError("定位参数与定位类型不匹配")
        return self


@router.post("/revisions/{revision_id}/locators")
def create_source_locator(revision_id: str, req: CreateLocator, db: Session = Depends(get_db)):
    with _source_errors(db):
        derived = None
        if req.kind == "pdf_page":
            record = store.read_revision(db, revision_id, scope=_scope(db, req.conversation_id, writing=True), include_raw=True)
            db.rollback()  # Parsing a complex PDF must not hold a SQLite transaction.
            derived = store.derive_pdf_page(record["raw_bytes"], media_type=record["media_type"],
                                            page_index=req.page_index, bbox=req.bbox)
        db.execute(text("BEGIN IMMEDIATE"))
        scope = _scope(db, req.conversation_id, writing=True)
        if req.kind == "text_lines":
            result = store.add_text_locator(db, revision_id, scope=scope, line_start=req.line_start, line_end=req.line_end)
        elif req.kind == "pdf_page":
            result = store.save_derived_pdf_locator(db, revision_id, scope=scope, derived=derived)
        else:
            result = documents.add_document_locator(db, revision_id, scope=scope, kind=req.kind,
                paragraph_start=req.paragraph_start, paragraph_end=req.paragraph_end,
                sheet=req.sheet, cell_range=req.cell_range, block_id=req.block_id)
        db.commit()
    return result


@router.get("/revisions/{revision_id}/locators/{locator_id}")
def get_source_locator(revision_id: str, locator_id: str, conversation_id: str = Query(pattern=_CID),
                       db: Session = Depends(get_db)):
    with _source_errors(db):
        result = store.read_locator(db, locator_id, scope=_scope(db, conversation_id))
        if result["revision_id"] != revision_id:
            raise HTTPException(404, "此定位不属于指定来源版本")
        return result


@router.get("/revisions/{revision_id}/document")
def read_source_document(revision_id: str, conversation_id: str = Query(pattern=_CID),
                         offset: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000),
                         sheet: str | None = Query(default=None, max_length=128),
                         locator_id: str | None = Query(default=None, pattern=_RID),
                         db: Session = Depends(get_db)):
    with _source_errors(db):
        scope = _scope(db, conversation_id)
        doc = documents.read_document(db, revision_id, scope=scope)
        locator = store.read_locator(db, locator_id, scope=scope) if locator_id else None
        if locator and locator["revision_id"] != revision_id:
            raise HTTPException(404, "此定位不属于指定来源版本")
        if sheet is None and locator:
            sheet = locator["location"].get("sheet")
        if sheet is not None and sheet not in [s["name"] for s in doc["sheets"]]:
            raise HTTPException(422, "工作表不属于此固定版本")
        blocks = [b for b in doc["blocks"] if sheet is None or b.get("sheet") == sheet]
        if locator:
            location = locator["location"]
            if location.get("block_id") is not None:
                offset = next((i for i, block in enumerate(blocks) if block.get("block_id") == location["block_id"]), 0)
            elif location.get("paragraph_start") is not None:
                offset = max(0, location["paragraph_start"] - 1)
            elif location.get("row_start") is not None:
                offset = next((i for i, block in enumerate(blocks)
                    if block.get("row_start", 0) >= location["row_start"]), max(0, len(blocks) - limit))
        # Bounded reader response, without a duplicate multi-MB text property.
        return {k: v for k, v in doc.items() if k not in ("text", "blocks")} | {
            "blocks": blocks[offset:offset + limit], "offset": offset, "sheet": sheet,
            "total_blocks": len(blocks),
            "next_offset": offset + limit if offset + limit < len(blocks) else None}


class CaptureProjectSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=_CID)
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    project_id: str = Field(min_length=1, max_length=32)
    workspace_revision: int = Field(ge=0, strict=True)
    path: str = Field(min_length=1, max_length=1024)
    source_id: str | None = Field(default=None, pattern=_RID)
    expected_version: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def paired_version(self):
        if (self.source_id is None) != (self.expected_version is None):
            raise ValueError("更新项目来源须同时指定来源ID和已读取的版本号")
        return self


@router.post("/capture-project")
def capture_project_source(req: CaptureProjectSource, db: Session = Depends(get_db)):
    from ..source_capture import capture_project
    with _source_errors(db):
        return capture_project(db, req.model_dump())


class ImportSources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=_CID)
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    revision_ids: list[str] = Field(min_length=1, max_length=16)


@router.post("/library-import")
def import_source_library(req: ImportSources, db: Session = Depends(get_db)):
    from ..source_library import import_selected
    with _source_errors(db):
        scope = _scope(db, req.conversation_id, writing=True)
        return import_selected(db, scope=scope, revision_ids=req.revision_ids, request_id=req.request_id)


class OpenLibrary(EnsureConversation):
    library_revision_id: str | None = Field(default=None, pattern=_RID)


@router.get("/library/{paper_id}/versions")
def library_versions(paper_id:str, conversation_id:str=Query(pattern=_CID),db:Session=Depends(get_db)):
    from ..library_sources import ensure_owned,scope_of
    with _source_errors(db):
        _scope(db,conversation_id)
        canonical,_=ensure_owned(db,paper_id)
        rows=[]
        for receipt in db.scalars(select(SourceLibraryImport).where(SourceLibraryImport.paper_id==paper_id)
                .order_by(SourceLibraryImport.created_at)):
            scope=scope_of(db,receipt.revision_id)
            if scope.scope_type!='library':continue
            record=store.read_revision(db,receipt.revision_id,scope=scope)
            rows.append({'revision_id':record['revision_id'],'title':record['title'],'parser':record['parser'],
                'created_at':record['created_at'],'text_sha256':record['text_sha256'],'raw_sha256':record['raw_sha256'],
                'primary':record['revision_id']==canonical['revision_id']})
        db.commit();return {'paper_id':paper_id,'items':rows}


@router.post("/library/{paper_id}/open")
def open_library_original(paper_id: str, req: OpenLibrary, db: Session = Depends(get_db)):
    from ..conversation_store import ensure_conversation, ConversationDeleted, ConversationArchived
    from ..source_library import open_library_source
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            ensure_conversation(db, req.conversation_id)
        except (ConversationDeleted, ConversationArchived) as exc:
            raise HTTPException(410 if isinstance(exc, ConversationDeleted) else 409, str(exc)) from None
        record, _ = open_library_source(db, paper_id, req.conversation_id, req.library_revision_id)
        metadata = _metadata(record, db)
        db.commit()
        return {**metadata, "conversation_id": req.conversation_id}
