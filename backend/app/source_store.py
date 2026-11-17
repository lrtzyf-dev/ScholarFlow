"""Source core. Callers supply authorized scopes and already obtained bytes.

This layer never resolves/opens a caller path, URL, attachment reference or
library ID. Capture aliases retain provenance; content blobs deduplicate by
hash within one scope. Revisions and locators always reference fixed snapshots.
Helpers flush only, leaving commit/rollback to the caller's transaction.

The caller must validate membership and the user's authorization receipt before
constructing SourceAuthorization. Document text is never parsed as permission.
Text extraction/transport/locating a passage does not prove model understanding.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .source_models import Source, SourceLocator, SourceRevision, SourceSnapshot
from .timeutil import utcnow


MAX_RAW_BYTES = 100 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024
KINDS = {"upload", "local", "library", "web"}
TEXT_ORIGINS = {"none", "provided_text", "parsed_text", "ocr_text", "web_text"}
_ID = re.compile(r"^[a-f0-9]{32}$")
_SCOPE_ID = re.compile(r"^[\w:-]{1,96}$")


class SourceError(ValueError):
    pass


class SourceAccessDenied(SourceError):
    pass


class SourceNotFound(SourceError):
    pass


class SourceConflict(SourceError):
    pass


class SourceCorruption(SourceError):
    pass


@dataclass(frozen=True)
class SourceScope:
    scope_type: str
    scope_id: str


@dataclass(frozen=True)
class SourceAuthorization:
    scope: SourceScope
    authorization_id: str
    source_kind: str
    action: str = "capture"


def _scope(scope):
    if (not isinstance(scope, SourceScope) or scope.scope_type not in ("conversation", "project", "library")
            or not isinstance(scope.scope_id, str) or not _SCOPE_ID.fullmatch(scope.scope_id)):
        raise SourceAccessDenied("来源授权范围无效")
    return scope


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SourceNotFound("来源引用不存在")
    return value


def _text(value, label, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise SourceError(f"{label}无效")
    return value


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _fingerprint(raw_hash, text_hash, media_type):
    return _hash(json.dumps([raw_hash, text_hash, media_type], ensure_ascii=False).encode("utf-8"))


def _revision_fingerprint(revision):
    return _hash(json.dumps([revision.source_id, revision.snapshot_id, revision.version, revision.title_at_capture,
        revision.snapshot_fingerprint, revision.authorization_id, revision.origin_ref,
        revision.text_origin, revision.parser], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _source(db, source_id, scope):
    _scope(scope)
    source = db.get(Source, _id(source_id), populate_existing=True)
    if source is None:
        raise SourceNotFound("来源引用不存在")
    if (source.scope_type, source.scope_id) != (scope.scope_type, scope.scope_id):
        raise SourceAccessDenied("来源不属于当前授权范围")
    return source


def _revision(db, revision_id, scope):
    revision = db.get(SourceRevision, _id(revision_id), populate_existing=True)
    if revision is None:
        raise SourceNotFound("来源版本已缺失，不能跳转到其他版本代替")
    source = _source(db, revision.source_id, scope)
    snapshot = db.get(SourceSnapshot, revision.snapshot_id, populate_existing=True)
    if snapshot is None:
        raise SourceCorruption("来源快照已缺失，请从备份恢复")
    if (snapshot.scope_type, snapshot.scope_id) != (scope.scope_type, scope.scope_id):
        raise SourceCorruption("来源快照的授权范围不一致")
    try:
        if not isinstance(snapshot.raw_bytes, bytes) or (snapshot.text is not None and not isinstance(snapshot.text, str)):
            raise TypeError()
        raw_hash = _hash(snapshot.raw_bytes)
        text_hash = _hash(snapshot.text.encode("utf-8")) if snapshot.text is not None else None
        valid = (raw_hash == snapshot.raw_sha256 and text_hash == snapshot.text_sha256
                 and snapshot.fingerprint == _fingerprint(raw_hash, text_hash, snapshot.media_type)
                 and revision.snapshot_fingerprint == snapshot.fingerprint
                 and revision.fingerprint == _revision_fingerprint(revision))
    except (TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise SourceCorruption("来源快照校验失败，不能作为原文证据使用")
    return source, revision, snapshot


def _public(source, revision, snapshot, *, include_raw=False):
    result = {"source_id": source.id, "revision_id": revision.id, "snapshot_id": snapshot.id,
              "version": revision.version, "current_version": source.version,
              "current_revision_id": source.current_revision_id, "title": revision.title_at_capture,
              "title_at_capture": revision.title_at_capture,
              "current_title": source.title, "kind": source.kind,
              "scope_type": source.scope_type, "scope_id": source.scope_id,
              "media_type": snapshot.media_type, "raw_sha256": snapshot.raw_sha256,
              "text_sha256": snapshot.text_sha256, "snapshot_fingerprint": snapshot.fingerprint,
              "raw_size": len(snapshot.raw_bytes), "text": snapshot.text,
              "created_at": revision.created_at.isoformat() + "Z",
              "origin_ref": revision.origin_ref, "text_origin": revision.text_origin, "parser": revision.parser,
              "authorization_id": revision.authorization_id, "trust": "untrusted_source_data",
              "authorizes_actions": False, "extraction_accuracy_verified": False,
              "model_understanding_verified": False}
    if include_raw:
        result["raw_bytes"] = snapshot.raw_bytes
    return result


def capture_source(db: Session, *, scope: SourceScope, authorization: SourceAuthorization,
                   kind: str, title: str, raw_bytes: bytes | None = None, text: str | None = None,
                   media_type="text/plain", origin_ref=None, text_origin=None, parser=None,
                   source_id=None, expected_version=None):
    _scope(scope)
    if (not isinstance(authorization, SourceAuthorization) or authorization.scope != scope
            or authorization.source_kind != kind or authorization.action != "capture"):
        raise SourceAccessDenied("必须提供当前范围及来源类型的明确采集授权")
    _text(authorization.authorization_id, "来源授权编号", 160)
    if not isinstance(kind, str) or kind not in KINDS:
        raise SourceError("来源类型无效")
    title, media_type = _text(title, "来源名称", 1024), _text(media_type, "来源媒体类型", 128).lower()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type):
        raise SourceError("来源媒体类型无效")
    if origin_ref is not None:
        _text(origin_ref, "来源标识", 8192)
    if parser is not None:
        _text(parser, "解析器标识", 160)
    if text is not None:
        if not isinstance(text, str):
            raise SourceError("来源文本必须由调用方明确提供")
        try:
            encoded_text = text.encode("utf-8")
        except UnicodeError:
            raise SourceError("来源文本编码无效") from None
        if len(encoded_text) > MAX_TEXT_BYTES:
            raise SourceError("来源文本超过大小限制")
    else:
        encoded_text = None
    if raw_bytes is None and encoded_text is not None:
        raw_bytes = encoded_text
    if not isinstance(raw_bytes, bytes) or not 1 <= len(raw_bytes) <= MAX_RAW_BYTES:
        raise SourceError("必须显式提供有界资料字节；不能传入文件路径或空内容")
    text_origin = text_origin or ("provided_text" if text is not None else "none")
    if not isinstance(text_origin, str) or text_origin not in TEXT_ORIGINS or (text is None) != (text_origin == "none"):
        raise SourceError("来源文本的提取类型无效")
    raw_hash, text_hash = _hash(raw_bytes), _hash(encoded_text) if encoded_text is not None else None
    fingerprint = _fingerprint(raw_hash, text_hash, media_type)
    source = _source(db, source_id, scope) if source_id else None
    if source is not None:
        if type(expected_version) is not int or expected_version < 1:
            raise SourceConflict("更新来源必须携带已读取的版本号")
        if source.kind != kind:
            raise SourceConflict("不能把现有来源改为另一种来源类型")
        if source.version != expected_version:
            raise SourceConflict("来源已更新，请刷新后重试")
        current = _revision(db, source.current_revision_id, scope)
        if current[0].id != source.id or current[1].version != source.version:
            raise SourceCorruption("来源当前版本引用不一致")
        if (current[2].fingerprint, current[1].title_at_capture, current[1].origin_ref,
                current[1].text_origin, current[1].parser, current[1].authorization_id) == (
                fingerprint, title, origin_ref, text_origin, parser, authorization.authorization_id):
            return {**_public(*current), "reused_snapshot": True, "reused_revision": True}
    elif expected_version is not None:
        raise SourceConflict("新来源不能指定已有版本号")
    snapshot = db.scalar(select(SourceSnapshot).where(SourceSnapshot.scope_type == scope.scope_type,
        SourceSnapshot.scope_id == scope.scope_id, SourceSnapshot.fingerprint == fingerprint))
    reused = snapshot is not None
    if snapshot is None:
        inserted = db.execute(insert(SourceSnapshot).values(id=uuid4().hex, scope_type=scope.scope_type,
            scope_id=scope.scope_id, fingerprint=fingerprint, raw_sha256=raw_hash, text_sha256=text_hash,
            media_type=media_type, raw_bytes=raw_bytes, text=text, created_at=utcnow())
            .on_conflict_do_nothing(index_elements=["scope_type", "scope_id", "fingerprint"]))
        reused = inserted.rowcount == 0
        snapshot = db.scalar(select(SourceSnapshot).where(SourceSnapshot.scope_type == scope.scope_type,
            SourceSnapshot.scope_id == scope.scope_id, SourceSnapshot.fingerprint == fingerprint))
    if (snapshot is None or snapshot.raw_bytes != raw_bytes or snapshot.text != text
            or snapshot.raw_sha256 != raw_hash or snapshot.text_sha256 != text_hash or snapshot.media_type != media_type):
        raise SourceCorruption("已有内容哈希与快照内容不一致")
    revision_id = uuid4().hex
    version = source.version + 1 if source else 1
    if source is None:
        source = Source(scope_type=scope.scope_type, scope_id=scope.scope_id, kind=kind, title=title,
                        version=version, current_revision_id=revision_id)
        db.add(source)
        db.flush()
    else:
        changed = db.execute(update(Source).where(Source.id == source.id, Source.version == expected_version)
            .values(version=version, title=title, current_revision_id=revision_id, updated_at=utcnow())
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise SourceConflict("来源已更新，请刷新后重试")
        db.expire(source)
    revision = SourceRevision(id=revision_id, source_id=source.id, snapshot_id=snapshot.id, version=version,
        title_at_capture=title, snapshot_fingerprint=snapshot.fingerprint,
        authorization_id=authorization.authorization_id, origin_ref=origin_ref, text_origin=text_origin, parser=parser)
    revision.fingerprint = _revision_fingerprint(revision)
    db.add(revision)
    db.flush()
    return {**_public(source, revision, snapshot), "reused_snapshot": reused, "reused_revision": False}


def read_revision(db: Session, revision_id: str, *, scope: SourceScope, include_raw=False):
    if type(include_raw) is not bool:
        raise SourceError("原始资料读取选项无效")
    return _public(*_revision(db, revision_id, scope), include_raw=include_raw)


def current_source(db: Session, source_id: str, *, scope: SourceScope):
    source = _source(db, source_id, scope)
    current = read_revision(db, source.current_revision_id, scope=scope)
    if current["source_id"] != source.id or current["version"] != source.version:
        raise SourceCorruption("来源当前版本引用不一致")
    return current


def revision_history(db: Session, source_id: str, *, scope: SourceScope, before_version=None, limit=20):
    """Authorize the source before listing bounded metadata, never its bodies.

    Version cursors remain stable when another window appends a newer revision.
    Reading an individual version still performs the full snapshot integrity check.
    """
    from .source_models import SourceExtraction, SourceLibraryImport
    source = _source(db, source_id, scope)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise SourceError("版本列表数量无效")
    if before_version is not None and (type(before_version) is not int or before_version < 1):
        raise SourceError("版本列表游标无效")
    query = (select(SourceRevision, SourceSnapshot.media_type, SourceSnapshot.raw_sha256,
        SourceSnapshot.text_sha256, func.length(SourceSnapshot.raw_bytes),
        SourceSnapshot.text.is_not(None), SourceExtraction.revision_id,
        SourceExtraction.document["format"].as_string(), SourceLibraryImport.paper_id)
        .join(SourceSnapshot, SourceSnapshot.id == SourceRevision.snapshot_id)
        .outerjoin(SourceExtraction, SourceExtraction.revision_id == SourceRevision.id)
        .outerjoin(SourceLibraryImport, SourceLibraryImport.revision_id == SourceRevision.id)
        .where(SourceRevision.source_id == source.id,
               SourceSnapshot.scope_type == scope.scope_type, SourceSnapshot.scope_id == scope.scope_id))
    if before_version is not None:
        query = query.where(SourceRevision.version < before_version)
    rows = db.execute(query.order_by(SourceRevision.version.desc()).limit(limit + 1)).all()
    items = []
    for revision, media, raw_hash, text_hash, size, has_text, extraction, fmt, paper in rows[:limit]:
        items.append({"source_id": source.id, "revision_id": revision.id,
            "version": revision.version, "current_version": source.version,
            "current_revision_id": source.current_revision_id, "title": revision.title_at_capture,
            "kind": source.kind, "origin_ref": revision.origin_ref, "media_type": media,
            "created_at": revision.created_at.isoformat() + "Z", "raw_sha256": raw_hash,
            "text_sha256": text_hash, "raw_size": size, "document_format": fmt,
            "parse_status": "text_available" if has_text else "pdf_on_demand" if media == "application/pdf" else "original_only",
            "library_paper_id": paper, "library_import_performed": bool(paper),
            "integrity_verified": False, "trust": "untrusted_source_data", "authorizes_actions": False})
    return {"source_id": source.id, "current_version": source.version,
            "current_revision_id": source.current_revision_id, "items": items,
            "next_before_version": items[-1]["version"] if len(rows) > limit else None}


def _locator_hash(revision_id, kind, location, quote):
    return _hash(json.dumps([revision_id, kind, location, quote], ensure_ascii=False,
                           sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _save_locator(db, revision_id, kind, location, quote):
    fingerprint = _locator_hash(revision_id, kind, location, quote)
    locator = db.scalar(select(SourceLocator).where(SourceLocator.revision_id == revision_id,
                                                   SourceLocator.fingerprint == fingerprint))
    if locator is None:
        locator = SourceLocator(revision_id=revision_id, kind=kind, location=location, quote=quote, fingerprint=fingerprint)
        db.add(locator)
        db.flush()
    elif locator.fingerprint != _locator_hash(locator.revision_id, locator.kind, locator.location, locator.quote):
        raise SourceCorruption("已有来源定位记录校验失败")
    return {"locator_id": locator.id, "revision_id": revision_id, "kind": kind, "location": location,
            "quote": quote, "fingerprint": fingerprint, "trust": "untrusted_source_data",
            "authorizes_actions": False, "model_understanding_verified": False}


def add_text_locator(db: Session, revision_id: str, *, scope: SourceScope, line_start: int, line_end: int):
    _, _, snapshot = _revision(db, revision_id, scope)
    lines = snapshot.text.splitlines(keepends=True) if snapshot.text is not None else []
    if type(line_start) is not int or type(line_end) is not int or not 1 <= line_start <= line_end <= len(lines):
        raise SourceError("文本行范围无效或该版本没有文本快照")
    return _save_locator(db, revision_id, "text_lines", {"line_start": line_start, "line_end": line_end,
        "line_index_base": 1, "text_sha256": snapshot.text_sha256}, "".join(lines[line_start - 1:line_end]))


@dataclass(frozen=True)
class DerivedPDFPage:
    raw_sha256: str
    location: dict
    quote: str


def derive_pdf_page(raw_bytes: bytes, *, media_type: str, page_index: int, bbox=None):
    """Parse already authorized bytes outside the database write transaction."""
    if media_type != "application/pdf" or type(page_index) is not int or page_index < 0:
        raise SourceError("PDF 页码或来源类型无效")
    if not isinstance(raw_bytes, bytes) or not 1 <= len(raw_bytes) <= MAX_RAW_BYTES:
        raise SourceError("PDF 原件字节无效")
    raw_hash = _hash(raw_bytes)
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            if len(pdf.pages) > 1000:
                raise SourceError("PDF 超过1000页本地解析限制")
            if page_index >= len(pdf.pages):
                raise SourceError(f"PDF 共{len(pdf.pages)}页，可用page_index为0至{len(pdf.pages) - 1}，请求未读取原文")
            page = pdf.pages[page_index]
            actual = list(page.bbox)
            if bbox is None:
                region = actual
            elif (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                  or any(type(value) not in (int, float) or not math.isfinite(value) for value in bbox)
                  or not actual[0] <= bbox[0] < bbox[2] <= actual[2]
                  or not actual[1] <= bbox[1] < bbox[3] <= actual[3]):
                raise SourceError("PDF 定位区域必须处于真实页面坐标范围内")
            else:
                region = list(bbox)
            quote = page.crop(tuple(region)).extract_text() or ""
            location = {"page_index": page_index, "page_number": page_index + 1, "page_index_base": 0,
                "page_count": len(pdf.pages),
                "bbox": region, "page_bbox": actual, "page_width": float(page.width), "page_height": float(page.height),
                "rotation": page.rotation, "coordinate_space": "pdf_points_top_left", "unit": "pt",
                "geometry_verified": True, "raw_sha256": raw_hash,
                "text_observed": bool(quote), "extraction_accuracy_verified": False}
    except SourceError:
        raise
    except Exception:
        raise SourceError("该快照无法解析为可定位 PDF；请保留原件或使用文本引用") from None
    return DerivedPDFPage(raw_hash, location, quote)


def save_derived_pdf_locator(db: Session, revision_id: str, *, scope: SourceScope, derived: DerivedPDFPage):
    """Internal server-derived result, never accept this object from an API body.

    The caller rechecks user authorization after acquiring its short write
    transaction. Revalidate fixed content here before persisting the location.
    """
    _, _, snapshot = _revision(db, revision_id, scope)
    if (not isinstance(derived, DerivedPDFPage) or snapshot.media_type != "application/pdf"
            or snapshot.raw_sha256 != derived.raw_sha256):
        raise SourceConflict("PDF 定位与原件版本不一致，未保存")
    return _save_locator(db, revision_id, "pdf_page", derived.location, derived.quote)


def add_pdf_locator(db: Session, revision_id: str, *, scope: SourceScope, page_index: int, bbox=None):
    """Derive geometry/quote from the actual immutable PDF bytes, never OCR guesses."""
    _, _, snapshot = _revision(db, revision_id, scope)
    derived = derive_pdf_page(snapshot.raw_bytes, media_type=snapshot.media_type, page_index=page_index, bbox=bbox)
    return save_derived_pdf_locator(db, revision_id, scope=scope, derived=derived)


def read_locator(db: Session, locator_id: str, *, scope: SourceScope):
    locator = db.get(SourceLocator, _id(locator_id), populate_existing=True)
    if locator is None:
        raise SourceNotFound("来源定位记录已缺失")
    source, revision, snapshot = _revision(db, locator.revision_id, scope)
    if locator.fingerprint != _locator_hash(locator.revision_id, locator.kind, locator.location, locator.quote):
        raise SourceCorruption("来源定位记录校验失败")
    return {"locator_id": locator.id, "source_id": source.id, "revision_id": revision.id,
            "kind": locator.kind, "location": locator.location, "quote": locator.quote,
            "raw_sha256": snapshot.raw_sha256, "fingerprint": locator.fingerprint,
            "trust": "untrusted_source_data", "authorizes_actions": False, "model_understanding_verified": False}
