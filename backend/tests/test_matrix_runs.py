"""Durable matrix mechanisms over real immutable originals; no paid calls."""
from copy import deepcopy
from io import BytesIO
from uuid import uuid4
import hashlib

import pytest
from sqlalchemy import select, text, update

from app import matrix_run, research_runs as runs, research_store as matrices, source_store as sources
from app.db import SessionLocal
from app.models import Conversation, Paper
from app.research_models import MatrixRunSnapshot, MatrixRevision, ResearchMatrix
from app.runtime_models import RuntimeRun, RunAttempt
from app.source_models import SourceLibraryBinding, SourceLibraryImport
from app.timeutil import utcnow

CID = "matrix-durable"
FIELDS = [{"key": "method", "label": "Method", "query": "method performance"},
          {"key": "absent", "label": "Not located", "query": "quasar supernova"}]


@pytest.fixture(autouse=True)
def local_settings(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    runs._stopping.clear()


def raw_document(fmt, suffix=""):
    content = "The method compares model performance " + suffix
    if fmt in ("txt", "md"): return (content + ("\r\n" if fmt == "md" else "\n")).encode()
    if fmt == "csv": return ("text,result\n" + content + ",28.4\n").encode()
    if fmt == "docx":
        from docx import Document
        doc = Document(); doc.add_paragraph(content)
        output = BytesIO(); doc.save(output); return output.getvalue()
    if fmt == "xlsx":
        from openpyxl import Workbook
        book = Workbook(); book.active.append([content, 28.4])
        output = BytesIO(); book.save(output); book.close(); return output.getvalue()
    import fitz
    doc = fitz.open(); doc.new_page().insert_text((45, 45), content)
    raw = doc.tobytes(); doc.close(); return raw


def seed(db, formats=("md", "txt"), fields=None):
    from app.source_parsing import MEDIA
    db.add(Conversation(id=CID, title="Fixed matrix"))
    ids = []
    for number, fmt in enumerate(formats):
        raw = raw_document(fmt, str(number))
        paper = Paper(title="Research " + str(number), status="parsed", content_hash=hashlib.sha256(raw).hexdigest())
        db.add(paper); db.flush()
        scope = sources.SourceScope("library", paper.id)
        fixed = sources.capture_source(db, scope=scope,
            authorization=sources.SourceAuthorization(scope, "library:" + paper.id, "library"),
            kind="library", title=paper.title, raw_bytes=raw,
            text=raw.decode() if fmt in ("md", "txt") else None, media_type=MEDIA[fmt])
        db.add_all([SourceLibraryBinding(paper_id=paper.id, revision_id=fixed["revision_id"]),
            SourceLibraryImport(revision_id=fixed["revision_id"], paper_id=paper.id,
                raw_sha256=paper.content_hash, request_id="matrix-owned-test")])
        ids.append(paper.id)
    db.flush()
    item = matrices.create_matrix(db, CID, "Fixed methods", ids, deepcopy(fields or FIELDS))
    db.commit()
    return item, ids


def create(db, item, **selection):
    db.rollback()
    ident = "research_" + uuid4().hex
    db.execute(text("BEGIN IMMEDIATE"))
    _, attempt, fresh = runs.create(db, kind="matrix", run_id=ident, conversation_id=CID,
        inputs={"matrix_id": item.id, "expected_version": item.version,
                "field_keys": selection.get("field_keys"), "paper_ids": selection.get("paper_ids")},
        request_id="matrix-" + uuid4().hex)
    db.commit()
    assert fresh
    return ident, attempt.id


def execute(db, ident, attempt):
    try: runs.execute(ident, attempt)
    except Exception as error:
        runs._mark_failure(ident, attempt, error)
        raise
    db.expire_all()
    return runs.public(db, ident, CID)


def resume(db, ident, *, retry_unknown=False):
    db.expire_all()
    revision = db.get(RuntimeRun, ident).revision
    db.rollback(); db.execute(text("BEGIN IMMEDIATE"))
    _, attempt, fresh = runs.resume(db, ident, conversation_id=CID,
        request_id="resume-" + uuid4().hex, expected_revision=revision, retry_unknown=retry_unknown)
    db.commit()
    assert fresh
    return attempt.id


def apply(db, ident, version, request_id="apply-unique-request"):
    db.rollback(); db.execute(text("BEGIN IMMEDIATE"))
    replayed = matrix_run.apply_saved(db, ident, CID, version, request_id)
    db.commit(); db.expire_all()
    return runs.public(db, ident, CID), replayed


def edit(db, item, pid, status="not_found"):
    db.expire_all()
    item = matrices.matrix(db, item.id, CID, writing=True)
    document = deepcopy(item.document)
    document["rows"][0]["cells"][pid].update(manual={"value": "Keep this manual analysis", "actor": "local_user",
        "status": status, "note": "Reviewed exact source"}, status=status, verification_status="human_reviewed")
    item = matrices.save_matrix(db, item, document, item.version, "edit")
    db.commit()
    return item.version


def test_local_no_chat_credential_and_fixed_locators_survive_new_session(db, monkeypatch):
    from app.pipeline import embeddings
    monkeypatch.setattr(embeddings, "embedding_connection", lambda: pytest.fail("Local opened provider credential"))
    item, ids = seed(db)
    ident, attempt = create(db, item)
    value = execute(db, ident, attempt)
    assert value["outcome"] == "completed" and not value["can_resume"]
    assert value["result"]["progress"] == {"completed": 4, "total": 4}
    assert value["result"]["applied_version"] == 2 and value["usage"]["call_count"] == 0
    assert value["result"]["method_details"]["embedding"]["mode"] == "local"
    cells = value["result"]["document"]["rows"]
    assert all(c["status"] == "not_found" and c["extraction_status"] == "not_found" for c in cells[1]["cells"].values())
    with SessionLocal() as reader:
        assert runs.public(reader, ident, CID)["result"] == value["result"]
        for cell in cells[0]["cells"].values():
            assert cell["verification_status"] == cell["status"] == "unverified" and cell["evidence"]
            e = cell["evidence"][0]
            loc = sources.read_locator(reader, e["locator_id"], scope=sources.SourceScope("conversation", CID))
            assert loc["quote"] == e["quote"] and loc["revision_id"] == e["revision_id"]


@pytest.mark.parametrize("fmt,kind", [("txt", "text_lines"), ("md", "text_lines"),
    ("docx", "word_paragraphs"), ("xlsx", "sheet_cells"), ("csv", "sheet_cells"), ("pdf", "pdf_page")])
def test_six_formats_have_valid_locators(db, fmt, kind):
    item, ids = seed(db, (fmt, "md"), fields=FIELDS[:1])
    ident, attempt = create(db, item)
    value = execute(db, ident, attempt)
    evidence = value["result"]["document"]["rows"][0]["cells"][ids[0]]["evidence"]
    assert evidence
    loc = sources.read_locator(db, evidence[0]["locator_id"], scope=sources.SourceScope("conversation", CID))
    assert loc["kind"] == kind and loc["quote"] == evidence[0]["quote"]
    if fmt == "pdf": assert loc["location"]["precision"] == "page"


def test_interruption_resumes_only_missing_cells(db, monkeypatch):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    save = runs.Execution.set_payload
    def stop(context, payload):
        save(context, payload)
        if payload["progress"]["completed"] == 1: raise runs.ResearchStopped("Synthetic local stop")
    monkeypatch.setattr(runs.Execution, "set_payload", stop)
    with pytest.raises(runs.ResearchStopped): execute(db, ident, attempt)
    first = runs.public(db, ident, CID)
    assert first["can_resume"] and first["result"]["progress"]["completed"] == 1
    monkeypatch.setattr(runs.Execution, "set_payload", save)
    ranked = []; rank = matrix_run._rank
    monkeypatch.setattr(matrix_run, "_rank", lambda *a, **k: ranked.append(a[0]["paper_id"]) or rank(*a, **k))
    final = execute(db, ident, resume(db, ident))
    assert final["outcome"] == "completed" and final["attempt_number"] == 2 and len(ranked) == 3
    # Committing the matrix adds revision history; it must not change a known
    # extraction receipt or call the ranking operation again for that cell.
    for key in ("evidence", "quote", "status", "manual", "verification_status"):
        assert final["result"]["document"]["rows"][0]["cells"][ids[0]][key] == first["result"]["document"]["rows"][0]["cells"][ids[0]][key]
    old_results = db.get(RunAttempt, attempt).checkpoint["context_archive"][runs.JOB_KEY]["cell_results"]
    final_results = db.get(RunAttempt, final["attempt_id"]).checkpoint["context_archive"][runs.JOB_KEY]["cell_results"]
    assert final_results[:1] == old_results


def test_conflict_partial_apply_delivery_preserves_terminal_attempt_and_manual(db):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    version = edit(db, item, ids[0])
    conflict = execute(db, ident, attempt)
    assert conflict["outcome"] == "partial" and not conflict["can_resume"]
    assert conflict["result"]["apply_conflict"]["requires_explicit_apply"]
    old = deepcopy(db.get(RunAttempt, attempt).checkpoint)
    with pytest.raises(sources.SourceConflict, match="合并"):
        resume(db, ident)
    db.rollback()
    final, replayed = apply(db, ident, version)
    assert not replayed and final["outcome"] == "completed" and final["attempt_number"] == 2
    assert final["usage"]["call_count"] == 0 and final["result"]["applied_version"] == 3
    assert final["result"]["document"] == matrices.matrix(db, item.id, CID).document
    assert db.get(RuntimeRun, ident).goal_id == db.get(RunAttempt, attempt).checkpoint["goal_id"]
    assert db.get(RunAttempt, attempt).checkpoint == old
    current = matrices.matrix(db, item.id, CID).document["rows"][0]["cells"][ids[0]]
    assert current["manual"]["value"] == "Keep this manual analysis" and current["verification_status"] == "human_reviewed"
    assert current["status"] == "not_found" and not current["evidence"]
    assert current["extraction_candidate"]["evidence"] and current["extraction_candidate"]["requires_review"]
    again, replayed = apply(db, ident, version)
    assert replayed and again["revision"] == final["revision"] and again["attempt_id"] == final["attempt_id"]
    with pytest.raises(sources.SourceConflict): apply(db, ident, version + 1)
    db.rollback()
    edit(db, item, ids[1])
    latest = deepcopy(matrices.matrix(db, item.id, CID).document)
    assert runs.public(db, ident, CID)["result"] == final["result"]
    assert matrices.matrix(db, item.id, CID).document == latest


def test_partial_apply_then_resume_finishes_missing_cells_without_rewriting_delivery(db, monkeypatch):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    save = runs.Execution.set_payload
    def stop(context, payload):
        save(context, payload)
        if payload["progress"]["completed"] == 1: raise runs.ResearchStopped("Synthetic local stop")
    monkeypatch.setattr(runs.Execution, "set_payload", stop)
    with pytest.raises(runs.ResearchStopped): execute(db, ident, attempt)
    partial, _ = apply(db, ident, 1)
    assert partial["outcome"] == "partial" and partial["can_resume"] and partial["attempt_number"] == 2
    old = deepcopy(db.get(RunAttempt, partial["attempt_id"]).checkpoint)
    monkeypatch.setattr(runs.Execution, "set_payload", save)
    final = execute(db, ident, resume(db, ident))
    assert final["outcome"] == "completed" and final["result"]["applied_version"] == 3 and final["attempt_number"] == 3
    assert db.get(RunAttempt, partial["attempt_id"]).checkpoint == old


def test_api_scope_fenced_cancel_and_legacy_routes_do_not_execute(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.agent import tools
    item, ids = seed(db)
    db.add(Conversation(id="matrix-other", title="Other")); db.commit()
    monkeypatch.setattr(runs, "start", lambda ident: None)
    monkeypatch.setattr(matrix_run, "execute", lambda *a: pytest.fail("Migration dispatched"))
    with TestClient(app) as client:
        ident = "research_" + uuid4().hex
        body = {"conversation_id": CID, "run_id": ident, "request_id": "matrix-http-create", "matrix_id": item.id, "expected_version": 1}
        response = client.post("/api/research/matrix-runs", json=body)
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["kind"] == "matrix" and first["attempt_id"]
        center = client.get("/api/run-center", params={"view": "all"}).json()
        target = next(i for i in center["items"] if i["target_run_id"] == ident)
        assert target["kind"] == target["target_kind"] == "matrix"
        assert client.post("/api/research/matrix-runs", json=body).json()["replayed"]
        assert len(client.get("/api/research/matrix-runs", params={"conversation_id": CID, "matrix_id": item.id}).json()["items"]) == 1
        assert client.get("/api/research/matrix-runs/" + ident, params={"conversation_id": "matrix-other"}).status_code == 404
        assert client.post("/api/research/matrix-runs/" + ident + "/cancel", json={"conversation_id": CID}).status_code == 422
        fence = {"conversation_id": CID, "expected_revision": first["revision"] - 1, "expected_attempt_id": first["attempt_id"]}
        assert client.post("/api/research/matrix-runs/" + ident + "/cancel", json=fence).status_code == 409
        fence["expected_revision"] = first["revision"]
        stopped = client.post("/api/research/matrix-runs/" + ident + "/cancel", json=fence)
        assert stopped.status_code == 200
        resumed = client.post("/api/research/matrix-runs/" + ident + "/resume", json={"conversation_id": CID,
            "request_id": "matrix-fenced-resume", "expected_revision": stopped.json()["revision"]})
        assert resumed.status_code == 200 and resumed.json()["attempt_id"] != first["attempt_id"]
        fence["expected_revision"] = resumed.json()["revision"]
        assert client.post("/api/research/matrix-runs/" + ident + "/cancel", json=fence).status_code == 409
        assert client.get("/api/research/matrix-runs/" + ident, params={"conversation_id": CID}).json()["attempt_id"] == resumed.json()["attempt_id"]
        for path, request in [(f"/api/research/matrices/{item.id}/extract", {"conversation_id": CID, "expected_version": 1}),
                              ("/api/papers/compare", {"conversation_id": CID, "paper_ids": ids})]:
            legacy = client.post(path, json=request)
            assert legacy.status_code == 409 and legacy.json()["detail"]["not_executed"]
    assert all(t["name"] != "compare_papers" for t in tools.TOOL_SPECS)
    from app import research_tools
    assert all(s["function"]["name"] != "extract_evidence_matrix" for s in research_tools.TOOL_SPECS)
    legacy = research_tools.execute("extract_evidence_matrix", {"matrix_id": item.id}, conversation_id=CID)
    assert legacy["not_executed"] and legacy["code"] == "migration_required"


def api_embeddings(db, item, monkeypatch, *, compatible=True, fail_at=None, dimension=8):
    """Use production APIEmbedder+ledger with an explicit synthetic HTTP exit."""
    import httpx
    import numpy as np
    from app.config import settings
    from app.pipeline import embeddings
    from app.pipeline.vectordb import NumpyVectorStore
    from app.models import Chunk
    from app.source_documents import prepare_extraction, save_extraction, read_document
    monkeypatch.setattr(settings, "embedding_api_key", "synthetic-matrix-credential")
    monkeypatch.setattr(settings, "embedding_model", "synthetic-vector-model")
    monkeypatch.setattr(settings, "embedding_base_url", "https://matrix.embedding.test/v1")
    if compatible:
        vectors = NumpyVectorStore()
        for paper in item.document["papers"]:
            scope = sources.SourceScope("conversation", CID)
            prepared = prepare_extraction(db, paper["revision_id"], scope=scope)
            if prepared: save_extraction(db, paper["revision_id"], scope=scope, extraction=prepared)
            doc = read_document(db, paper["revision_id"], scope=scope)
            for number, block in enumerate(doc["blocks"]):
                if not block.get("text", "").strip(): continue
                quote = ("".join(doc["text"].splitlines(keepends=True)[block["line_start"] - 1:block["line_end"]])
                    if block["kind"] == "text_line" else block["text"])
                chunk = Chunk(paper_id=paper["id"], chunk_index=number, content=quote)
                db.add(chunk); db.flush()
                vectors.add([chunk.id], [paper["id"]], np.asarray([[1.0] + [0.0] * 7], dtype=np.float32))
        db.commit()
    calls = []
    def transport(request):
        assert str(request.url) == "https://matrix.embedding.test/v1/embeddings"
        calls.append(request)
        if len(calls) == fail_at: raise httpx.ReadError("Synthetic unknown paid response", request=request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] + [0.0] * (dimension - 1)}],
            "usage": {"prompt_tokens": 5, "total_tokens": 5}})
    client_class, embedder_class = httpx.Client, embeddings.APIEmbedder
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client_class(*a, **k, transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(embeddings, "APIEmbedder", lambda *a, **k: embedder_class(*a, **k, synthetic=True))
    return calls


def test_api_query_once_per_field_production_meter_and_known_response_resume(db, monkeypatch):
    from app.ledger_models import CallRecord
    from app.call_ledger import summarize_calls
    item, ids = seed(db)
    calls = api_embeddings(db, item, monkeypatch)
    ident, attempt = create(db, item)
    save = runs.Execution.set_payload
    def stop(context, payload):
        save(context, payload)
        if payload["progress"]["completed"] == 1: raise runs.ResearchStopped("Synthetic post-response stop")
    monkeypatch.setattr(runs.Execution, "set_payload", stop)
    with pytest.raises(runs.ResearchStopped): execute(db, ident, attempt)
    first = runs.public(db, ident, CID)
    assert len(calls) == 1 and first["usage"]["call_count"] == 0 and not first["requires_unknown_confirmation"]
    assert summarize_calls(run_id=ident, include_synthetic=True, db=db)["synthetic_call_count"] == 1
    monkeypatch.setattr(runs.Execution, "set_payload", save)
    final = execute(db, ident, resume(db, ident))
    assert len(calls) == 2 and final["outcome"] == "completed" and final["usage"]["call_count"] == 0
    assert summarize_calls(run_id=ident, include_synthetic=True, db=db)["synthetic_call_count"] == 2
    records = list(db.scalars(select(CallRecord).where(CallRecord.run_id == ident)))
    assert len(records) == 2 and all(c.synthetic and c.service == "embedding" for c in records)
    assert {c["method"] for c in final["result"]["method_details"]["cells"]} == {"api_embedding_fts_rrf"}


def test_unknown_api_response_needs_explicit_retry_and_reuses_fixed_corpus(db, monkeypatch):
    item, ids = seed(db)
    calls = api_embeddings(db, item, monkeypatch, fail_at=2)
    ident, attempt = create(db, item)
    with pytest.raises(Exception, match="Synthetic|网络|连接|响应|接口"):
        execute(db, ident, attempt)
    first = runs.public(db, ident, CID)
    assert first["result"]["progress"]["completed"] == 2 and first["requires_unknown_confirmation"]
    before = db.scalar(select(MatrixRunSnapshot.fingerprint).where(MatrixRunSnapshot.run_id == ident, MatrixRunSnapshot.kind == "corpus"))
    with pytest.raises(sources.SourceConflict, match="未确认|核对"):
        resume(db, ident)
    db.rollback()
    assert len(calls) == 2
    final = execute(db, ident, resume(db, ident, retry_unknown=True))
    assert len(calls) == 3 and final["outcome"] == "completed" and not final["requires_unknown_confirmation"]
    assert before == db.scalar(select(MatrixRunSnapshot.fingerprint).where(MatrixRunSnapshot.run_id == ident, MatrixRunSnapshot.kind == "corpus"))


def test_api_no_compatible_vectors_is_explicit_lexical_zero_http(db, monkeypatch):
    item, ids = seed(db)
    calls = api_embeddings(db, item, monkeypatch, compatible=False)
    ident, attempt = create(db, item)
    final = execute(db, ident, attempt)
    assert not calls and final["usage"]["call_count"] == 0
    assert {c["method"] for c in final["result"]["method_details"]["cells"]} == {"lexical_no_compatible_fixed_vectors"}
    assert final["result"]["method_details"]["embedding"]["mode"] == "api"


def test_query_budget_is_frozen_and_exhaustion_pauses_without_unknown_dispatch(db, monkeypatch):
    from app.resource_policy import update_policy
    from app.resource_budget import ResourceBudgetExceeded
    from app.resource_models import ResourceBudget, ResourceReservation
    item, ids = seed(db)
    calls = api_embeddings(db, item, monkeypatch)
    update_policy(db, CID, {"currency": "USD", "amount_limit": "0.01", "request_estimate": "0.01"}, 0)
    db.commit()
    ident, attempt = create(db, item)
    binding = db.get(RunAttempt, attempt).checkpoint["runtime_config"]
    budget_id = binding["resource_budget_id"]
    assert db.get(ResourceBudget, budget_id).owner_run_id == ident
    with pytest.raises(ResourceBudgetExceeded): execute(db, ident, attempt)
    value = runs.public(db, ident, CID)
    assert len(calls) == 1 and value["status"] == "paused" and value["resource_pause"]["kind"] == "budget"
    assert not value["requires_unknown_confirmation"] and value["result"]["progress"]["completed"] == 2
    assert len(list(db.scalars(select(ResourceReservation).where(ResourceReservation.budget_id == budget_id)))) == 1


def test_embedding_credential_change_refuses_resume_without_new_http(db, monkeypatch):
    from app.config import settings
    item, ids = seed(db)
    calls = api_embeddings(db, item, monkeypatch, fail_at=1)
    ident, attempt = create(db, item)
    with pytest.raises(Exception): execute(db, ident, attempt)
    monkeypatch.setattr(settings, "embedding_api_key", "synthetic-replaced-credential")
    with pytest.raises(sources.SourceConflict, match="配置已变化"):
        resume(db, ident, retry_unknown=True)
    db.rollback()
    assert len(calls) == 1


def test_unavailable_protected_embedding_credential_does_not_silently_become_local(db, monkeypatch):
    from app import config
    from app.credential_protection import CredentialProtectionError
    item, ids = seed(db)
    monkeypatch.setattr(config, "credential_status", lambda: {"embedding_api_key": "unavailable"})
    with pytest.raises(CredentialProtectionError): create(db, item)
    db.rollback()
    assert not list(db.scalars(select(RuntimeRun)))


def test_embedding_connection_and_fingerprint_are_captured_under_same_settings_lock(db, monkeypatch):
    import threading
    from app import config
    from app.pipeline import embeddings, vectordb
    item, ids = seed(db)
    monkeypatch.setattr(config.settings, "embedding_api_key", "synthetic-old-key")
    monkeypatch.setattr(config.settings, "embedding_model", "synthetic-old-model")
    monkeypatch.setattr(config.settings, "embedding_base_url", "https://matrix.embedding.test/v1")
    entered, attempted, written = threading.Event(), threading.Event(), threading.Event()
    before = vectordb.current_embedding_fingerprint()
    real_connection = embeddings.embedding_connection
    def connection():
        value = real_connection()
        entered.set()
        assert attempted.wait(3)
        assert not written.is_set()
        return value
    def change():
        assert entered.wait(3)
        attempted.set()
        with config._settings_lock:
            config.settings.embedding_model = "synthetic-new-model"
            written.set()
    monkeypatch.setattr(embeddings, "embedding_connection", connection)
    worker = threading.Thread(target=change)
    worker.start()
    try: binding = matrix_run.capture_config(db, CID)
    finally: worker.join(timeout=5)
    assert written.is_set() and not worker.is_alive()
    assert binding["matrix_embedding"]["model_id"] == "synthetic-old-model"
    assert binding["matrix_embedding_fingerprint"] == before
    assert vectordb.current_embedding_fingerprint() != before


def test_library_head_and_title_changes_do_not_replace_admitted_fixed_version(db):
    item, ids = seed(db, fields=FIELDS[:1])
    ident, attempt = create(db, item)
    before = deepcopy(item.document["papers"])
    paper = db.get(Paper, ids[0]); paper.title = "Changed live title"; paper.content_hash = "a" * 64
    db.commit()
    result = execute(db, ident, attempt)
    assert result["result"]["document"]["papers"] == before
    assert result["result"]["document"]["rows"][0]["cells"][ids[0]]["evidence"][0]["source_sha256"] == before[0]["content_sha256"]


def test_query_dimension_change_keeps_known_response_and_does_not_silently_fallback(db, monkeypatch):
    item, ids = seed(db)
    calls = api_embeddings(db, item, monkeypatch, dimension=5)
    ident, attempt = create(db, item)
    with pytest.raises(sources.SourceConflict, match="维度"):
        execute(db, ident, attempt)
    assert len(calls) == 1 and not runs.public(db, ident, CID)["requires_unknown_confirmation"]
    with pytest.raises(sources.SourceConflict, match="维度"):
        execute(db, ident, resume(db, ident))
    assert len(calls) == 1


@pytest.mark.parametrize("target", ["matrix_json", "matrix_bytes", "corpus_json", "corpus_bytes", "missing_corpus",
    "progress", "payload", "locator", "raw", "extraction", "source_scope"])
def test_corruption_or_revoked_source_never_rebuilds_or_sends_http(db, monkeypatch, target):
    from app.source_models import Source, SourceSnapshot, SourceExtraction, SourceLocator
    item, ids = seed(db)
    ident, attempt = create(db, item)
    value = execute(db, ident, attempt)
    checkpoint = deepcopy(db.get(RunAttempt, attempt).checkpoint)
    job = checkpoint["context_archive"][runs.JOB_KEY]
    evidence = value["result"]["document"]["rows"][0]["cells"][ids[0]]["evidence"][0]
    if target.startswith(("matrix_", "corpus_")):
        kind, changed = target.split("_")
        row = db.scalar(select(MatrixRunSnapshot).where(MatrixRunSnapshot.run_id == ident, MatrixRunSnapshot.kind == kind))
        if changed == "json":
            document = deepcopy(row.document); document["forged"] = True
            db.execute(update(MatrixRunSnapshot).where(MatrixRunSnapshot.id == row.id).values(document=document))
        else:
            db.execute(update(MatrixRunSnapshot).where(MatrixRunSnapshot.id == row.id).values(vectors=b"Forged"))
    elif target == "missing_corpus":
        from sqlalchemy import delete
        db.execute(delete(MatrixRunSnapshot).where(MatrixRunSnapshot.run_id == ident, MatrixRunSnapshot.kind == "corpus"))
    elif target == "progress": job["cell_results"][0]["cell"]["quote"] = "Forged"
    elif target == "payload": checkpoint["result"]["payload"]["progress"]["completed"] = 999
    elif target == "locator":
        db.execute(update(SourceLocator).where(SourceLocator.id == evidence["locator_id"]).values(quote="Forged"))
    elif target == "raw":
        record = sources.read_revision(db, evidence["revision_id"], scope=sources.SourceScope("conversation", CID))
        db.execute(update(SourceSnapshot).where(SourceSnapshot.id == record["snapshot_id"]).values(raw_bytes=b"Forged"))
    elif target == "extraction":
        db.execute(update(SourceExtraction).where(SourceExtraction.revision_id == evidence["revision_id"]).values(fingerprint="a" * 64))
    else:
        db.execute(update(Source).where(Source.id == evidence["source_id"]).values(scope_id="another-conversation"))
    db.get(RunAttempt, attempt).checkpoint = checkpoint
    db.commit()
    monkeypatch.setattr(matrix_run, "_prepare_corpus", lambda *a: pytest.fail("Corrupt job rebuilt corpus"))
    with pytest.raises(sources.SourceError): runs.public(db, ident, CID)
    with pytest.raises(sources.SourceError): apply(db, ident, 2, "corrupt-apply-request")
    db.rollback()


def test_snapshots_are_append_only(db):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    row = db.scalar(select(MatrixRunSnapshot).where(MatrixRunSnapshot.run_id == ident))
    row.document = {"forged": "replace"}
    with pytest.raises(ValueError, match="不可原地"):
        db.flush()
    db.rollback()


def test_restart_recovers_queued_run_and_keeps_snapshot_and_admission_identity(db):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    before = db.scalar(select(MatrixRunSnapshot.fingerprint).where(MatrixRunSnapshot.run_id == ident))
    runs.recover_startup(); db.expire_all()
    partial = runs.public(db, ident, CID)
    assert partial["status"] == "interrupted" and partial["can_resume"]
    final = execute(db, ident, resume(db, ident))
    assert final["outcome"] == "completed" and final["attempt_number"] == 2
    assert db.scalar(select(MatrixRunSnapshot.fingerprint).where(MatrixRunSnapshot.run_id == ident,
        MatrixRunSnapshot.kind == "matrix")) == before


def test_automatic_database_commit_before_checkpoint_is_replayed_once(db, monkeypatch):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    original = runs.Execution.set_payload
    def crash_after_commit(context, payload):
        if payload.get("applied_version") is not None: raise OSError("Synthetic checkpoint interruption after database commit")
        return original(context, payload)
    monkeypatch.setattr(runs.Execution, "set_payload", crash_after_commit)
    with pytest.raises(OSError): execute(db, ident, attempt)
    db.expire_all()
    assert matrices.matrix(db, item.id, CID).version == 2
    before = list(db.scalars(select(MatrixRevision.id).where(MatrixRevision.matrix_id == item.id)))
    monkeypatch.setattr(runs.Execution, "set_payload", original)
    monkeypatch.setattr(matrix_run, "_rank", lambda *a, **k: pytest.fail("Known cells reranked"))
    final = execute(db, ident, resume(db, ident))
    assert final["outcome"] == "completed" and final["result"]["applied_version"] == 2
    assert list(db.scalars(select(MatrixRevision.id).where(MatrixRevision.matrix_id == item.id))) == before


@pytest.mark.parametrize("changed", ["schema", "source"])
def test_explicit_merge_rejects_changed_schema_or_fixed_source(db, changed):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    document = deepcopy(item.document)
    if changed == "schema": document["dimensions"][0]["query"] = "different field intent"
    else: document["papers"][0]["content_sha256"] = "a" * 64
    matrices.save_matrix(db, item, document, 1, "schema")
    db.commit()
    result = execute(db, ident, attempt)
    assert result["outcome"] == "partial" and result["result"]["progress"]["completed"] == 4
    with pytest.raises(sources.SourceConflict, match="字段|来源"):
        apply(db, ident, 2)
    db.rollback()
    assert db.get(RuntimeRun, ident).current_attempt_id == attempt
    assert len(list(db.scalars(select(MatrixRevision).where(MatrixRevision.matrix_id == item.id)))) == 2


@pytest.mark.parametrize("status", ["supported", "conflict", "not_found"])
def test_retrieval_candidates_never_borrow_human_review_or_replace_reviewed_evidence(db, monkeypatch, status):
    item, ids = seed(db, fields=FIELDS[:1])
    ident, attempt = create(db, item)
    execute(db, ident, attempt)
    db.expire_all(); item = matrices.matrix(db, item.id, CID)
    edit(db, item, ids[0], status=status)
    db.expire_all(); item = matrices.matrix(db, item.id, CID)
    if status == "not_found":
        doc = deepcopy(item.document)
        doc["rows"][0]["cells"][ids[0]].update(evidence=[], quote="")
        matrices.save_matrix(db, item, doc, item.version, "edit"); db.commit()
        item = matrices.matrix(db, item.id, CID)
    before = deepcopy(item.document["rows"][0]["cells"][ids[0]])
    monkeypatch.setattr(matrix_run, "_rank", lambda *a, **k: ([], "local_hash_ngram_fts_rrf"))
    new_id, new_attempt = create(db, item)
    value = execute(db, new_id, new_attempt)
    cell = value["result"]["document"]["rows"][0]["cells"][ids[0]]
    saved = matrices.matrix(db, item.id, CID).document["rows"][0]["cells"][ids[0]]
    for result in (cell, saved):
        for key in ("manual", "status", "verification_status", "evidence", "quote"):
            assert result[key] == before[key]
        candidate = result["extraction_candidate"]
        assert candidate["requires_review"] and candidate["verification_status"] == "unverified"
        assert candidate["extraction_status"] == "not_found" and not candidate["evidence"]
    package = matrices.export_matrix(db, matrices.matrix(db, item.id, CID))
    assert matrices.validate_import(db, package, CID)["rows"][0]["cells"][ids[0]]["evidence"] == before["evidence"]
    forged = deepcopy(package)
    forged["document"]["rows"][0]["cells"][ids[0]]["extraction_candidate"]["quote"] = "Forged new quote"
    with pytest.raises(sources.SourceError, match="候选"):
        matrices.validate_import(db, forged, CID)


def test_candidate_adoption_is_explicit_binds_digest_and_keeps_old_review_history(db):
    from fastapi.testclient import TestClient
    from app.main import app
    item, ids = seed(db, fields=FIELDS[:1])
    edit(db, item, ids[0])
    item = matrices.matrix(db, item.id, CID)
    ident, attempt = create(db, item)
    execute(db, ident, attempt)
    current = matrices.matrix(db, item.id, CID)
    candidate = current.document["rows"][0]["cells"][ids[0]]["extraction_candidate"]
    payload = {"conversation_id": CID, "expected_version": current.version, "field_key": "method", "paper_id": ids[0],
        "value": "Reviewed the proposed new quote", "note": "Checked original", "status": "supported",
        "use_candidate": True, "candidate_sha256": "f" * 64}
    with TestClient(app) as client:
        path = "/api/research/matrices/" + item.id + "/cell"
        assert client.patch(path, json=payload).status_code == 409
        payload["candidate_sha256"] = candidate["sha256"]
        result = client.patch(path, json=payload)
        assert result.status_code == 200, result.text
        cell = result.json()["document"]["rows"][0]["cells"][ids[0]]
        assert cell["evidence"] == candidate["evidence"] and cell["quote"] == candidate["quote"]
        assert cell["verification_status"] == "human_reviewed" and cell["status"] == "supported"
        assert "extraction_candidate" not in cell and cell["extraction_history"][-1]["manual"]["value"] == "Keep this manual analysis"
        assert client.patch(path, json=payload).status_code == 409


def test_archived_completed_read_and_interrupted_resume_authorization(db, monkeypatch):
    item, ids = seed(db)
    ident, attempt = create(db, item)
    done = execute(db, ident, attempt)
    db.get(Conversation, CID).archived_at = utcnow(); db.commit()
    assert runs.public(db, ident, CID)["result"] == done["result"]
    with pytest.raises(sources.SourceConflict, match="归档"):
        apply(db, ident, 2)
    db.rollback()


def test_full_backup_restores_matrix_snapshots_and_conversation_delete_removes_only_owned_data(db, tmp_path):
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from fastapi.testclient import TestClient
    from app.main import app
    from app import config, data_backup, data_cleanup
    from app.db import engine
    item, ids = seed(db)
    ident, attempt = create(db, item)
    original = execute(db, ident, attempt)
    row_ids = list(db.scalars(select(MatrixRunSnapshot.id).where(MatrixRunSnapshot.run_id == ident)))
    assert len(row_ids) == 2
    db.rollback()
    folder = tmp_path / "matrix-backup-source"; folder.mkdir()
    data_backup._database_snapshot(Path(engine.url.database), folder / config.settings.db_name)
    exported = data_backup.create_backup(folder, database=config.settings.db_name, origin_data_dir=config.DATA_DIR)
    target = tmp_path / "restored-matrix"
    staged = data_backup.prepare_restore(target, exported["path"], expected_database=config.settings.db_name)
    data_backup.schedule_restore(target, staged["id"])
    data_backup.apply_pending_restore(target, expected_database=config.settings.db_name)
    restored_engine = create_engine("sqlite:///" + str(target / config.settings.db_name))
    try:
        with Session(restored_engine) as restored:
            value = runs.public(restored, ident, CID)
            assert value["result"] == original["result"]
            e = value["result"]["document"]["rows"][0]["cells"][ids[0]]["evidence"][0]
            assert sources.read_locator(restored, e["locator_id"], scope=sources.SourceScope("conversation", CID))["quote"] == e["quote"]
            assert all(restored.get(MatrixRunSnapshot, row_id) is not None for row_id in row_ids)
    finally: restored_engine.dispose()
    before = data_cleanup.preview(db, CID)
    assert before["records"]["research_matrix_run_snapshots"] == 2
    assert before["storage_preview"]["by_table"]["research_matrix_run_snapshots"] > 0
    with TestClient(app) as client:
        response = client.delete("/api/conversations/" + CID)
        assert response.status_code == 200, response.text
    db.expire_all()
    assert all(db.get(MatrixRunSnapshot, row_id) is None for row_id in row_ids)
    assert all(db.get(Paper, pid) is not None for pid in ids)
    assert data_cleanup.preview(db, CID)["total_records"] == 0

