"""Explicit authorized project-file capture, without mutable path references."""
from __future__ import annotations

import hashlib
import json
import os

from sqlalchemy import select, text

from . import workspace, source_store as store
from .source_models import SourceCaptureReceipt, SourceRevision
from .source_parsing import MEDIA, extract
from .source_documents import save_extraction
from .source_runtime import conversation_scope


def _project(db, req):
    conversation_scope(db, req["conversation_id"])
    workspace.check_revision(db, req["conversation_id"], req["workspace_revision"])
    project = workspace.conversation_project(db, req["conversation_id"])
    if project is None or project.id != req["project_id"]:
        workspace.fail("会话项目已变化，请重新载入后选择本地资料", 409)
    scope = workspace.project_scope(project)
    workspace.root_path(scope)  # Recheck physical identity after parsing too.
    return scope


def capture_project(db, req):
    scope = _project(db, req)
    path = workspace.scoped_path(scope, req["path"])
    extension = path.suffix[1:].casefold()
    if extension not in MEDIA:
        raise store.SourceError("本地资料支持PDF、TXT、Markdown、CSV、DOCX与XLSX")
    try:
        with path.open("rb") as file:
            workspace._check_handle(file, scope, path)
            before = os.fstat(file.fileno())
            if before.st_size > store.MAX_RAW_BYTES:
                raise store.SourceError("本地资料超过100MiB，请拆分后添加")
            raw = file.read(store.MAX_RAW_BYTES + 1)
            after = os.fstat(file.fileno())
            workspace._check_handle(file, scope, path)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise store.SourceConflict("读取期间本地文件已变化，未保存混合版本")
    except OSError:
        raise store.SourceError("无法读取项目内所选原件") from None
    db.rollback()
    extracted, error = None, None
    try:
        extracted = extract(raw, MEDIA[extension])
    except store.SourceError as exc:
        error = str(exc)
    payload = [req["project_id"], req["workspace_revision"],
        workspace.relative_path(req["path"]), hashlib.sha256(raw).hexdigest()]
    source_id, expected_version = req.get("source_id"), req.get("expected_version")
    if (source_id is None) != (expected_version is None):
        raise store.SourceError("更新项目来源须同时指定来源ID和已读取的版本号")
    if source_id is not None:
        payload.extend([source_id, expected_version])
    digest = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
    db.execute(text("BEGIN IMMEDIATE"))
    _project(db, req)
    authorization_scope = conversation_scope(db, req["conversation_id"])
    origin = f'project:{req["project_id"]}:{workspace.relative_path(req["path"])}'
    if source_id is not None:
        current = store.current_source(db, source_id, scope=authorization_scope)
        if current["kind"] != "local" or current["origin_ref"] != origin:
            raise store.SourceConflict("此来源不属于所选项目文件，请添加为新来源")
    prior = db.scalar(select(SourceCaptureReceipt).where(SourceCaptureReceipt.scope_type == "conversation",
        SourceCaptureReceipt.scope_id == req["conversation_id"], SourceCaptureReceipt.request_id == req["request_id"]))
    if prior is not None:
        if prior.payload_sha256 != digest:
            raise store.SourceConflict("此选择请求已绑定其他文件内容，请用新的请求编号")
        result = store.read_revision(db, prior.revision_id, scope=authorization_scope)
        revision = db.get(SourceRevision, prior.revision_id)
        if (not prior.revision_fingerprint or revision.fingerprint != prior.revision_fingerprint
                or result["kind"] != "local" or result["authorization_id"] != "project:" + req["request_id"]
                or result["origin_ref"] != origin or result["raw_sha256"] != hashlib.sha256(raw).hexdigest()
                or (source_id is not None and (result["source_id"] != source_id or result["version"] != expected_version + 1))):
            raise store.SourceCorruption("本地来源选择回执与固定版本不一致")
        result["replayed"] = True
    else:
        result = store.capture_source(db, scope=authorization_scope,
            authorization=store.SourceAuthorization(authorization_scope, "project:" + req["request_id"], "local"),
            kind="local", title=path.name, raw_bytes=raw, text=extracted.text if extracted else None,
            media_type=MEDIA[extension], origin_ref=origin,
            text_origin="parsed_text" if extracted else "none", parser=extracted.parser if extracted else None,
            source_id=source_id, expected_version=expected_version)
        if extracted:
            save_extraction(db, result["revision_id"], scope=authorization_scope, extraction=extracted)
        db.add(SourceCaptureReceipt(scope_type="conversation", scope_id=req["conversation_id"],
            request_id=req["request_id"], payload_sha256=digest, revision_id=result["revision_id"],
            revision_fingerprint=db.get(SourceRevision, result["revision_id"]).fingerprint))
        result["replayed"] = False
    from .api.sources import _metadata
    metadata = _metadata(result, db)
    if error:
        metadata["parse_error"] = error
    if source_id:
        from .source_update_store import consume_if_matching
        consume_if_matching(db,authorization_scope,source_id,req["request_id"],result["raw_sha256"])
    db.commit()
    return metadata
