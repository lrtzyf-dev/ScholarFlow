"""Fixed non-PDF revisions use one metered embedding configuration per import."""
import io
import json
import threading

import httpx
import pytest
from sqlalchemy import text

from app import config, source_library, source_store as sources
from app.call_ledger import list_calls
from app.models import Chunk, Conversation, Paper
from app.pipeline import embeddings, indexer, vectordb
from app.source_documents import save_extraction
from app.source_models import SourceLibraryImport
from app.source_parsing import MEDIA, extract


@pytest.fixture
def pipeline(db, monkeypatch, tmp_path):
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    vector = tmp_path / "vectors.npy"
    monkeypatch.setattr(source_library, "PARSED_DIR", parsed)
    real_store = vectordb.NumpyVectorStore
    monkeypatch.setattr(vectordb, "NumpyVectorStore", lambda: real_store(vector))
    monkeypatch.setattr(indexer, "EMBED_BATCH_SIZE", 1)
    for name, value in {"embedding_base_url": "https://source-embed.example.test/v1",
                        "embedding_api_key": "synthetic-source-embedding-key",
                        "embedding_model": "Embedding-Exact-Case"}.items():
        monkeypatch.setattr(config.settings, name, value)
    monkeypatch.setattr(embeddings, "_embedder", None)
    real_client, calls = httpx.Client, []

    def normal(request, number):
        body = json.loads(request.content)
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(len(body["input"]))],
            "usage": {"prompt_tokens": 3, "total_tokens": 3}})

    handler = [normal]

    def dispatch(request):
        assert not db.in_transaction(), "Source indexing held a transaction during provider HTTP"
        assert any(item["state"] == "started" for item in list_calls()["items"])
        calls.append({"body": json.loads(request.content), "url": str(request.url),
                      "authorization": request.headers.get("authorization")})
        return handler[0](request, len(calls))

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(dispatch), **kwargs))
    scope = sources.SourceScope("conversation", "embedding-source-conversation")
    db.add(Conversation(id=scope.scope_id, title="Embedding source test"))
    db.commit()
    return scope, calls, handler, vector, real_store


def raw_document(extension, label="first"):
    if extension in ("txt", "md"):
        return f"# {label} document\nFirst result: 12.\nSecond result: 15.\n".encode()
    if extension == "csv":
        return f"{label},value\nfirst,12\nsecond,15\n".encode()
    stream = io.BytesIO()
    if extension == "docx":
        from docx import Document
        document = Document()
        for line in [f"{label} document", "First result: 12.", "Second result: 15."]:
            document.add_paragraph(line)
        document.save(stream)
    else:
        from openpyxl import Workbook
        document = Workbook()
        for row in [[label, "value"], ["first", 12], ["second", 15]]:
            document.active.append(row)
        document.save(stream)
    return stream.getvalue()


def capture(db, scope, extension="md", label="first"):
    raw = raw_document(extension, label)
    extraction = extract(raw, MEDIA[extension])
    record = sources.capture_source(db, scope=scope,
        authorization=sources.SourceAuthorization(scope, f"capture-{label}", "upload"),
        kind="upload", title=f"{label}.{extension}", raw_bytes=raw, text=extraction.text,
        media_type=MEDIA[extension], text_origin="parsed_text", parser=extraction.parser)
    save_extraction(db, record["revision_id"], scope=scope, extraction=extraction)
    db.commit()
    return record, raw


def import_one(db, scope, record):
    return source_library.import_selected(db, scope=scope, revision_ids=[record["revision_id"]],
        request_id="synthetic-library-import")["items"][0]


@pytest.mark.parametrize("extension", ["txt", "md", "csv", "docx", "xlsx"])
def test_each_nonpdf_format_freezes_all_batches_and_retains_exact_original(db, pipeline, extension):
    scope, calls, _, vector, real_store = pipeline
    record, raw = capture(db, scope, extension)
    result = import_one(db, scope, record)
    assert result["status"] == "imported", result
    assert len(calls) == 3
    assert all(call["body"]["model"] == "Embedding-Exact-Case"
               and call["url"] == "https://source-embed.example.test/v1/embeddings"
               and call["authorization"] == "Bearer synthetic-source-embedding-key" for call in calls)
    ledger = list_calls()["items"]
    assert len(ledger) == 3 and all(item["state"] == "completed" for item in ledger)
    assert all(item["usage_source"] == "actual" for item in ledger)
    store = real_store(vector)
    assert store.embedding_fingerprint == vectordb.current_embedding_fingerprint()
    assert len(store.ids) == 3
    assert sources.read_revision(db, record["revision_id"], scope=scope, include_raw=True)["raw_bytes"] == raw
    receipt = db.get(SourceLibraryImport, record["revision_id"])
    assert receipt.paper_id == result["paper_id"] and len(receipt.chunk_locators) == 3
    assert import_one(db, scope, record)["status"] == "existing"
    assert len(calls) == 3, "An existing import receipt must not trigger another paid request"


def snapshot(db, record, vector):
    receipt = db.get(SourceLibraryImport, record["revision_id"])
    return {"receipt": (receipt.paper_id, receipt.raw_sha256, dict(receipt.chunk_locators)),
            "chunks": [(row.id, row.content) for row in db.query(Chunk).order_by(Chunk.id)],
            "fts": list(db.execute(text("SELECT chunk_id,content FROM chunks_fts ORDER BY chunk_id"))),
            "matrix": vector.read_bytes(), "metadata": vector.with_suffix(".json").read_bytes()}


@pytest.mark.parametrize("failure", ["model", "key", "endpoint", "dimensions", "provider"])
def test_failed_new_import_preserves_existing_receipts_index_and_fixed_original(db, pipeline, monkeypatch, failure):
    scope, calls, handler, vector, _ = pipeline
    first, _ = capture(db, scope)
    assert import_one(db, scope, first)["status"] == "imported"
    before = snapshot(db, first, vector)
    second, raw = capture(db, scope, label="second")
    normal = handler[0]
    calls.clear()

    def changed(request, number):
        setting = {"model": "embedding_model", "key": "embedding_api_key", "endpoint": "embedding_base_url"}.get(failure)
        if setting:
            monkeypatch.setattr(config.settings, setting,
                                "https://changed.example.test/v1" if failure == "endpoint" else "Changed-Configuration")
        if failure == "dimensions" and number == 2:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
        if failure == "provider" and number == 2:
            return httpx.Response(503, text="SYNTHETIC_PRIVATE_PROVIDER_TEXT")
        return normal(request, number)

    handler[0] = changed
    result = import_one(db, scope, second)
    assert result["status"] == "failed" and "SYNTHETIC_PRIVATE_PROVIDER_TEXT" not in result["error"]
    assert len(calls) == (2 if failure in ("dimensions", "provider") else 1)
    assert all(call["body"]["model"] == "Embedding-Exact-Case" for call in calls)
    assert snapshot(db, first, vector) == before
    assert db.get(SourceLibraryImport, second["revision_id"]) is None
    assert db.query(Paper).count() == 1
    assert sources.read_revision(db, second["revision_id"], scope=scope, include_raw=True)["raw_bytes"] == raw
    monkeypatch.setattr(config.settings, "embedding_model", "Embedding-Exact-Case")
    monkeypatch.setattr(config.settings, "embedding_api_key", "synthetic-source-embedding-key")
    monkeypatch.setattr(config.settings, "embedding_base_url", "https://source-embed.example.test/v1")
    handler[0] = normal
    assert import_one(db, scope, second)["status"] == "imported"
    assert db.query(Paper).count() == 2


def test_post_prefetch_configuration_change_is_rejected_before_any_cache_write(db, pipeline, monkeypatch):
    scope, _, _, vector, _ = pipeline
    first, _ = capture(db, scope)
    assert import_one(db, scope, first)["status"] == "imported"
    before = snapshot(db, first, vector)
    second, raw = capture(db, scope, label="second")
    prepare = source_library._prepare_vectors

    def change_after(locators):
        result = prepare(locators)
        monkeypatch.setattr(config.settings, "embedding_model", "Changed-After-Prefetch")
        return result

    monkeypatch.setattr(source_library, "_prepare_vectors", change_after)
    result = import_one(db, scope, second)
    assert result["status"] == "failed" and "配置已变化" in result["error"]
    assert snapshot(db, first, vector) == before
    assert db.query(Paper).count() == 1 and db.get(SourceLibraryImport, second["revision_id"]) is None
    assert sources.read_revision(db, second["revision_id"], scope=scope, include_raw=True)["raw_bytes"] == raw


def test_final_index_and_commit_hold_configuration_lock_but_http_does_not(db, pipeline, monkeypatch):
    scope, _, handler, _, _ = pipeline
    record, _ = capture(db, scope)
    observations = []

    def competing_lock(stage):
        def attempt():
            acquired = config._settings_lock.acquire(blocking=False)
            observations.append((stage, acquired))
            if acquired:
                config._settings_lock.release()
        worker = threading.Thread(target=attempt)
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive()

    normal = handler[0]
    def check_http(request, number):
        competing_lock("http")
        return normal(request, number)
    handler[0] = check_http
    original_index = source_library._index_document
    indexed = [False]
    def check_index(*args):
        competing_lock("index")
        result=original_index(*args)
        indexed[0]=True
        return result
    monkeypatch.setattr(source_library, "_index_document", check_index)
    commit = db.commit
    def check_commit():
        # Library-owned source copies may flush the receipt before commit;
        # assert the transaction boundary after actual indexing, not ORM's
        # transient pending-object membership.
        if indexed[0]:
            competing_lock("commit")
            indexed[0]=False
        return commit()
    monkeypatch.setattr(db, "commit", check_commit)
    assert import_one(db, scope, record)["status"] == "imported"
    assert observations == [("http", True)] * 3 + [("index", False), ("commit", False)]
