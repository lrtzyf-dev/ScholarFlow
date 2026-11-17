"""Short SQLite transactions for explicit reading/index version selection."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import numpy as np
from sqlalchemy import delete, select, text, update

from . import source_store as sources
from .library_index_models import (LibraryIndexState, LibraryIndexOperation, LibraryIndexReceipt,
                                   LibraryIndexVector, LibraryChunkArchive, LibraryIndexPreparedBatch)
from .library_sources import ensure_owned, scope_of
from .models import Paper, Chunk
from .source_models import SourceLibraryBinding, SourceLibraryImport
from .source_documents import read_document
from .source_runtime import conversation_scope
from .timeutil import utcnow


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def checked_version(db, paper_id, revision_id):
    paper = db.get(Paper, paper_id, populate_existing=True)
    receipt = db.get(SourceLibraryImport, revision_id)
    if not paper or not receipt or receipt.paper_id != paper_id:
        raise sources.SourceNotFound("所选解析版本不属于此文献")
    scope = sources.SourceScope("library", paper_id)
    record = sources.read_revision(db, revision_id, scope=scope)
    doc = read_document(db, revision_id, scope=scope)
    if record["raw_sha256"] != paper.content_hash or doc["raw_sha256"] != paper.content_hash:
        raise sources.SourceConflict("文献原件已改变；不能将其他原件的解析绑定到此文献")
    return paper, record, doc


def receipt_document(row):
    return {key: getattr(row, key) for key in ("id", "paper_id", "revision_id", "source_fingerprint", "raw_sha256",
            "index_mode", "embedding_fingerprint", "dimension", "chunk_locators")}


def checked_receipt(db, ident):
    row = db.get(LibraryIndexReceipt, ident, populate_existing=True)
    if row is None or digest(receipt_document(row)) != row.fingerprint:
        raise sources.SourceCorruption("已发布索引回执完整性检查失败；未替换历史证据")
    return row


def query_index_identity(db, paper_ids=None):
    """Stable read-only identity for cache fencing; grants no source access.

    The caller separately fingerprints legacy Chunk/FTS/numpy bytes and handles
    authorization. Only published generations exclude a paper's numpy rows;
    a reading-only version choice still invalidates conservatively via version.
    """
    query = select(LibraryIndexState.paper_id, LibraryIndexState.version,
        LibraryIndexState.indexed_revision_id, LibraryIndexState.current_build_id)
    if paper_ids is not None:
        if isinstance(paper_ids, str):
            raise sources.SourceError("索引身份范围须为文献编号集合")
        query = query.where(LibraryIndexState.paper_id.in_(sorted(set(paper_ids))))
    pointers, managed = [], []
    for paper_id, version, revision_id, build_id in db.execute(query.order_by(LibraryIndexState.paper_id)):
        receipt = checked_receipt(db, build_id) if build_id else None
        if receipt and (receipt.paper_id != paper_id or receipt.revision_id != revision_id):
            raise sources.SourceCorruption("当前索引指针与固定版本回执不一致")
        pointers.append({"paper_id": paper_id, "version": version, "indexed_revision_id": revision_id,
            "current_build_id": build_id, "receipt_fingerprint": receipt.fingerprint if receipt else None})
        if build_id: managed.append(paper_id)
    return {"pointers": pointers, "managed_paper_ids": managed, "fingerprint": digest(pointers)}


def _legacy_index_revision(db, paper_id):
    ids = set(db.scalars(select(Chunk.id).where(Chunk.paper_id == paper_id)))
    if not ids:
        return None
    for receipt in db.scalars(select(SourceLibraryImport).where(SourceLibraryImport.paper_id == paper_id)):
        if ids <= set(receipt.chunk_locators or {}) and scope_of(db, receipt.revision_id).scope_type == "library":
            return receipt.revision_id
    return None  # A native/legacy index without a parse-version receipt is unknown.


def state(db, paper_id):
    paper = db.get(Paper, paper_id, populate_existing=True)
    binding = db.get(SourceLibraryBinding, paper_id, populate_existing=True)
    if paper is None or binding is None:
        raise sources.SourceNotFound("请先打开文献固定版本，建立独立原件后再管理索引")
    row = db.get(LibraryIndexState, paper_id, populate_existing=True)
    receipt = checked_receipt(db, row.current_build_id) if row and row.current_build_id else None
    if receipt and (receipt.paper_id != paper_id or receipt.revision_id != row.indexed_revision_id):
        raise sources.SourceCorruption("当前索引指针与固定版本回执不一致")
    from .pipeline.vectordb import current_embedding_fingerprint
    return {"paper_id": paper_id, "version": row.version if row else 0, "primary_revision_id": binding.revision_id,
        "indexed_revision_id": row.indexed_revision_id if row else _legacy_index_revision(db, paper_id),
        "current_build_id": row.current_build_id if row else None,
        "index_mode": receipt.index_mode if receipt else "legacy_unknown",
        "embedding_fingerprint": receipt.embedding_fingerprint if receipt else None,
        "embedding_matches_current": bool(receipt and receipt.index_mode == "semantic"
            and receipt.embedding_fingerprint == current_embedding_fingerprint()),
        "dimension": receipt.dimension if receipt else None,
        "chunk_count": len(receipt.chunk_locators) if receipt else len(list(db.scalars(select(Chunk.id).where(Chunk.paper_id == paper_id)))),
        "old_evidence_policy": "fixed_original_revision_and_archived_chunk"}


def _cas(db, paper_id, expected_version, expected_primary_id):
    current = state(db, paper_id)
    if current["version"] != expected_version or current["primary_revision_id"] != expected_primary_id:
        raise sources.SourceConflict("文献默认版本或索引已被其他窗口修改；请重新载入，已准备结果不会重复计费")
    return current


def _advance_state(db, paper_id, current, *, indexed_revision_id=None, current_build_id=None, change_index=False):
    row = db.get(LibraryIndexState, paper_id)
    values = {"version": current["version"] + 1, "updated_at": utcnow()}
    if change_index:
        values.update(indexed_revision_id=indexed_revision_id, current_build_id=current_build_id)
    if row is None:
        if not change_index:
            values["indexed_revision_id"] = current["indexed_revision_id"]
        db.add(LibraryIndexState(paper_id=paper_id, **values))
        db.flush()
    else:
        result = db.execute(update(LibraryIndexState).where(LibraryIndexState.paper_id == paper_id,
            LibraryIndexState.version == current["version"]).values(**values), execution_options={"synchronize_session": False})
        if result.rowcount != 1:
            raise sources.SourceConflict("索引版本发生并发冲突，原索引已保留")
    db.expire_all()


def select_primary(db, paper_id, revision_id, *, conversation_id, expected_version, expected_primary_id):
    conversation_scope(db, conversation_id)
    checked_version(db, paper_id, revision_id)
    current = _cas(db, paper_id, expected_version, expected_primary_id)
    if revision_id == current["primary_revision_id"]:
        return current
    result = db.execute(update(SourceLibraryBinding).where(SourceLibraryBinding.paper_id == paper_id,
        SourceLibraryBinding.revision_id == expected_primary_id).values(revision_id=revision_id),
        execution_options={"synchronize_session": False})
    if result.rowcount != 1:
        raise sources.SourceConflict("默认版本已改变，未覆盖另一窗口的选择")
    _advance_state(db, paper_id, current)
    return state(db, paper_id)


def create_operation(db, paper_id, revision_id, *, conversation_id, request_id, expected_version,
                     expected_primary_id, set_primary, index_mode):
    conversation_scope(db, conversation_id)
    _, record, doc = checked_version(db, paper_id, revision_id)
    payload = digest([conversation_id, revision_id, expected_version, expected_primary_id, bool(set_primary), index_mode])
    existing = db.scalar(select(LibraryIndexOperation).where(LibraryIndexOperation.paper_id == paper_id,
        LibraryIndexOperation.request_id == request_id))
    if existing:
        if existing.request_sha256 != payload:
            raise sources.SourceConflict("同一索引请求编号不能用于不同的版本或选项")
        return existing, False
    _cas(db, paper_id, expected_version, expected_primary_id)
    if index_mode not in ("text", "semantic"):
        raise sources.SourceError("请选择仅全文检索或全文与向量检索")
    if not doc["text"].strip():
        raise sources.SourceError("所选解析没有可检索文字；图片或扫描件需先经授权解析")
    active = db.scalar(select(LibraryIndexOperation.id).where(LibraryIndexOperation.paper_id == paper_id,
        LibraryIndexOperation.status.in_(("queued", "preparing", "prepared"))))
    if active:
        raise sources.SourceConflict("此文献已有待完成索引准备，请先继续提交或取消该任务")
    from .pipeline.vectordb import current_embedding_fingerprint
    ident = uuid4().hex
    row = LibraryIndexOperation(id=ident, run_id="library-index:" + ident, paper_id=paper_id,
        conversation_id=conversation_id, request_id=request_id, request_sha256=payload,
        target_revision_id=revision_id, target_fingerprint=doc["fingerprint"], raw_sha256=record["raw_sha256"],
        expected_version=expected_version, expected_primary_id=expected_primary_id, set_primary=int(set_primary),
        index_mode=index_mode, embedding_fingerprint=current_embedding_fingerprint() if index_mode == "semantic" else None)
    db.add(row); db.flush()
    return row, True


def checked_operation(db, ident, conversation_id, *, writing=False):
    conversation_scope(db, conversation_id, writing=writing)
    row = db.get(LibraryIndexOperation, ident, populate_existing=True)
    if row is None or row.conversation_id != conversation_id:
        raise sources.SourceNotFound("当前会话没有此索引任务")
    return row


def authorize(db, op, *, check_cas=True):
    conversation_scope(db, op.conversation_id)
    if op.status in ("cancelled", "done"):
        raise sources.SourceConflict("索引任务已取消或提交，不能重复执行")
    _, record, doc = checked_version(db, op.paper_id, op.target_revision_id)
    if doc["fingerprint"] != op.target_fingerprint or record["raw_sha256"] != op.raw_sha256:
        raise sources.SourceCorruption("准备的解析版本已经损坏，未修改原索引")
    if op.index_mode == "semantic":
        from .pipeline.vectordb import current_embedding_fingerprint
        from .pipeline.embeddings import embedding_connection
        if op.embedding_fingerprint != current_embedding_fingerprint():
            raise sources.SourceConflict("Embedding 模型或凭证已改变，旧准备结果不会发布")
        from .config import settings
        if settings.embedding_mode in ("api", "litellm"):
            embedding_connection()  # Includes credential revocation checks, without HTTP.
    if check_cas:
        _cas(db, op.paper_id, op.expected_version, op.expected_primary_id)
    return record, doc


def operation_output(op):
    return {"id": op.id, "run_id": op.run_id, "paper_id": op.paper_id, "conversation_id": op.conversation_id,
        "target_revision_id": op.target_revision_id, "status": op.status, "error": op.error,
        "index_mode": op.index_mode, "embedding_fingerprint": op.embedding_fingerprint,
        "dimension": op.dimension, "prepared_count": len((op.prepared or {}).get("locator_ids", [])),
        "prepared_result_available": op.status == "prepared" and bool((op.prepared or {}).get("locator_ids")),
        "expected_version": op.expected_version, "expected_primary_id": op.expected_primary_id,
        "set_primary": bool(op.set_primary), "published_build_id": op.published_build_id,
        "updated_at": op.updated_at.isoformat() + "Z"}


def _archive_current_chunks(db, paper_id):
    for chunk in db.scalars(select(Chunk).where(Chunk.paper_id == paper_id)):
        if db.get(LibraryChunkArchive, chunk.id):
            continue
        document = {k: getattr(chunk, k) for k in ("id", "paper_id", "chunk_index", "parent_index", "page", "bbox", "section", "kind", "content", "char_count")}
        vector = db.get(LibraryIndexVector, chunk.id)
        document.update(archive_scope="historical_index_chunk", authorizes_actions=False)
        db.add(LibraryChunkArchive(chunk_id=chunk.id, paper_id=paper_id, document=document,
            fingerprint=digest(document), vector_data=vector.data if vector else None,
            vector_fingerprint=vector.sha256 if vector else None))


def publish(db, op):
    record, doc = authorize(db, op)
    current = _cas(db, op.paper_id, op.expected_version, op.expected_primary_id)
    ids = (op.prepared or {}).get("locator_ids", [])
    if not ids or (op.prepared or {}).get("fingerprint") != digest(ids):
        raise sources.SourceCorruption("索引准备目录校验失败，原索引已保留")
    vectors = None
    if op.index_mode == "semantic":
        raw = op.vectors or b""
        if (not op.dimension or len(raw) != len(ids) * op.dimension * 4
                or hashlib.sha256(raw).hexdigest() != op.vector_sha256):
            raise sources.SourceCorruption("索引准备向量校验失败，原索引已保留")
        vectors = np.frombuffer(raw, dtype="<f4").reshape(len(ids), op.dimension)
        if not np.isfinite(vectors).all():
            raise sources.SourceCorruption("索引向量包含无效数值")
    scope = sources.SourceScope("library", op.paper_id)
    locators = [sources.read_locator(db, ident, scope=scope) for ident in ids]
    if any(l["revision_id"] != op.target_revision_id or l["location"].get("extraction_fingerprint") != doc["fingerprint"] for l in locators):
        raise sources.SourceCorruption("索引准备位置不属于所选解析版本")
    _archive_current_chunks(db, op.paper_id)
    db.execute(delete(LibraryIndexVector).where(LibraryIndexVector.paper_id == op.paper_id))
    db.execute(delete(Chunk).where(Chunk.paper_id == op.paper_id))
    db.execute(text("DELETE FROM chunks_fts WHERE paper_id=:pid"), {"pid": op.paper_id})
    from .pipeline.indexer import cjk_spaced
    mapping = {}
    for i, locator in enumerate(locators):
        location = locator["location"]
        chunk = Chunk(paper_id=op.paper_id, chunk_index=i, parent_index=i, page=location.get("page_number", 0),
            bbox=location.get("bbox"), section=doc["format"], kind="text", content=locator["quote"], char_count=len(locator["quote"]))
        db.add(chunk); db.flush()
        db.execute(text("INSERT INTO chunks_fts(chunk_id,paper_id,content) VALUES (:cid,:pid,:content)"),
            {"cid": chunk.id, "pid": op.paper_id, "content": cjk_spaced(chunk.content)})
        mapping[chunk.id] = locator["locator_id"]
        if vectors is not None:
            vector = vectors[i].astype("<f4").tobytes()
            db.add(LibraryIndexVector(chunk_id=chunk.id, paper_id=op.paper_id, build_id=op.id,
                dimension=op.dimension, data=vector, sha256=hashlib.sha256(vector).hexdigest()))
    receipt = LibraryIndexReceipt(id=op.id, paper_id=op.paper_id, revision_id=op.target_revision_id,
        source_fingerprint=doc["fingerprint"], raw_sha256=record["raw_sha256"], index_mode=op.index_mode,
        embedding_fingerprint=op.embedding_fingerprint, dimension=op.dimension, chunk_locators=mapping)
    receipt.fingerprint = digest(receipt_document(receipt)); db.add(receipt)
    paper = db.get(Paper, op.paper_id)
    paper.status, paper.error = "parsed", ""
    if op.set_primary:
        result = db.execute(update(SourceLibraryBinding).where(SourceLibraryBinding.paper_id == op.paper_id,
            SourceLibraryBinding.revision_id == op.expected_primary_id).values(revision_id=op.target_revision_id),
            execution_options={"synchronize_session": False})
        if result.rowcount != 1: raise sources.SourceConflict("默认解析版本发生并发冲突")
    _advance_state(db, op.paper_id, current, indexed_revision_id=op.target_revision_id, current_build_id=op.id, change_index=True)
    op = db.get(LibraryIndexOperation, op.id)
    op.status, op.published_build_id, op.error = "done", receipt.id, ""
    op.vectors, op.updated_at = None, utcnow()
    db.execute(delete(LibraryIndexPreparedBatch).where(LibraryIndexPreparedBatch.operation_id == op.id))
    db.flush()
    return operation_output(op)
