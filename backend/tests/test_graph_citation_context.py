"""Actual immutable originals, citation markers and graph evidence; no models."""
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.citation_contexts import extract_contexts
from app.main import app
from app.models import Conversation, Paper, Reference


def ref(index=1, raw="[1] Smith, J. Robust Study of Research Methods. 2020."):
    return SimpleNamespace(ref_index=index, raw_text=raw, title="Robust Study of Research Methods")


def document(lines):
    return {"format": "text", "blocks": [{"kind": "text_line", "line_start": i,
        "line_end": i, "text": line} for i, line in enumerate(lines, 1)]}


@pytest.mark.parametrize("marker", ["[1]", "[1, 2]", "[1–3]", "[1; 3]", "[1，3]"])
def test_numeric_markers_exclude_bibliography(marker):
    result = extract_contexts(document([f"Earlier work supports the method {marker}.", "References",
        "[1] Smith, J. Robust Study of Research Methods. 2020."]), ref())
    assert result["status"] == "located"
    assert len(result["contexts"]) == 1
    assert result["contexts"][0]["marker"] == marker
    assert result["contexts"][0]["location"]["line_start"] == 1
    assert result["contexts"][0]["semantic_support"] == "unverified"


@pytest.mark.parametrize("marker", ["Smith (2020)", "(Smith, 2020)", "(Smith et al., 2020)", "Smith et al. (2020)"])
def test_author_year_markers(marker):
    result = extract_contexts(document([f"The method follows {marker} for comparison."]), ref())
    assert result["contexts"][0]["match_method"] == "author_year"


def test_missing_wrong_author_or_reference_only_is_honest():
    for lines in (["No citation exists."], ["Jones (2020) described another method."],
                  ["Smith (2021) described another method."], ["## References", ref().raw_text],
                  [ref().raw_text], ["References", "The method uses [1]."]):
        result = extract_contexts(document(lines), ref())
        assert result["status"] == "not_found" and result["contexts"] == []
    # A hostile huge range is bounded rather than allocating all integers.
    assert extract_contexts(document(["The method uses [1–99999]."]), ref())["contexts"] == []
    # Sentence splitting must not turn an unheaded author-year bibliography
    # entry into a supposed body sentence.
    entry = "Smith (2020). Comparative methods for robust evaluation."
    assert extract_contexts(document([entry]), ref(raw=entry))["contexts"] == []


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    db.add_all([Conversation(id="graph_context", title="Graph context"),
                Conversation(id="graph_reader", title="Graph reader")])
    db.commit()
    with TestClient(app) as client:
        yield client


BODY = "Earlier research improves robust evaluation [1]."


def original(extension):
    buffer = BytesIO()
    if extension == "pdf":
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(buffer)
        c.drawString(40, 750, BODY)
        c.showPage(); c.drawString(40, 750, "References")
        c.drawString(40, 730, ref().raw_text); c.save()
        return buffer.getvalue()
    if extension == "docx":
        from docx import Document
        d = Document(); d.add_paragraph(BODY); d.add_paragraph("References"); d.add_paragraph(ref().raw_text)
        d.save(buffer); return buffer.getvalue()
    if extension == "xlsx":
        from openpyxl import Workbook
        b = Workbook(); b.active.title = "Evidence"; b.active.append([BODY]);
        b.active.append(["References"]); b.active.append([ref().raw_text])
        b.save(buffer); b.close(); return buffer.getvalue()
    if extension == "csv":
        return (BODY + "\nReferences\n" + ref().raw_text + "\n").encode()
    return ("# Body\n" + BODY + "\nReferences\n" + ref().raw_text + "\n").encode()


def seed(client, db, extension):
    uploaded = client.post("/api/sources/upload", files={"file": ("context." + extension, original(extension))},
        data={"conversation_id": "graph_context", "request_id": "graph-upload-" + extension})
    assert uploaded.status_code == 200, uploaded.text
    imported = client.post("/api/sources/library-import", json={"conversation_id": "graph_context",
        "revision_ids": [uploaded.json()["revision_id"]], "request_id": "graph-import-" + extension})
    assert imported.status_code == 200, imported.text
    pid = imported.json()["items"][0]["paper_id"]
    db.execute(delete(Reference).where(Reference.paper_id == pid))
    target = Paper(title=ref().title, status="parsed", content_hash="target-has-no-original")
    db.add(target); db.flush()
    db.add(Reference(paper_id=pid, ref_index=1, title=ref().title, raw_text=ref().raw_text,
        page=2 if extension == "pdf" else None))
    db.commit()
    response = client.get("/api/graph")
    assert response.status_code == 200, response.text
    edges = [e for e in response.json()["edges"] if e["via"] == ref().title]
    assert len(edges) == 1, response.json()
    rid = edges[0]["relation_id"]
    detail = client.get("/api/research/relations/" + rid)
    assert detail.status_code == 200, detail.text
    return pid, detail.json(), uploaded.json()


@pytest.mark.parametrize("extension,kind", [("pdf", "pdf_page"), ("md", "text_lines"),
    ("txt", "text_lines"), ("docx", "word_paragraphs"), ("xlsx", "sheet_cells"), ("csv", "sheet_cells")])
def test_six_original_formats_graph_passage_location_and_exact_alias(client, db, extension, kind):
    _, detail, _ = seed(client, db, extension)
    extracted = detail["extracted"]
    assert extracted["citation_sentence"] == BODY
    assert extracted["sentence_status"] == "located"
    assert len(extracted["citation_contexts"]) == 1
    context = extracted["citation_contexts"][0]
    located = client.post(f'/api/research/relations/{detail["id"]}/locate',
        json={"conversation_id": "graph_reader", "context_index": 0})
    assert located.status_code == 200, located.text
    result = located.json()
    assert result["location_kind"] == "citation_context"
    assert result["citation_sentence_verified"] is True and result["semantic_support_verified"] is False
    base = f'/api/sources/revisions/{result["revision_id"]}'
    loc = client.get(base + '/locators/' + result["locator_id"], params={"conversation_id": "graph_reader"})
    assert loc.status_code == 200, loc.text
    assert loc.json()["kind"] == kind
    assert BODY in loc.json()["quote"]
    if extension == "pdf":
        assert context["location"]["page_index"] == 0
        assert context["location"]["precision"] == "text_region"
        assert context["location"]["bbox"][3] < 150
    again = client.post(f'/api/research/relations/{detail["id"]}/locate',
        json={"conversation_id": "graph_reader", "context_index": 0})
    assert again.json()["revision_id"] == result["revision_id"]
    assert again.json()["locator_id"] == result["locator_id"]
    assert client.get(base + '/locators/' + result["locator_id"],
        params={"conversation_id": "graph_context"}).status_code == 404


def test_old_context_survives_original_mutation_and_reparse_without_redirect(client, db):
    pid, detail, _ = seed(client, db, "pdf")
    from pathlib import Path
    paper = db.get(Paper, pid, populate_existing=True)
    Path(paper.pdf_path).write_bytes(b"replacement is not the old PDF")
    # Even a content identity change must not send the old relation to a new head.
    paper.content_hash = "new-version-content-hash"; db.commit()
    response = client.post(f'/api/research/relations/{detail["id"]}/locate',
        json={"conversation_id": "graph_reader", "context_index": 0})
    assert response.status_code == 200, response.text
    rid = response.json()["revision_id"]
    downloaded = client.get(f'/api/sources/revisions/{rid}/file', params={"conversation_id": "graph_reader"})
    assert downloaded.content.startswith(b"%PDF")
    # Legacy reference-entry locate cannot silently target the new original.
    assert client.post(f'/api/research/relations/{detail["id"]}/locate',
        json={"conversation_id": "graph_reader"}).status_code == 409
    db.delete(paper); db.commit()
    assert client.post(f'/api/research/relations/{detail["id"]}/locate',
        json={"conversation_id": "graph_reader", "context_index": 0}).status_code == 404


def test_manual_relation_correction_survives_context_rebuild_and_missing_locator(client, db):
    from app.source_models import SourceLocator
    pid, detail, _ = seed(client, db, "md")
    url = '/api/research/relations/' + detail["id"]
    reviewed = client.patch(url, json={"expected_version": detail["version"], "action": "correct",
        "reason": "The body citation was checked; relation type is uncertain", "relation_type": "shared_topic"})
    assert reviewed.status_code == 200, reviewed.text
    manual = deepcopy(reviewed.json()["effective"])
    loc_id = detail["extracted"]["citation_contexts"][0]["locator_id"]
    db.execute(delete(SourceLocator).where(SourceLocator.id == loc_id)); db.commit()
    rebuilt = client.get('/api/graph')
    assert rebuilt.status_code == 200, rebuilt.text
    current = client.get(url).json()
    assert current["manual"] and current["effective"] == manual
    assert current["extracted"]["citation_contexts"][0]["locator_id"] != loc_id
    assert client.post(url + '/locate', json={"conversation_id": "graph_reader", "context_index": 0}).status_code == 200
    assert client.post(url + '/locate', json={"conversation_id": "graph_reader", "context_index": 7}).status_code == 404


def test_pdf_region_pass_shared_across_references_and_warm_projection(client, db, monkeypatch):
    from app import citation_contexts
    original_pass = citation_contexts._pdf_regions
    original_index = citation_contexts._passage_index
    calls = []
    indexes = []
    def counted(raw):
        calls.append(len(raw)); return original_pass(raw)
    monkeypatch.setattr(citation_contexts, '_pdf_regions', counted)
    def counted_index(document):
        indexes.append(document["raw_sha256"]); return original_index(document)
    monkeypatch.setattr(citation_contexts, '_passage_index', counted_index)
    pid, _, _ = seed(client, db, "pdf")
    assert len(calls) == 1
    assert len(indexes) == 1
    for i in range(2, 7):
        db.add(Reference(paper_id=pid, ref_index=i, raw_text=f"[{i}] Smith, J. Another title {i}. 2020.",
            title=ref().title, page=2))
    db.commit()
    assert client.get('/api/graph').status_code == 200
    assert len(calls) == 2  # One new cold build, not one parse per new reference.
    assert len(indexes) == 2
    assert client.get('/api/graph').status_code == 200
    assert len(calls) == 2  # Hot reads do not parse any originals.
    assert len(indexes) == 2
