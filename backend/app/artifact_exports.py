"""One atomic saved byte payload per idempotent fixed-revision export request."""
from __future__ import annotations

import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from . import artifact_store as store
from .artifact_models import ArtifactExport, ArtifactManifest, ArtifactRevision
from .models import Conversation
from . import artifact_export_formats as formats

PROTOCOL = "artifact-export-v1"
RENDERER_VERSION = "artifact-export-renderer-v1"
MAX_BYTES = formats.MAX_BYTES
EXPORT_FORMATS = frozenset(formats.FORMATS)


def _request(scope_key, artifact_id, revision_id, manifest_sha256, format, options, run_id, goal_id):
    return {"protocol": PROTOCOL, "scope_key": scope_key, "artifact_id": artifact_id,
        "revision_id": revision_id, "manifest_sha256": manifest_sha256, "format": format,
        "options": options, "origin_run_id": run_id, "origin_goal_id": goal_id}


def _binding(row, validation):
    # Detect accidental/isolated field corruption; this is not a DB signature.
    return store.digest(store.canonical({key: getattr(row, key) for key in (
        "id", "artifact_id", "revision_id", "conversation_id", "scope_key", "request_id", "request_sha256",
        "content_sha256", "manifest_sha256", "title", "format", "options", "protocol", "renderer_version",
        "filename", "media_type", "payload_sha256", "payload_size", "origin_run_id", "origin_goal_id")}
        | {"validation": validation}))


def validate(db, row):
    """Recheck stored bytes and source; no writes, renderer or script execution."""
    try:
        if row.id:
            current = db.get(ArtifactExport, row.id, populate_existing=True)
            if current is None:
                raise ValueError("export no longer exists")
            row = current
        db.get(ArtifactManifest, row.artifact_id, populate_existing=True)
        db.get(ArtifactRevision, row.revision_id, populate_existing=True)
        artifact = store.get(db, row.artifact_id, row.conversation_id)
        rev = store.revision(db, artifact, row.revision_id)
        scope_key, _ = store.scope(db, row.conversation_id)
        if (row.scope_key != scope_key or row.protocol != PROTOCOL or row.format not in EXPORT_FORMATS
                or row.options != {} or row.content_sha256 != rev.content_sha256
                or row.manifest_sha256 != rev.manifest_sha256 or not isinstance(row.title, str)
                or not 1 <= len(row.title) <= 240 or not isinstance(row.renderer_version, str)
                or not 1 <= len(row.renderer_version) <= 96):
            raise ValueError("fixed export metadata mismatch")
        request = _request(scope_key, artifact.id, rev.id, rev.manifest_sha256, row.format, row.options,
                           row.origin_run_id, row.origin_goal_id)
        media, ext = formats.FORMATS[row.format]
        if (row.request_sha256 != store.digest(store.canonical(request))
                or row.filename != f"ScholarFlow-{artifact.id}-v{rev.version}.{ext}" or row.media_type != media
                or not isinstance(row.payload, bytes) or type(row.payload_size) is not int
                or not 0 < row.payload_size <= MAX_BYTES or row.payload_size != len(row.payload)
                or store.digest(row.payload) != row.payload_sha256 or not isinstance(row.validation, dict)):
            raise ValueError("export bytes or metadata mismatch")
        stored = {key: value for key, value in row.validation.items() if key != "binding_sha256"}
        if row.validation.get("binding_sha256") != _binding(row, stored):
            raise ValueError("export receipt binding mismatch")
        generation = {key: stored[key] for key in ("descriptor", "computation", "warnings")}
        checked = formats.inspect(db, artifact, rev, row.title, row.format, row.payload, generation)
        if checked != stored:
            raise ValueError("export verification changed")
        return checked
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(409, "固定导出完整性或格式校验失败，请保留文件并从备份恢复") from exc


def public(row):
    """Metadata only, also safe for deferred-BLOB list queries."""
    query = f"?conversation_id={row.conversation_id}" if row.conversation_id else ""
    return {key: getattr(row, key) for key in ("id", "artifact_id", "revision_id", "conversation_id",
        "format", "filename", "media_type", "payload_sha256", "payload_size", "content_sha256",
        "manifest_sha256", "renderer_version", "protocol", "validation", "origin_run_id", "origin_goal_id")} | {
        "export_id": row.id, "status": "exported", "created_at": row.created_at.isoformat(),
        "download_url": f"/api/artifacts/{row.artifact_id}/exports/{row.id}/download{query}"}


def get(db, artifact_id, export_id, conversation_id=None):
    store.get(db, artifact_id, conversation_id)
    row = db.get(ArtifactExport, export_id, populate_existing=True)
    if row is None or row.artifact_id != artifact_id or row.conversation_id != conversation_id:
        raise HTTPException(404, "固定导出不存在或不属于当前会话")
    validate(db, row)
    return row


def _existing(db, scope_key, request_id, request_hash):
    found = db.query(ArtifactExport).filter_by(scope_key=scope_key, request_id=request_id).populate_existing().first()
    if found is not None:
        if found.request_sha256 != request_hash:
            raise HTTPException(409, "同一导出请求 ID 已用于不同参数，请使用新请求 ID")
        validate(db, found)
    return found


def create(db, *, artifact_id, conversation_id, revision_id, request_id, format, options=None,
           origin_run_id=None, origin_goal_id=None, before_commit=None):
    if (not isinstance(request_id, str) or re.fullmatch(r"[\w-]{8,96}", request_id) is None
            or not isinstance(revision_id, str) or re.fullmatch(r"[a-f0-9]{32}", revision_id) is None
            or not isinstance(format, str) or format not in EXPORT_FORMATS
            or (options is not None and options != {})
            or options is not None and not isinstance(options, dict)
            or before_commit is not None and not callable(before_commit)
            or origin_run_id is not None and (not isinstance(origin_run_id, str) or not 1 <= len(origin_run_id) <= 96)
            or origin_goal_id is not None and (not isinstance(origin_goal_id, str) or not 1 <= len(origin_goal_id) <= 64)):
        raise HTTPException(422, "请提供固定版本、有效导出格式和8–96字请求 ID；当前不接受导出选项")
    db.get(ArtifactManifest, artifact_id, populate_existing=True)
    db.get(ArtifactRevision, revision_id, populate_existing=True)
    if conversation_id:
        db.get(Conversation, conversation_id, populate_existing=True)
    artifact = store.get(db, artifact_id, conversation_id, writing=True)
    rev = store.revision(db, artifact, revision_id)
    key, _ = store.scope(db, conversation_id, writing=True)
    options = {}
    request_hash = store.digest(store.canonical(_request(key, artifact.id, rev.id, rev.manifest_sha256,
        format, options, origin_run_id, origin_goal_id)))
    found = _existing(db, key, request_id, request_hash)
    if found:
        if before_commit is not None:
            from .run_store import coordinated
            try:
                with coordinated():
                    before_commit()
            except Exception:
                db.rollback()
                raise
        return public(found)
    title, content_hash, manifest_hash, version = artifact.title, rev.content_sha256, rev.manifest_sha256, rev.version
    kind = artifact.kind
    try:
        payload, generation = formats.render(db, artifact, rev, title, format)
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BYTES:
            raise ValueError("导出文件超出16 MiB限制")
        validation = formats.inspect(db, artifact, rev, title, format, payload, generation)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "导出生成或文件结构校验失败；未保存成功回执") from exc
    media, extension = formats.FORMATS[format]
    row = ArtifactExport(id=uuid4().hex, artifact_id=artifact.id, revision_id=rev.id,
        conversation_id=conversation_id, scope_key=key, request_id=request_id, request_sha256=request_hash,
        content_sha256=content_hash, manifest_sha256=manifest_hash, title=title,
        format=format, options=options, protocol=PROTOCOL, renderer_version=RENDERER_VERSION,
        filename=f"ScholarFlow-{artifact.id}-v{version}.{extension}", media_type=media,
        payload=payload, payload_sha256=store.digest(payload), payload_size=len(payload), validation=validation,
        origin_run_id=origin_run_id, origin_goal_id=origin_goal_id)
    row.validation = {**validation, "binding_sha256": _binding(row, validation)}
    from .run_store import coordinated
    try:
        # Rendering takes place outside the short conversation mutation lock.
        # Obtain the database writer boundary before rechecking deletion/scope,
        # so another process cannot delete between that check and this commit.
        with coordinated():
            result = db.execute(update(ArtifactManifest).where(ArtifactManifest.id == artifact_id)
                .values(id=artifact_id))
            if result.rowcount != 1:
                raise HTTPException(404, "成果已删除，未保存导出")
            if conversation_id:
                db.get(Conversation, conversation_id, populate_existing=True)
            latest = store.get(db, artifact_id, conversation_id, writing=True)
            db.get(ArtifactRevision, revision_id, populate_existing=True)
            fixed = store.revision(db, latest, revision_id)
            if (fixed.manifest_sha256 != row.manifest_sha256 or fixed.content_sha256 != row.content_sha256
                    or fixed.version != version or latest.kind != kind):
                raise HTTPException(409, "生成期间固定版本发生改变，未保存导出")
            if before_commit is not None:
                before_commit()
            db.add(row)
            db.commit()
    except IntegrityError:
        db.rollback()
        found = _existing(db, key, request_id, request_hash)
        if found is None:
            raise
        if before_commit is not None:
            try:
                with coordinated():
                    before_commit()
            except Exception:
                db.rollback()
                raise
        return public(found)
    except Exception:
        db.rollback()
        raise
    return public(row)


def listing(db, artifact_id, conversation_id=None, *, revision_id=None, offset=0, limit=30):
    artifact = store.get(db, artifact_id, conversation_id)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise HTTPException(422, "导出分页须为非负偏移量，每次最多100项")
    query = db.query(ArtifactExport).options(defer(ArtifactExport.payload)).filter_by(
        artifact_id=artifact.id, conversation_id=conversation_id)
    if revision_id is not None:
        store.revision(db, artifact, revision_id)
        query = query.filter_by(revision_id=revision_id)
    # Query.count() wraps the entity SELECT and can reintroduce the BLOB even
    # when the list itself defers it. Count only the indexed identity instead.
    total = query.with_entities(func.count(ArtifactExport.id)).scalar()
    rows = query.order_by(ArtifactExport.created_at.desc(), ArtifactExport.id.desc()).offset(offset).limit(limit).all()
    return {"items": [public(row) for row in rows], "total": total,
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        "verification": "listing_metadata_only_payload_checked_on_read"}
