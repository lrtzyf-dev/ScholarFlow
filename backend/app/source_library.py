"""Explicit selection to library; immutable originals remain Source revisions.

PDFs use the existing parse/index pipeline. Other formats are indexed as real
source passages, never represented as invented PDF pages or bounding boxes.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4
import hashlib
import json
import threading

from sqlalchemy import select, text

from . import config, source_store as store
from .source_models import Source, SourceRevision, SourceSnapshot, SourceExtraction, SourceLibraryImport, SourceLibraryBinding, SourceLibraryRevisionOrigin
from .source_runtime import conversation_scope, revision_ids
from .source_documents import read_document, add_document_locator, save_extraction
from .source_parsing import Extraction, MEDIA
from .models import Paper, Chunk
from .config import PDF_DIR, PARSED_DIR

_import_lock = threading.RLock()


def _scope_available(db, scope):
    if scope.scope_type != "conversation":
        raise store.SourceAccessDenied("入库操作必须属于当前会话")
    return conversation_scope(db, scope.scope_id)


def _document_chunks(db, record, scope):
    doc = read_document(db, record["revision_id"], scope=scope)
    rid, chunks = record["revision_id"], []
    if not doc["text"].strip():
        raise store.SourceError("资料没有可供索引的文本，请先获取有效文本")
    if len(doc["blocks"]) > 20_000:
        raise store.SourceError("资料结构超过单次文献入库限制，请按章节拆分")
    for block in doc["blocks"]:
        if block.get("retrieval_role") == "compatibility":
            continue
        if not block["text"].strip():
            continue
        if len(block["text"]) > 16_000:
            raise store.SourceError("资料中存在超过16000字的单段或单行，请先拆分后入库")
        if doc["format"] == "word":
            locator = (add_document_locator(db, rid, scope=scope, kind="word_block", block_id=block["block_id"])
                if block["kind"] == "word_part" else add_document_locator(db, rid, scope=scope, kind="word_paragraphs",
                    paragraph_start=block["paragraph_index"], paragraph_end=block["paragraph_index"]))
        elif doc["format"] in ("excel", "csv"):
            locator = add_document_locator(db, rid, scope=scope, kind="sheet_cells",
                sheet=block["sheet"], cell_range=block["cell_range"])
        else:
            locator = store.add_text_locator(db, rid, scope=scope,
                line_start=block["line_start"], line_end=block["line_end"])
        chunks.append(locator)
    return doc, chunks


def _prepare_vectors(locators):
    from .pipeline.indexer import _prepare_embedding_vectors
    return _prepare_embedding_vectors([{"content": item["quote"]} for item in locators])


def _index_document(db, paper, record, scope, prepared):
    from .pipeline.indexer import cjk_spaced
    from .pipeline.vectordb import NumpyVectorStore, current_embedding_fingerprint
    doc, locators, encoded, fingerprint = prepared
    # The caller holds the settings lock through cache publication and DB commit.
    if current_embedding_fingerprint() != fingerprint:
        raise store.SourceError("Embedding 配置已变化，请重新入库；原附件已保留")
    if doc["raw_sha256"] != record["raw_sha256"]:
        raise store.SourceCorruption("文献索引与固定原件版本不一致")
    # Verify every prepared locator after reacquiring the transaction.
    for locator in locators:
        saved = store.read_locator(db, locator["locator_id"], scope=scope)
        if saved["revision_id"] != record["revision_id"] or saved["quote"] != locator["quote"]:
            raise store.SourceCorruption("文献索引定位与固定原件不一致")
    rows, mapping = [], {}
    for i, locator in enumerate(locators):
        row = Chunk(paper_id=paper.id, chunk_index=i, parent_index=i,
                    page=0, bbox=None, section=doc["format"], kind="text",
                    content=locator["quote"], char_count=len(locator["quote"]))
        db.add(row)
        rows.append(row)
    db.flush()
    for row, locator in zip(rows, locators):
        db.execute(text("INSERT INTO chunks_fts(chunk_id,paper_id,content) VALUES (:cid,:pid,:content)"),
            {"cid": row.id, "pid": paper.id, "content": cjk_spaced(row.content)})
        mapping[row.id] = locator["locator_id"]
    if rows:
        vectors = NumpyVectorStore()
        vectors.begin_build(len(rows), encoded.shape[1])
        vectors.fill([row.id for row in rows], [paper.id] * len(rows), encoded)
        vectors.flush()
    # The original is retained in Source; this manifest names its structure,
    # and deliberately supplies no fabricated rendered page dimensions.
    path = PARSED_DIR / f"{paper.id}.json"
    path.write_text(json.dumps({"source_revision_id": record["revision_id"], "format": doc["format"],
        "page_count": 0, "pages": []}, ensure_ascii=False), encoding="utf-8")
    paper.parsed_path, paper.page_count, paper.status, paper.error = str(path), 0, "parsed", ""
    return mapping


def import_selected(db, *, scope, revision_ids: list[str], request_id):
    # Production has one runtime owner. This also prevents two windows from
    # paying to embed the same selection before its final receipt is committed.
    with _import_lock:
        return _import_selected(db, scope=scope, ids=revision_ids, request_id=request_id)


def _import_selected(db, *, scope, ids, request_id):
    try:
        ids = revision_ids(ids)
    except ValueError as exc:
        raise store.SourceError(str(exc)) from None
    if not ids:
        raise store.SourceError("请明确选择至少一个需要加入文献库的来源")
    items = []
    # Finish the API's read authorization transaction before per-item writes.
    db.rollback()
    for rid in ids:
        db.rollback()  # Drop only this helper's completed item's read transaction.
        new_path, new_pid = None, None
        try:
            db.execute(text("BEGIN IMMEDIATE"))
            _scope_available(db, scope)
            record = store.read_revision(db, rid, scope=scope, include_raw=True)
            receipt = db.get(SourceLibraryImport, rid)
            if receipt is not None:
                paper = db.get(Paper, receipt.paper_id)
                if receipt.raw_sha256 != record["raw_sha256"] or paper is None or paper.content_hash != record["raw_sha256"]:
                    raise store.SourceCorruption("文献入库回执与固定原件不一致")
                from .library_sources import ensure_owned
                ensure_owned(db, paper.id, rid)
                db.commit()
                items.append({"revision_id": rid, "paper_id": paper.id, "status": "existing", "index_status": paper.status})
                continue
            paper = db.scalar(select(Paper).where(Paper.content_hash == record["raw_sha256"]).order_by(Paper.created_at, Paper.id))
            existing = paper is not None
            mapping = {}
            if paper is None:
                prepared = None
                if record["media_type"] != "application/pdf":
                    doc, locators = _document_chunks(db, record, scope)
                    db.commit()  # Never hold SQLite's writer while calling an embedding service.
                    encoded, fingerprint = _prepare_vectors(locators)
                    db.execute(text("BEGIN IMMEDIATE"))
                    _scope_available(db, scope)
                    record = store.read_revision(db, rid, scope=scope, include_raw=True)
                    paper = db.scalar(select(Paper).where(Paper.content_hash == record["raw_sha256"]).order_by(Paper.created_at, Paper.id))
                    prepared = (doc, locators, encoded, fingerprint)
                existing = paper is not None
            # Embedding HTTP has completed and released the settings lock. Keep
            # the final check, cache fingerprint and committed receipt consistent.
            with config._settings_lock:
                if paper is None:
                    if prepared is not None:
                        from .pipeline.vectordb import current_embedding_fingerprint
                        if current_embedding_fingerprint() != prepared[3]:
                            raise store.SourceError("Embedding 配置已变化，请重新入库；原附件已保留")
                    paper = Paper(id=uuid4().hex, title=record["title"], content_hash=record["raw_sha256"], status="uploaded")
                    new_pid = paper.id
                    db.add(paper)
                    db.flush()
                    if record["media_type"] == "application/pdf":
                        # Deterministic owned path, never the uploaded filename.
                        new_path = PDF_DIR / f"{paper.id}.pdf"
                        with new_path.open("xb") as file:
                            file.write(record["raw_bytes"])
                        paper.pdf_path = str(new_path)
                    else:
                        mapping = _index_document(db, paper, record, scope, prepared)
                else:
                    other = db.scalar(select(SourceLibraryImport).where(SourceLibraryImport.paper_id == paper.id))
                    if other:
                        # Chunk locators belong to other.revision_id; preserve that
                        # receipt as authority instead of relabeling them as rid.
                        mapping = {}
                db.add(SourceLibraryImport(revision_id=rid, raw_sha256=record["raw_sha256"], paper_id=paper.id,
                                           request_id=request_id, chunk_locators=mapping))
                db.flush()
                from .library_sources import ensure_owned
                ensure_owned(db, paper.id, rid)
                pid, media = paper.id, record["media_type"]
                db.commit()
            if not existing and media == "application/pdf":
                from .tasks import schedule_parse
                schedule_parse(pid)
                db.expire_all()
                paper = db.get(Paper, pid)
            items.append({"revision_id": rid, "paper_id": pid, "status": "existing" if existing else "imported",
                          "index_status": paper.status})
        except Exception as exc:
            db.rollback()
            # A committed paper owns its file even when background scheduling
            # failed; never delete it on this failure path.
            if new_path is not None and db.get(Paper, new_path.stem) is None:
                new_path.unlink(missing_ok=True)
                db.rollback()
            if new_pid is not None and db.get(Paper, new_pid) is None:
                db.rollback()
                from .pipeline.vectordb import NumpyVectorStore
                NumpyVectorStore().remove_paper(new_pid)
                (PARSED_DIR / f"{new_pid}.json").unlink(missing_ok=True)
            message = str(exc) if isinstance(exc, store.SourceError) else "资料入库未完成，原附件和其他选项已保留"
            items.append({"revision_id": rid, "status": "failed", "error": message})
    return {"items": items}


def library_metadata(db, paper_id):
    return library_metadata_many(db, [paper_id]).get(paper_id, {})


def library_metadata_many(db, paper_ids):
    """List metadata must never materialize original bytes or full extractions."""
    from sqlalchemy import func
    if not paper_ids:
        return {}
    bindings=dict(db.execute(select(SourceLibraryBinding.paper_id,SourceLibraryBinding.revision_id)
        .where(SourceLibraryBinding.paper_id.in_(paper_ids))).all())
    candidates=db.execute(select(SourceLibraryImport.paper_id,SourceLibraryImport.revision_id)
        .where(SourceLibraryImport.paper_id.in_(paper_ids)).order_by(SourceLibraryImport.created_at,SourceLibraryImport.revision_id)).all()
    chosen={}
    for pid,rid in candidates:
        if pid in bindings:
            if bindings[pid]==rid: chosen[pid]=rid
        elif pid not in chosen: chosen[pid]=rid
    rows=db.execute(select(SourceRevision.id,Source.id,Source.scope_type,Source.scope_id,SourceSnapshot.media_type,
        func.json_extract(SourceExtraction.document,'$.format'))
        .select_from(SourceRevision).outerjoin(Source,Source.id==SourceRevision.source_id)
        .outerjoin(SourceSnapshot,SourceSnapshot.id==SourceRevision.snapshot_id)
        .outerjoin(SourceExtraction,SourceExtraction.revision_id==SourceRevision.id)
        .where(SourceRevision.id.in_(chosen.values()))).all() if chosen else []
    metadata={rid:{"source_revision_id":rid,"source_id":sid,"source_conversation_id":scope_id if scope_type=='conversation' else None,
        "source_media_type":media,"document_format":fmt} for rid,sid,scope_type,scope_id,media,fmt in rows}
    return {pid:metadata.get(rid,{"source_revision_id":rid}) for pid,rid in chosen.items()}


def open_library_source(db, paper_id, conversation_id, library_revision_id=None):
    """Global library access creates an explicit alias inside the reader scope."""
    from .library_sources import ensure_owned, copy_record, copy_locator
    scope = conversation_scope(db, conversation_id)
    record, receipt = ensure_owned(db, paper_id)
    origin_scope = store.SourceScope("library", paper_id)
    origin = "library:" + paper_id + ":" + record["revision_id"]
    if library_revision_id and library_revision_id != record["revision_id"]:
        selected = db.get(SourceLibraryImport, library_revision_id)
        if selected is None or selected.paper_id != paper_id:
            raise store.SourceNotFound("此固定版本不属于所选文献")
        record = store.read_revision(db, library_revision_id, scope=origin_scope, include_raw=True)
        receipt = selected
        origin = "library:" + paper_id + ":" + library_revision_id
    alias = db.scalar(select(SourceRevision).join(Source, Source.id == SourceRevision.source_id).where(
        Source.scope_type == scope.scope_type, Source.scope_id == scope.scope_id,
        Source.kind == "library", SourceRevision.origin_ref == origin))
    if alias:
        result = store.read_revision(db, alias.id, scope=scope, include_raw=True)
        from .library_sources import equivalent_extraction
        if not equivalent_extraction(db, result, record):
            raise store.SourceCorruption("会话文献别名与原件内容不一致")
    else:
        result = copy_record(db, record, origin_scope, scope, origin)
    if db.get(SourceLibraryImport, result["revision_id"]) is None:
        mapping={chunk:copy_locator(db,loc,origin_scope,result["revision_id"],scope)["locator_id"]
                 for chunk,loc in (receipt.chunk_locators or {}).items()}
        db.add(SourceLibraryImport(revision_id=result["revision_id"],raw_sha256=result["raw_sha256"],paper_id=paper_id,
            request_id="library-alias-"+result["revision_id"],chunk_locators=mapping))
        db.flush()
    origin_mapping=db.get(SourceLibraryRevisionOrigin,result["revision_id"])
    if origin_mapping is None:
        db.add(SourceLibraryRevisionOrigin(origin_revision_id=result["revision_id"],library_revision_id=record["revision_id"],paper_id=paper_id))
        db.flush()
    elif origin_mapping.library_revision_id!=record["revision_id"] or origin_mapping.paper_id!=paper_id:
        raise store.SourceCorruption("会话别名的库版本映射不一致")
    return result, receipt


def augment_library_evidence(db, evidences, conversation_id):
    if not conversation_id:
        return evidences
    for evidence in evidences:
        from .library_index_models import LibraryIndexReceipt
        from .library_index_store import checked_receipt
        published = next((checked_receipt(db, row.id) for row in db.scalars(select(LibraryIndexReceipt).where(
            LibraryIndexReceipt.paper_id == evidence.get("paper_id"))) if evidence.get("chunk_id") in row.chunk_locators), None)
        if published:
            owner_rid, owner_mapping, pid = published.revision_id, published.chunk_locators, published.paper_id
            owner_scope = store.SourceScope("library", pid)
            store.read_revision(db, owner_rid, scope=owner_scope)
            locator = store.read_locator(db, owner_mapping[evidence["chunk_id"]], scope=owner_scope)
            if locator["revision_id"] != owner_rid or locator["location"].get("extraction_fingerprint") != published.source_fingerprint:
                raise store.SourceCorruption("检索证据不属于其索引固定版本")
            record, _ = open_library_source(db, pid, conversation_id, library_revision_id=owner_rid)
            location = dict(locator["location"])
            location["extraction_fingerprint"] = read_document(db, record["revision_id"], scope=conversation_scope(db, conversation_id))["fingerprint"]
            copied = store._save_locator(db, record["revision_id"], locator["kind"], location, locator["quote"])
            evidence.update(source_id=record["source_id"], revision_id=record["revision_id"], locator_id=copied["locator_id"],
                source_title=record["title"], source_kind="library", source_location=location, source_version=record["version"],
                source_media_type=record["media_type"], source_sha256=record["raw_sha256"], locator_kind=copied["kind"],
                quote=copied["quote"], page=location.get("page_number", 0), bbox=location.get("bbox"))
            continue
        receipt = db.scalar(select(SourceLibraryImport).where(SourceLibraryImport.paper_id == evidence.get("paper_id")))
        if receipt is None:
            continue
        # Find the receipt that actually owns the chunk map when identical
        # content was explicitly selected from several conversations.
        owner = next((row for row in db.scalars(select(SourceLibraryImport).where(
            SourceLibraryImport.paper_id == receipt.paper_id)).all() if evidence.get("chunk_id") in (row.chunk_locators or {})), None)
        if owner is None:
            continue  # Native PDF pipeline already supplies genuine page/bbox.
        from .library_sources import ensure_owned
        from .library_sources import scope_of
        fixed = (store.read_revision(db, owner.revision_id, scope=scope_of(db, owner.revision_id))
            if scope_of(db, owner.revision_id).scope_type == "library" else ensure_owned(db, receipt.paper_id, owner.revision_id)[0])
        record, _ = open_library_source(db, receipt.paper_id, conversation_id, library_revision_id=fixed["revision_id"])
        old_revision = db.get(SourceRevision, owner.revision_id)
        old_source = db.get(Source, old_revision.source_id)
        locator = store.read_locator(db, owner.chunk_locators[evidence["chunk_id"]],
            scope=store.SourceScope(old_source.scope_type, old_source.scope_id))
        location = dict(locator["location"])
        if location.get("extraction_fingerprint"):
            location["extraction_fingerprint"] = read_document(db, record["revision_id"],
                scope=store.SourceScope("conversation", conversation_id))["fingerprint"]
        copied = store._save_locator(db, record["revision_id"], locator["kind"], location, locator["quote"])
        evidence.update(source_id=record["source_id"], revision_id=record["revision_id"],
            locator_id=copied["locator_id"], source_title=record["title"], source_kind="library",
            source_location=location, source_version=record["version"],
            source_media_type=record["media_type"], source_sha256=record["raw_sha256"],
            locator_kind=copied["kind"], quote=copied["quote"], page=0, bbox=None)
    return evidences
