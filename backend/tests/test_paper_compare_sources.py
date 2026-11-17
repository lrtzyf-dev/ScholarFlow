"""Comparison consumers retain typed locations and authorized fixed versions."""
from io import BytesIO
import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Conversation, Paper
from app.source_models import Source, SourceLibraryImport
from tests.test_research_persistence import extract


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner, research_runs
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(research_runs, "start", lambda ident: None)
    db.add_all([Conversation(id="compare-origin", title="Original materials"),
                Conversation(id="compare-reader", title="Comparison reader"),
                Conversation(id="compare-unrelated", title="Unrelated")])
    db.commit()
    with TestClient(app) as client:
        yield client


def materials():
    from docx import Document
    from openpyxl import Workbook
    from reportlab.pdfgen import canvas
    pdf = BytesIO()
    writer = canvas.Canvas(pdf)
    writer.drawString(40, 750, "The method uses PDF_COMPARE_TARGET for evaluation.")
    writer.save()
    word, doc = BytesIO(), Document()
    doc.add_paragraph("Preface without the target.")
    doc.add_paragraph("The method uses WORD_COMPARE_TARGET for evaluation.")
    doc.save(word)
    excel, book = BytesIO(), Workbook()
    book.active.title = "Results"
    book.active.append(["Method", "Result"])
    book.active.append(["EXCEL_COMPARE_TARGET method", 42])
    book.save(excel)
    book.close()
    return [
        ("study.pdf", pdf.getvalue(), "pdf_page"),
        ("study.md", b"# Preface\nThe method uses MARKDOWN_COMPARE_TARGET for evaluation.\n", "text_lines"),
        ("study.txt", b"Preface\nThe method uses TEXT_COMPARE_TARGET for evaluation.\n", "text_lines"),
        ("study.docx", word.getvalue(), "word_paragraphs"),
        ("study.xlsx", excel.getvalue(), "sheet_cells"),
        ("study.csv", b"Method,Result\nCSV_COMPARE_TARGET method,42\n", "sheet_cells"),
    ]


def upload(client, filename, raw, request, **changes):
    response = client.post("/api/sources/upload", files={"file": (filename, raw)}, data={
        "conversation_id": "compare-origin", "request_id": request, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def import_one(client, revision, request):
    response = client.post("/api/sources/library-import", json={
        "conversation_id": "compare-origin", "revision_ids": [revision["revision_id"]], "request_id": request})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["status"] in ("imported", "existing"), item
    return item["paper_id"]


def compare(client, papers, dimensions=("method",)):
    from app.agent.paper_compare import DEFAULT_DIMENSIONS
    fields = [f for f in DEFAULT_DIMENSIONS if f["key"] in dimensions]
    response = client.post("/api/research/matrices", json={"conversation_id": "compare-reader",
        "title": "Durable comparison", "paper_ids": papers, "fields": fields})
    assert response.status_code == 200, response.text
    return extract(client, response.json())["document"]


def test_mixed_comparison_preserves_typed_locators_scope_and_old_bytes(client, db):
    sources, papers, kinds = [], [], {}
    for index, (name, raw, kind) in enumerate(materials()):
        source = upload(client, name, raw, f"comparison-upload-{index}")
        paper = import_one(client, source, f"comparison-import-{index}")
        sources.append(source)
        papers.append(paper)
        kinds[paper] = kind
    result = compare(client, papers)
    assert result["conversation_id"] == "compare-reader"
    assert result["comparison_method"] == "retrieved_passages"
    fixed = []
    for paper, cell in result["rows"][0]["cells"].items():
        assert cell["status"] == "unverified" and cell["evidence"]
        for evidence in cell["evidence"]:
            assert evidence["verification_status"] == "unverified"
            assert evidence["source_sha256"] == db.get(Paper, paper).content_hash
            if kinds[paper] == "pdf_page":
                assert evidence["page"] >= 1
            else:
                assert evidence["page"] == 0 and evidence["bbox"] is None
            assert evidence["locator_kind"] == kinds[paper]
            assert evidence["location_status"] == "fixed_source"
            assert evidence["revision_id"] not in [s["revision_id"] for s in sources]
            path = f'/api/sources/revisions/{evidence["revision_id"]}'
            scoped = "?conversation_id=compare-reader"
            locator = client.get(path + "/locators/" + evidence["locator_id"] + scoped)
            assert locator.status_code == 200, locator.text
            assert locator.json()["quote"] == evidence["quote"]
            assert locator.json()["location"] == evidence["source_location"]
            assert client.get(path + "?conversation_id=compare-unrelated").status_code == 404
            fixed.append(evidence)
    markdown = sources[1]
    newer = upload(client, "study.md", b"The method now says CHANGED_VERSION.\n", "comparison-update-md",
        source_id=markdown["source_id"], expected_version=1)
    assert newer["version"] == 2
    md_evidence = next(e for e in fixed if e["paper_id"] == papers[1] and "MARKDOWN_COMPARE_TARGET" in e["quote"])
    path = f'/api/sources/revisions/{md_evidence["revision_id"]}/file?conversation_id=compare-reader'
    assert b"MARKDOWN_COMPARE_TARGET" in client.get(path).content
    assert b"CHANGED_VERSION" not in client.get(path).content
    # Repeat comparison reuses aliases and locators, rather than changing a
    # historical reference or implicitly selecting the updated source head.
    before = [s.id for s in db.scalars(select(Source).where(Source.scope_id == "compare-reader"))]
    again = compare(client, papers)
    after = [s.id for s in db.scalars(select(Source).where(Source.scope_id == "compare-reader"))]
    assert set(before) == set(after)
    assert again["rows"] == result["rows"]


def test_missing_or_archived_scope_cannot_create_comparison_alias(client, db):
    from app.timeutil import utcnow
    paper_ids = []
    for index in range(2):
        item = upload(client, f"sample{index}.md", f"method sample {index}\n".encode(), f"scope-upload-{index}")
        paper_ids.append(import_one(client, item, f"scope-import-{index}"))
    response = client.post("/api/papers/compare", json={"paper_ids": paper_ids, "conversation_id": "unknown"})
    assert response.status_code == 404
    conv = db.get(Conversation, "compare-reader")
    conv.archived_at = utcnow()
    db.commit()
    response = client.post("/api/papers/compare", json={"paper_ids": paper_ids, "conversation_id": "compare-reader"})
    assert response.status_code == 409
    assert not list(db.scalars(select(Source).where(Source.scope_id == "compare-reader")))


def test_unscoped_legacy_comparison_never_guesses_private_source_scope(client, db):
    paper_ids = []
    for index in range(2):
        item = upload(client, f"sample{index}.md", f"method sample {index}\n".encode(), f"legacy-upload-{index}")
        paper_ids.append(import_one(client, item, f"legacy-import-{index}"))
    response = client.post("/api/papers/compare", json={"paper_ids": paper_ids, "dimensions": ["method"]})
    assert response.status_code == 409 and response.json()["detail"]["not_executed"]
    assert not list(db.scalars(select(Source).where(Source.scope_id == "compare-reader")))


def test_multidimensional_comparison_records_api_embeddings_before_alias_writer(client, db, monkeypatch):
    """Real APIEmbedder + real durable ledger, with only HTTP transport replaced.

    Every transport observes its committed started ledger from another session.
    The immutable matrix aliases are prepared before query dispatch, then each
    field buys one query vector reused across three documents. No writer spans
    network I/O. No real provider or private file is used.
    """
    import httpx
    from app.config import settings
    from app.db import SessionLocal
    from app.ledger_models import CallRecord
    from app.pipeline import embeddings

    for name, value in {
        "embedding_base_url": "https://comparison.example.test/v1",
        "embedding_model": "synthetic-comparison-embedding", "embedding_api_key": "synthetic-comparison-key",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(embeddings, "_embedder", embeddings.APIEmbedder(synthetic=True))
    production_embedder = embeddings.APIEmbedder
    monkeypatch.setattr(embeddings, "APIEmbedder", lambda *a, **k: production_embedder(*a, **k, synthetic=True))
    transport_entries = []
    querying = []
    def respond(request):
        assert str(request.url) == "https://comparison.example.test/v1/embeddings"
        with SessionLocal() as observer:
            calls = list(observer.scalars(select(CallRecord).where(CallRecord.state == "started")))
            assert len(calls) == 1 and calls[0].service == "embedding"
            assert calls[0].synthetic == 1
            aliases = list(observer.scalars(select(Source).where(Source.scope_id == "compare-reader")))
            assert len(aliases) == (3 if querying else 0)
            transport_entries.append(calls[0].id)
        from sqlalchemy import text
        with SessionLocal() as writer:
            writer.execute(text("BEGIN IMMEDIATE")); writer.rollback()
        body = json.loads(request.content)
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [1.0, 0.0]} for index, _ in enumerate(body["input"])],
            "usage": {"prompt_tokens": 10, "total_tokens": 10}})
    original_client = httpx.Client
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(embeddings.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    papers = []
    for index in range(3):
        item = upload(client, f"api-{index}.md", f"method dataset benchmark unique{index}\n".encode(),
                      f"api-upload-{index}")
        papers.append(import_one(client, item, f"api-import-{index}"))
    indexed_calls = len(transport_entries)
    assert indexed_calls == 3
    querying.append(True)
    result = compare(client, papers, ("method", "dataset"))
    assert len(transport_entries) - indexed_calls == 2
    assert len(set(transport_entries)) == len(transport_entries)
    assert len(result["rows"]) == 2
    assert all(cell["evidence"] for row in result["rows"] for cell in row["cells"].values())
    assert all(evidence["location_status"] == "fixed_source" for row in result["rows"]
               for cell in row["cells"].values() for evidence in cell["evidence"])
    with SessionLocal() as observer:
        calls = list(observer.scalars(select(CallRecord)))
        assert len(calls) == 5 and all(call.state == "completed" for call in calls)
        assert all(not call.actual_usage for call in calls)
        assert all(call.estimated_usage == {"prompt_tokens": 10, "total_tokens": 10} for call in calls)
        assert len(list(observer.scalars(select(Source).where(Source.scope_id == "compare-reader")))) == 3


def test_comparison_service_keeps_aliases_and_existing_edits_in_callers_transaction(client, db):
    from app.agent.paper_compare import build_comparison
    from app.db import SessionLocal
    papers = []
    for index in range(2):
        source = upload(client, f"transaction-{index}.md", f"method text unique{index}".encode(), f"txn-upload-{index}")
        papers.append(import_one(client, source, f"txn-import-{index}"))
    conversation = db.get(Conversation, "compare-reader")
    conversation.title = "Uncommitted caller edit"
    result = build_comparison(db, papers, [{"key": "method", "label": "Method", "query": "method"}],
                              conversation_id="compare-reader")
    assert result["rows"][0]["cells"][papers[0]]["evidence"][0]["revision_id"]
    with SessionLocal() as observer:
        assert observer.get(Conversation, "compare-reader").title == "Comparison reader"
        assert not list(observer.scalars(select(Source).where(Source.scope_id == "compare-reader")))
    db.rollback()
    assert db.get(Conversation, "compare-reader").title == "Comparison reader"
    assert not list(db.scalars(select(Source).where(Source.scope_id == "compare-reader")))
