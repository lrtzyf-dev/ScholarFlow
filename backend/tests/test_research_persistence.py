"""Research workflows use isolated originals and no real model calls."""
from copy import deepcopy
from io import BytesIO
import threading
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.models import Conversation, Paper
from app.research_models import MatrixRevision, AnnotationRevision


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner, research_runs
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(research_runs, "start", lambda ident: None)
    db.add_all([Conversation(id="research", title="Research"), Conversation(id="unrelated", title="Other")])
    db.commit()
    with TestClient(app) as client:
        yield client


def seed(client, index, extension="md"):
    target = f"The method uses research sample {index} for evaluation."
    if extension == "pdf":
        from reportlab.pdfgen import canvas
        buffer = BytesIO(); c = canvas.Canvas(buffer); c.drawString(40, 750, target); c.save(); raw = buffer.getvalue()
    elif extension == "docx":
        from docx import Document
        buffer = BytesIO(); d = Document(); d.add_paragraph(target); d.save(buffer); raw = buffer.getvalue()
    elif extension == "xlsx":
        from openpyxl import Workbook
        buffer = BytesIO(); b = Workbook(); b.active.append([target, index]); b.save(buffer); b.close(); raw = buffer.getvalue()
    elif extension == "csv":
        raw = f"method,value\n{target},{index}\n".encode()
    else:
        raw = f"# Sample {index}\n{target}\n".encode()
    response = client.post("/api/sources/upload", files={"file": (f"sample-{index}.{extension}", raw)},
        data={"conversation_id": "research", "request_id": f"research-upload-{index}"})
    assert response.status_code == 200, response.text
    source = response.json()
    response = client.post("/api/sources/library-import", json={"conversation_id": "research",
        "revision_ids": [source["revision_id"]], "request_id": f"research-import-{index}"})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["status"] == "imported", item
    return item["paper_id"], source


def create(client, ids):
    r = client.post("/api/research/matrices", json={"conversation_id": "research", "title": "Methods",
        "paper_ids": ids, "fields": [{"key": "method", "label": "Custom methods", "query": "method evaluation"}]})
    assert r.status_code == 200, r.text
    return r.json()


def extract(client, item, paper_ids=None, field_keys=None):
    from app import research_runs
    # Tests own dispatch explicitly, avoiding a race between HTTP response
    # assertions and a background worker. The production durable adapter runs.
    ident = "research_" + uuid4().hex
    response = client.post("/api/research/matrix-runs", json={"conversation_id": item["conversation_id"],
        "matrix_id": item["id"], "expected_version": item["version"], "run_id": ident,
        "request_id": "durable-" + uuid4().hex, "paper_ids": paper_ids, "field_keys": field_keys})
    assert response.status_code == 200, response.text
    research_runs.execute(ident, response.json()["attempt_id"])
    status = client.get("/api/research/matrix-runs/" + ident, params={"conversation_id": item["conversation_id"]})
    assert status.status_code == 200 and status.json()["outcome"] == "completed", status.text
    response = client.get("/api/research/matrices/" + item["id"], params={"conversation_id": item["conversation_id"]})
    assert response.status_code == 200, response.text
    return response.json()


def test_twenty_mixed_sources_manual_review_reextract_roundtrip(client, db):
    papers = [seed(client, i, ["pdf", "md", "txt", "docx", "xlsx", "csv"][i % 6])[0] for i in range(20)]
    m = create(client, papers)
    assert len(m["document"]["papers"]) == 20
    assert {c["status"] for c in m["document"]["rows"][0]["cells"].values()} == {"not_processed"}
    url = f'/api/research/matrices/{m["id"]}'
    m = extract(client, m)
    for cell in m["document"]["rows"][0]["cells"].values():
        assert cell["status"] == "unverified"
        assert all(e.get("revision_id") and e.get("locator_id") for e in cell["evidence"])
    for i, status in enumerate(["supported", "conflict", "not_found"]):
        r = client.patch(url + "/cell", json={"conversation_id": "research", "expected_version": m["version"],
            "field_key": "method", "paper_id": papers[i], "value": f"Human correction {i}", "status": status, "note": "Checked original"})
        assert r.status_code == 200, r.text
        m = r.json()
    reviewed = deepcopy(m["document"]["rows"][0]["cells"])
    assert client.patch(url + "/cell", json={"conversation_id": "research", "expected_version": 1,
        "field_key": "method", "paper_id": papers[0], "value": "stale", "status": "unverified"}).status_code == 409
    reopened = client.get(url, params={"conversation_id": "research"}).json()
    assert reopened["document"] == m["document"]
    m = extract(client, m, paper_ids=papers[:3])
    for pid in papers[:3]:
        cell = m["document"]["rows"][0]["cells"][pid]
        assert cell["manual"] == reviewed[pid]["manual"]
        assert cell["evidence"] == reviewed[pid]["evidence"]
        assert cell["extraction_candidate"]["requires_review"]
        assert len(cell["extraction_history"]) == 2
    package = client.get(url + "/export", params={"conversation_id": "research"}).json()
    r = client.post("/api/research/matrices/import", json={"conversation_id": "research", "package": package})
    assert r.status_code == 200, r.text
    imported = r.json()["document"]
    assert imported["papers"] == m["document"]["papers"]
    assert imported["rows"] == m["document"]["rows"]
    assert imported["dimensions"] == m["document"]["dimensions"]
    assert imported["schema_version"] == m["document"]["schema_version"]
    assert len(imported["imported_history"]["entries"]) == m["version"]
    assert len(list(db.scalars(select(MatrixRevision).where(MatrixRevision.matrix_id == m["id"])))) == m["version"]
    assert client.get(url, params={"conversation_id": "unrelated"}).status_code == 404
    assert client.post("/api/research/matrices/import", json={"conversation_id": "unrelated", "package": package}).status_code == 404
    broken = deepcopy(package)
    broken["document"]["rows"][0]["cells"][papers[0]]["evidence"][0]["quote"] = "Invented quote"
    assert client.post("/api/research/matrices/import", json={"conversation_id": "research", "package": broken}).status_code == 409


def test_selected_missing_source_is_not_silently_removed(client, db):
    ids = [seed(client, i)[0] for i in range(2)]
    legacy = client.post("/api/papers/compare", json={"conversation_id": "research", "paper_ids": ids + ["missing"]})
    assert legacy.status_code == 409 and legacy.json()["detail"]["not_executed"]
    response = client.post("/api/research/matrices", json={"conversation_id": "research", "paper_ids": ids + ["missing"],
        "title": "Missing", "fields": [{"key": "m", "label": "M", "query": "method"}]})
    assert response.status_code == 409 and "missing" in response.json()["detail"]


def test_reading_position_annotations_version_isolation_and_cas(client, db):
    pid, source = seed(client, 1)
    rid = source["revision_id"]
    source_path = f"/api/sources/revisions/{rid}"
    loc = client.post(source_path + "/locators", json={"conversation_id": "research", "kind": "text_lines", "line_start": 2, "line_end": 2}).json()
    path = f"/api/research/reading/{rid}"
    assert client.get(path, params={"conversation_id": "research"}).json()["user_read"] is False
    state = client.put(path, json={"conversation_id": "research", "expected_version": 0,
        "position": {"line_start": 2, "zoom": 1.3, "locator_id": loc["locator_id"]}, "user_read": True})
    assert state.status_code == 200, state.text
    assert client.put(path, json={"conversation_id": "research", "expected_version": 0, "position": {}}).status_code == 409
    annotations = []
    for kind in ("highlight", "annotation", "note"):
        r = client.post(path + "/annotations", json={"conversation_id": "research", "locator_id": loc["locator_id"],
            "kind": kind, "body": f"Note {kind}", "selected_text": "research sample 1"})
        assert r.status_code == 200, r.text
        annotations.append(r.json())
    first = annotations[0]
    r = client.patch(f'/api/research/annotations/{first["id"]}', json={"conversation_id": "research", "expected_version": 1, "body": "Edited"})
    assert r.status_code == 200
    assert client.get(path + "/annotations", params={"conversation_id": "research"}).json()["items"][0]["body"] == "Edited"
    assert len(list(db.scalars(select(AnnotationRevision).where(AnnotationRevision.annotation_id == first["id"])))) == 2
    assert client.patch(f'/api/research/annotations/{first["id"]}', json={"conversation_id": "research", "expected_version": 1, "body": "Stale"}).status_code == 409
    r = client.patch(f'/api/research/annotations/{first["id"]}', json={"conversation_id": "research", "expected_version": 2, "body": "Edited", "deleted": True})
    assert r.status_code == 200
    assert len(client.get(path + "/annotations", params={"conversation_id": "research"}).json()["items"]) == 2
    assert client.get(path, params={"conversation_id": "unrelated"}).status_code == 404
    updated = client.post("/api/sources/upload", files={"file": ("sample-1.md", b"new version")}, data={"conversation_id": "research", "request_id": "update-reading-source", "source_id": source["source_id"], "expected_version": 1}).json()
    new_path = f'/api/research/reading/{updated["revision_id"]}'
    assert client.get(new_path, params={"conversation_id": "research"}).json() == {"version": 0, "position": {}, "user_read": False}
    assert client.post(new_path + "/annotations", json={"conversation_id": "research", "locator_id": loc["locator_id"], "kind": "note"}).status_code == 409
    assert client.post(path + "/annotations", json={"conversation_id": "research", "locator_id": loc["locator_id"], "kind": "note", "selected_text": "invented"}).status_code == 409


def test_graph_edge_evidence_manual_correction_reextract_and_undo(client, db):
    from app.models import Reference
    from app.research_models import RelationEvidence, RelationRevision
    papers = [Paper(title=title, status="parsed", content_hash=f"hash{i}") for i,title in enumerate([
        "Original Research Source", "Neural Evidence Retrieval Transformer", "Corrected Target Experimental Study"])]
    db.add_all(papers); db.flush()
    ids = [p.id for p in papers]
    ref = Reference(paper_id=ids[0], ref_index=1, raw_text="Neural Evidence Retrieval Transformer", title="Neural Evidence Retrieval Transformer")
    db.add(ref); db.commit()
    graph = client.get("/api/graph").json()
    edge = graph["edges"][0]
    assert edge["relation_type"] == "uncertain"
    detail_url = '/api/research/relations/' + edge["relation_id"]
    detail = client.get(detail_url).json()
    assert detail["version"] == edge["relation_version"]
    assert detail["extracted"]["citation_sentence"] is None
    assert detail["extracted"]["location_status"] == "unavailable"
    corrected = client.patch(detail_url, json={"expected_version": detail["version"], "action":"correct", "reason":"Manually checked original", "target":ids[2], "relation_type":"shared_topic"})
    assert corrected.status_code == 200, corrected.text
    assert client.patch(detail_url, json={"expected_version": 1, "action":"exclude", "reason":"stale"}).status_code == 409
    refid = ref.id
    db.delete(db.get(Reference, refid)); db.commit()
    db.add(Reference(paper_id=ids[0], ref_index=1, raw_text="Neural Evidence Retrieval Transformer", title="Neural Evidence Retrieval Transformer")); db.commit()
    graph = client.get("/api/graph").json()
    edge = graph["edges"][0]
    assert edge["target"] == ids[2] and edge["relation_type"] == "shared_topic"
    assert edge["relation_id"] == detail["id"]
    r = client.patch(detail_url, json={"expected_version":edge["relation_version"],"action":"exclude","reason":"Excluded from current view"})
    assert r.status_code == 200
    assert client.get("/api/graph").json()["edges"] == []
    r = client.patch(detail_url, json={"expected_version":r.json()["version"],"action":"restore","restore_version":2,"reason":"Undo exclusion"})
    assert r.status_code == 200
    graph = client.get("/api/graph").json()
    assert graph["edges"][0]["target"] == ids[2]
    history = client.get(detail_url).json()["history"]
    assert len(history) == 4 and history[0]["action"] == "restore"
    assert history[-1]["document"]["extracted"]["target"] == ids[1]
    page = client.get("/api/graph?limit=2").json()
    assert len(page["nodes"]) == 2 and page["next_offset"] == 2
    last = client.get("/api/graph?limit=2&offset=2").json()
    assert len(last["nodes"]) == 1 and last["next_offset"] is None
    neighborhood = client.get('/api/graph', params={"focus":ids[0]}).json()
    assert {n["id"] for n in neighborhood["nodes"]} == {ids[0],ids[2]}
    # Removing the original automatic target cannot erase a reviewed correction.
    db.get(Paper, ids[1]).status = "failed"
    db.commit()
    graph = client.get('/api/graph').json()
    assert graph["edges"][0]["target"] == ids[2]
    assert '-.-' in client.get('/api/graph/diagram').json()["script"]


def test_agent_matrix_tools_bind_scope_and_preserve_human_status(client, db):
    from app.research_tools import execute, WRITE_TOOLS
    from app.source_store import SourceError
    ids=[seed(client,i)[0] for i in range(2)]
    args={"request_id":"agent-matrix-create-123","title":"Agent matrix","paper_ids":ids,"fields":[{"key":"m","label":"Methods","query":"method"}]}
    with pytest.raises(SourceError):
        execute("create_evidence_matrix",{**args,"conversation_id":"unrelated"},conversation_id="research")
    created=execute("create_evidence_matrix",args,conversation_id="research")
    assert execute("create_evidence_matrix",args,conversation_id="research")["matrix_id"]==created["matrix_id"]
    with pytest.raises(SourceError):
        execute("create_evidence_matrix",{**args,"title":"Changed request"},conversation_id="research")
    assert created["status"]=="saved"
    rid=created["matrix_id"]
    histories=list(db.scalars(select(MatrixRevision).where(MatrixRevision.matrix_id==rid)))
    assert histories[0].actor=="agent"
    with pytest.raises(SourceError):
        execute("read_evidence_matrix",{"matrix_id":rid},conversation_id="unrelated")
    result=execute("extract_evidence_matrix",{"matrix_id":rid,"expected_version":1,"paper_ids":ids,"field_keys":["m"]},conversation_id="research")
    assert result["not_executed"] and result["code"]=="migration_required"
    snapshot=client.get("/api/research/matrices/"+rid,params={"conversation_id":"research"}).json()
    assert extract(client,snapshot)["version"]==2
    result=execute("read_evidence_matrix",{"matrix_id":rid,"limit":1},conversation_id="research")
    assert result["next_offset"]==1 and len(result["papers"])==1
    cell=result["rows"][0]["cells"][ids[0]]
    assert cell["status"]=="unverified" and cell["manual"] is None
