"""Recoverable, metered preparation; only publication changes the live index."""
from __future__ import annotations

import hashlib
import threading

import numpy as np
from sqlalchemy import select, text

from . import library_index_store as store, source_store as sources
from .db import SessionLocal
from .library_index_models import LibraryIndexOperation, LibraryIndexPreparedBatch
from .runtime_context import bind_runtime_config
from .timeutil import utcnow

_guard = threading.RLock()
_threads = {}
_slots = threading.Semaphore(2)
_stopping = threading.Event()


def _authorized(ident):
    if _stopping.is_set():
        raise sources.SourceConflict("应用正在退出，索引准备已保存，可在重启后继续")
    with SessionLocal() as db:
        op = db.get(LibraryIndexOperation, ident)
        if op is None: raise sources.SourceAccessDenied("发起会话或索引任务已删除")
        store.authorize(db, op)
    return True


def _batch_saved(ident, start, encoded, fingerprint):
    data = encoded.astype("<f4").tobytes()
    with SessionLocal.begin() as db:
        op = db.get(LibraryIndexOperation, ident)
        if op is None or op.status == "cancelled": return
        if fingerprint != op.embedding_fingerprint:
            raise sources.SourceConflict("Embedding 准备配置与已确认任务不一致")
        old = db.get(LibraryIndexPreparedBatch, (ident, start))
        value = hashlib.sha256(data).hexdigest()
        if old and old.sha256 != value:
            raise sources.SourceCorruption("相同准备批次不能覆盖成不同向量")
        if old is None:
            db.add(LibraryIndexPreparedBatch(operation_id=ident, start=start, count=len(encoded),
                dimension=encoded.shape[1], embedding_fingerprint=fingerprint, data=data, sha256=value))
        op.updated_at = utcnow()


def _cached_batches(db, op, count):
    from .pipeline.indexer import EMBED_BATCH_SIZE
    result = {}
    for row in db.scalars(select(LibraryIndexPreparedBatch).where(LibraryIndexPreparedBatch.operation_id == op.id)):
        if (row.embedding_fingerprint != op.embedding_fingerprint or row.start % EMBED_BATCH_SIZE or row.start < 0
                or row.count != min(EMBED_BATCH_SIZE, count - row.start) or row.count <= 0 or row.dimension <= 0
                or len(row.data) != row.count * row.dimension * 4 or hashlib.sha256(row.data).hexdigest() != row.sha256):
            raise sources.SourceCorruption("保留的索引准备批次校验失败，未重复发起付费请求")
        values = np.frombuffer(row.data, dtype="<f4").reshape(row.count, row.dimension)
        if not np.isfinite(values).all(): raise sources.SourceCorruption("保留的向量包含无效数值")
        result[row.start] = values
    return result


def _prepare_locators(db, op):
    record, doc = store.authorize(db, op)
    scope = sources.SourceScope("library", op.paper_id)
    if doc["format"] != "pdf":
        from .source_library import _document_chunks
        _, locators = _document_chunks(db, record, scope)
        # Text locators predate structured fingerprints. Add a new bound
        # locator rather than modifying any old locator or extraction.
        locators = [sources._save_locator(db, op.target_revision_id, loc["kind"],
            {**loc["location"], "extraction_fingerprint": doc["fingerprint"], "text_sha256": doc["text_sha256"]}, loc["quote"])
            for loc in locators]
    else:
        locators = []
        for block in doc["blocks"]:
            if not block["text"].strip(): continue
            if not block.get("geometry_verified") or len(block["text"]) > 32_000:
                raise sources.SourceError("PDF页面缺少可验证文字位置或文字过长，请先按页核对解析结果")
            location = {k: v for k, v in block.items() if k not in ("kind", "text")}
            location.update(raw_sha256=doc["raw_sha256"], text_sha256=doc["text_sha256"],
                extraction_fingerprint=doc["fingerprint"], page_count=len(doc["pages"]), precision="page")
            locators.append(sources._save_locator(db, op.target_revision_id, "pdf_page", location, block["text"]))
    if not locators: raise sources.SourceError("此解析没有可检索的提取文字")
    ids = [loc["locator_id"] for loc in locators]
    if op.prepared and op.prepared.get("locator_ids") != ids:
        raise sources.SourceCorruption("固定版本的索引准备目录已变化，未重新调用模型")
    op.prepared = {"locator_ids": ids, "fingerprint": store.digest(ids)}
    op.status, op.error, op.updated_at = "preparing", "", utcnow()
    return locators


def execute(ident):
    """Also callable synchronously by isolated tests; no provider is faked here."""
    with SessionLocal() as db:
        op = db.get(LibraryIndexOperation, ident)
        if op is None or op.status in ("done", "cancelled"): return
        _authorized(ident)
        db.rollback(); db.execute(text("BEGIN IMMEDIATE"))
        op = db.get(LibraryIndexOperation, ident, populate_existing=True)
        if op.status == "prepared" and op.prepared:
            db.rollback()
        else:
            locators = _prepare_locators(db, op)
            binding = {"run_id": op.run_id, "conversation_id": op.conversation_id}
            mode, fingerprint = op.index_mode, op.embedding_fingerprint
            cached = _cached_batches(db, op, len(locators)) if mode == "semantic" else {}
            db.commit()  # No writer survives any Embedding call or admission wait.
            if mode == "semantic":
                from .pipeline.indexer import _prepare_embedding_vectors
                with bind_runtime_config(binding):
                    vectors, actual = _prepare_embedding_vectors([{"content": loc["quote"]} for loc in locators],
                        authorization_check=lambda: _authorized(ident), prepared_batches=cached,
                        on_batch=lambda start, values, fp: _batch_saved(ident, start, values, fp))
                if actual != fingerprint: raise sources.SourceConflict("Embedding配置已改变，原索引已保留")
                raw, dimension = vectors.astype("<f4").tobytes(), vectors.shape[1]
            else:
                raw, dimension = None, 0
            db.execute(text("BEGIN IMMEDIATE"))
            op = db.get(LibraryIndexOperation, ident, populate_existing=True)
            if op is None or op.status == "cancelled": db.rollback(); return
            # Retain fully paid results before CAS/authorization revalidation.
            op.vectors, op.dimension = raw, dimension
            op.vector_sha256 = hashlib.sha256(raw).hexdigest() if raw is not None else None
            op.status, op.updated_at = "prepared", utcnow(); db.commit()
        _authorized(ident)
        db.execute(text("BEGIN IMMEDIATE"))
        op = db.get(LibraryIndexOperation, ident, populate_existing=True)
        from . import config
        with config._settings_lock:
            store.publish(db, op)
            db.commit()  # chunks, FTS, vectors, receipt and selected version publish together.


def mark_error(ident, exc):
    with SessionLocal.begin() as db:
        op = db.get(LibraryIndexOperation, ident)
        if op is None or op.status in ("done", "cancelled"): return
        if op.status != "prepared":
            count = len(list(db.scalars(select(LibraryIndexPreparedBatch.start).where(LibraryIndexPreparedBatch.operation_id == ident))))
            op.status = "partial" if count else "interrupted"
        op.error = str(exc) if isinstance(exc, sources.SourceError) else "索引尚未提交，原索引与已准备批次已保留；请核对模型配置、授权或存储后重试"
        op.updated_at = utcnow()


def _worker(ident):
    try:
        from . import maintenance
        with maintenance.activity():
            while not _slots.acquire(timeout=.1):
                if _stopping.is_set(): raise sources.SourceConflict("应用退出，排队索引任务已保留")
            try: execute(ident)
            finally: _slots.release()
    except Exception as exc:
        try: mark_error(ident, exc)
        except Exception: pass  # The durable pre-publication state still preserves the old index.
    finally:
        with _guard: _threads.pop(ident, None)


def start(ident):
    with _guard:
        if ident in _threads: return
        if _stopping.is_set(): raise sources.SourceConflict("应用正在退出，请重新打开后继续")
        thread = threading.Thread(target=_worker, args=(ident,), daemon=True, name="library-index-" + ident[:8])
        _threads[ident] = thread
        try:
            thread.start()
        except Exception:
            _threads.pop(ident, None)
            error = sources.SourceConflict("后台准备未能启动，任务已保存；可核对后继续，原索引保持不变")
            mark_error(ident, error)
            raise error from None


def active():
    with _guard: return list(_threads)


def recover_startup():
    _stopping.clear()
    with SessionLocal.begin() as db:
        for op in db.scalars(select(LibraryIndexOperation).where(LibraryIndexOperation.status.in_(("queued", "preparing")))):
            count = len(list(db.scalars(select(LibraryIndexPreparedBatch.start).where(LibraryIndexPreparedBatch.operation_id == op.id))))
            op.status = "partial" if count else "interrupted"
            op.error = "应用曾退出；原索引保持不变，已完成批次已保存。核对后可继续，已保存批次不重复请求。"
    # Prepared publications require a fresh CAS/authorization check through the
    # resume action; no uncertain external request is automatically replayed.


def wait_shutdown():
    _stopping.set()
    with _guard: threads = list(_threads.values())
    for thread in threads: thread.join(timeout=2)
