"""Real PDF pipeline + real metered adapter, with synthetic HTTP only."""
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app import config
from app.call_ledger import list_calls
from app.models import Chunk, Paper, Reference, TaskRecord
from app.pipeline import embeddings, indexer
from app.pipeline.vectordb import NumpyVectorStore, current_embedding_fingerprint


@pytest.fixture
def pipeline(db, sample_pdf, monkeypatch, tmp_path):
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    vector = tmp_path / "vectors.npy"
    monkeypatch.setattr(indexer, "PARSED_DIR", parsed)
    monkeypatch.setattr(indexer, "NumpyVectorStore", lambda path=None: NumpyVectorStore(path or vector))
    monkeypatch.setattr(indexer, "EMBED_BATCH_SIZE", 1)
    for name, value in {"embedding_base_url": "https://indexer.example.test/v1",
                        "embedding_api_key": "synthetic-indexer-key", "embedding_model": "Embed-Case"}.items():
        monkeypatch.setattr(config.settings, name, value)
    monkeypatch.setattr(embeddings, "_embedder", None)
    real_client, calls = httpx.Client, []
    def normal(request, number):
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "data": [{"index": index, "embedding": [0.1, 0.2, 0.3]} for index in range(len(body["input"]))],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        })
    handler = [normal]
    def dispatch(request):
        # Persisted call admission must work before the provider receives bytes.
        assert not db.in_transaction(), "PDF indexing held a DB transaction during provider HTTP"
        assert any(item["state"] == "started" for item in list_calls()["items"])
        calls.append(json.loads(request.content))
        return handler[0](request, len(calls))
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(dispatch), **kwargs))
    paper = Paper(title="Synthetic metered PDF", pdf_path=str(sample_pdf), content_hash="metered-pdf")
    db.add(paper)
    db.commit()
    return paper, calls, handler, vector


def snapshot(db, paper, vector):
    pid = paper.id
    return {
        "chunks": [(row.id, row.content) for row in db.query(Chunk).filter_by(paper_id=pid).order_by(Chunk.id)],
        "references": [row.id for row in db.query(Reference).filter_by(paper_id=pid).order_by(Reference.id)],
        "fts": list(db.execute(text("SELECT chunk_id, content FROM chunks_fts WHERE paper_id=:pid ORDER BY chunk_id"), {"pid": pid})),
        "matrix": vector.read_bytes(), "metadata": vector.with_suffix(".json").read_bytes(),
        "parsed": Path(paper.parsed_path).read_bytes(),
    }


def check_preserved(db, paper, vector, before):
    db.expire_all()
    assert snapshot(db, paper, vector) == before
    assert paper.status == "failed"
    assert db.query(TaskRecord).filter_by(paper_id=paper.id).order_by(TaskRecord.created_at.desc()).first().status == "failed"


def test_ingest_pdf_remote_embedding_records_calls_without_writer_lock(db, pipeline):
    paper, calls, _, vector = pipeline
    indexer.ingest_pdf(db, paper)
    assert len(calls) >= 2
    receipts = list_calls()["items"]
    assert len(receipts) == len(calls)
    assert all(item["service"] == "embedding" and item["state"] == "completed" for item in receipts)
    assert all(item["usage_source"] == "actual" for item in receipts)
    assert paper.status == "parsed"
    store = NumpyVectorStore(vector)
    assert store.embedding_fingerprint == current_embedding_fingerprint()
    assert len(store.ids) == db.query(Chunk).filter_by(paper_id=paper.id).count()
    assert all(call["model"] == "Embed-Case" for call in calls)


@pytest.mark.parametrize("failure", ["provider", "dimensions", "configuration"])
def test_preflight_failure_keeps_old_chunks_fts_vectors_and_parsed_document(db, pipeline, monkeypatch, failure):
    paper, calls, handler, vector = pipeline
    indexer.ingest_pdf(db, paper)
    before = snapshot(db, paper, vector)
    calls.clear()
    normal = handler[0]
    def changed(request, number):
        if failure == "configuration":
            monkeypatch.setattr(config.settings, "embedding_model", "Changed-Mid-Flight")
        if failure == "provider" and number == 2:
            return httpx.Response(503, text="synthetic failure")
        if failure == "dimensions" and number == 2:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
        return normal(request, number)
    handler[0] = changed
    with pytest.raises((httpx.HTTPError, ValueError)):
        indexer.ingest_pdf(db, paper)
    check_preserved(db, paper, vector, before)
    assert all(call["model"] == "Embed-Case" for call in calls)
    if failure == "configuration":
        assert len(calls) == 1


def test_configuration_changed_after_prefetch_is_rejected_before_index_write(db, pipeline, monkeypatch):
    paper, _, _, vector = pipeline
    indexer.ingest_pdf(db, paper)
    before = snapshot(db, paper, vector)
    prepare = indexer._prepare_embedding_vectors
    def change_after(chunks):
        result = prepare(chunks)
        monkeypatch.setattr(config.settings, "embedding_api_key", "synthetic-replaced-key")
        return result
    monkeypatch.setattr(indexer, "_prepare_embedding_vectors", change_after)
    with pytest.raises(ValueError, match="配置已变化"):
        indexer.ingest_pdf(db, paper)
    check_preserved(db, paper, vector, before)


@pytest.mark.parametrize("failure", ["fts", "publish", "commit"])
def test_index_transaction_failure_restores_old_index(db, pipeline, monkeypatch, failure):
    paper, _, _, vector = pipeline
    indexer.ingest_pdf(db, paper)
    before = snapshot(db, paper, vector)
    if failure == "fts":
        execute = db.execute
        inserted = [0]
        def fail_insert(statement, *args, **kwargs):
            if str(statement).startswith("INSERT INTO chunks_fts"):
                inserted[0] += 1
                if inserted[0] == 2:
                    raise RuntimeError("synthetic FTS failure after first new chunk")
            return execute(statement, *args, **kwargs)
        monkeypatch.setattr(db, "execute", fail_insert)
    elif failure == "publish":
        replace, failed = indexer.os.replace, [False]
        destination = Path(paper.parsed_path)
        def fail_publish(source, target):
            if Path(target) == destination and not failed[0]:
                failed[0] = True
                raise OSError("synthetic final file replacement failure")
            return replace(source, target)
        monkeypatch.setattr(indexer.os, "replace", fail_publish)
    else:
        commit, count = db.commit, [0]
        def fail_commit():
            count[0] += 1
            if count[0] == 3:
                raise RuntimeError("synthetic final DB commit failure")
            return commit()
        monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises((RuntimeError, OSError)):
        indexer.ingest_pdf(db, paper)
    check_preserved(db, paper, vector, before)


def test_failed_publication_and_failed_restore_keep_recovery_bytes_and_safe_receipt(db, pipeline, monkeypatch):
    paper, _, _, vector = pipeline
    indexer.ingest_pdf(db, paper)
    before = snapshot(db, paper, vector)
    parsed_path = Path(paper.parsed_path)
    replace, published_failure = indexer.os.replace, [False]
    def twice_failing(source, target):
        if Path(target) == parsed_path and not published_failure[0]:
            published_failure[0] = True
            raise OSError("SYNTHETIC_PRIVATE_PROVIDER_TEXT publication failed")
        if Path(target) == vector and Path(source).name.startswith("previous-"):
            raise PermissionError("SYNTHETIC_PRIVATE_PROVIDER_TEXT restore failed")
        return replace(source, target)
    monkeypatch.setattr(indexer.os, "replace", twice_failing)
    with pytest.raises(indexer.IndexRecoveryRequired) as failure:
        indexer.ingest_pdf(db, paper)
    recovery = failure.value.directory
    assert recovery.is_dir()
    receipt = json.loads((recovery / "recovery-required.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "recovery_required"
    assert receipt["original_error_kind"] == "OSError"
    assert "SYNTHETIC_PRIVATE_PROVIDER_TEXT" not in json.dumps(receipt)
    assert len(receipt["files"]) == 1
    saved = receipt["files"][0]
    assert saved["destination"] == str(vector) and saved["error_kind"] == "PermissionError"
    assert (recovery / saved["backup"]).read_bytes() == before["matrix"]
    assert vector.with_suffix(".json").read_bytes() == before["metadata"]
    assert parsed_path.read_bytes() == before["parsed"]
    db.expire_all()
    assert [(row.id, row.content) for row in db.query(Chunk).filter_by(paper_id=paper.id).order_by(Chunk.id)] == before["chunks"]
    assert paper.status == "failed" and "需要恢复处理" in paper.error
    assert "SYNTHETIC_PRIVATE_PROVIDER_TEXT" not in paper.error
