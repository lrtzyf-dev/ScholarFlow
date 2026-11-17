"""Fixed-source citation signals, not a semantic or real-model evaluation."""
from copy import deepcopy
from io import BytesIO
from uuid import uuid4

import pytest
from sqlalchemy import text, update

from app import research_runs as runs, run_store, source_store as sources
from app.agent.checkpoint import AgentState
from app.db import SessionLocal
from app.models import Conversation, Paper, Reference
from app.runtime_models import RuntimeRun, RunAttempt
from app.source_models import SourceLocator, SourceSnapshot
from app.writing import quality_run
from tests.test_framework_evidence import seed, CID


def source(db, raw=b"Our method compares the performance and limitation of prior model [7].\nReferences\n[7] Smith A. Prior method. 2020.", media="text/plain", refs=None):
    pid = seed(db, raw, media)
    for number, entry in refs or [(7, "Smith A. Prior method. 2020.")]:
        db.add(Reference(paper_id=pid, ref_index=number, raw_text=entry))
    db.commit()
    runs._stopping.clear()
    return pid


def create(db, pid, ident=None, request="quality-request"):
    db.rollback()
    ident = ident or "research_" + uuid4().hex
    db.execute(text("BEGIN IMMEDIATE"))
    _, attempt, fresh = runs.create(db, kind="quality", run_id=ident, conversation_id=CID,
        inputs={"paper_id": pid}, request_id=request)
    db.commit()
    return ident, attempt.id, fresh


def execute(db, ident, attempt):
    try: runs.execute(ident, attempt)
    except Exception as error:
        runs._mark_failure(ident, attempt, error)
        raise
    db.expire_all()
    return runs.public(db, ident, CID)


def resume(db, ident):
    db.expire_all()
    revision = db.get(RuntimeRun, ident).revision
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    _, attempt, fresh = runs.resume(db, ident, conversation_id=CID, request_id="resume-" + uuid4().hex,
        expected_revision=revision)
    db.commit()
    assert fresh
    return attempt.id


def test_local_analysis_has_no_model_dependency_and_survives_new_session(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(runs, "capture_run_config", lambda *a: pytest.fail("Local quality requested a model binding"))
    pid = source(db)
    ident, attempt, _ = create(db, pid)
    value = execute(db, ident, attempt)
    assert value["outcome"] == "completed" and not value["can_resume"]
    assert value["model"]["mode"] == "local" and value["usage"]["call_count"] == 0
    assert value["result"]["source_acquired"] and not value["result"]["verified"]
    assert value["result"]["semantic_support"] == "unverified" and value["source_count"] == 1
    item = value["result"]["results"][0]
    assert item["ref_index"] == 7 and item["located_contexts"] == 1
    assert item["reference_entry_in_source"] and item["semantic_support"] == "unverified"
    evidence = item["evidence"][0]
    with SessionLocal() as reader:
        assert runs.public(reader, ident, CID)["result"] == value["result"]
        located = sources.read_locator(reader, evidence["locator_id"], scope=sources.SourceScope("conversation", CID))
        assert located["quote"] == evidence["quote"] and "[7]" in located["quote"]


def _bytes(fmt):
    content = "The proposed method compares model performance [7]."
    if fmt in ("txt", "md"): return (content + "\nReferences\n[7] Smith A. Prior method. 2020.").encode()
    if fmt == "csv": return ("kind,text\nbody," + content + "\n").encode()
    if fmt == "docx":
        from docx import Document
        doc = Document(); doc.add_paragraph(content)
        output = BytesIO(); doc.save(output); return output.getvalue()
    if fmt == "xlsx":
        from openpyxl import Workbook
        book = Workbook(); book.active.append([content])
        output = BytesIO(); book.save(output); return output.getvalue()
    import fitz
    doc = fitz.open(); page = doc.new_page(); page.insert_text((45, 45), content)
    raw = doc.tobytes(); doc.close(); return raw


@pytest.mark.parametrize("fmt,kind", [("txt", "text_lines"), ("md", "text_lines"),
    ("docx", "word_paragraphs"), ("xlsx", "sheet_cells"), ("csv", "sheet_cells"), ("pdf", "pdf_page")])
def test_all_six_formats_use_exact_fixed_source_locators(db, fmt, kind):
    from app.source_parsing import MEDIA
    pid = source(db, _bytes(fmt), MEDIA[fmt])
    ident, attempt, _ = create(db, pid)
    result = execute(db, ident, attempt)["result"]
    evidence = result["results"][0]["evidence"]
    assert evidence and evidence[0]["location"]["extraction_fingerprint"]
    located = sources.read_locator(db, evidence[0]["locator_id"], scope=sources.SourceScope("conversation", CID))
    assert located["kind"] == kind and located["quote"] == evidence[0]["quote"]
    assert located["revision_id"] == result["revision_id"]
    if fmt == "pdf": assert located["location"]["precision"] == "page"


def test_nonconsecutive_number_author_year_and_missing_are_distinguished(db):
    pid = source(db, b"Method compares performance [7].\nSmith (2020) proposes an improved model.\nOnly an unsupported superscript citation 12 appears here.\nReferences\n[12] Jones A. Other work. 2021.", refs=[
        (7, "Different A. Method. 2019."), (9, "Smith A. Prior method. 2020."), (12, "Jones A. Other work. 2021.")])
    ident, attempt, _ = create(db, pid)
    rows = execute(db, ident, attempt)["result"]["results"]
    assert [r["ref_index"] for r in rows] == [7, 9, 12]
    assert rows[0]["evidence"][0]["match_method"] == "numeric_brackets"
    assert not rows[0]["reference_entry_in_source"]
    assert rows[1]["evidence"][0]["match_method"] == "author_year"
    assert rows[2]["label"] == "not_located" and not rows[2]["evidence"]
    assert all(r["semantic_support"] == "unverified" for r in rows)


def test_old_input_is_preserved_after_mutable_library_entries_change(db):
    pid = source(db)
    ident, attempt, _ = create(db, pid)
    db.get(Paper, pid).title = "New title"; db.get(Paper, pid).content_hash = "a" * 64
    db.query(Reference).filter_by(paper_id=pid).one().raw_text = "A replacement entry"
    db.commit()
    result = execute(db, ident, attempt)["result"]
    assert result["title"] == "Neural evidence" and result["results"][0]["raw_text"] == "Smith A. Prior method. 2020."
    assert result["results"][0]["located_contexts"] == 1
    assert create(db, pid, ident=ident)[2] is False


def test_interrupted_local_analysis_only_completes_missing_references(db, monkeypatch):
    pid = source(db, b"A model method compares performance [2].\nAnother method discusses limitation [7].", refs=[(2, "One"), (7, "Two")])
    ident, attempt, _ = create(db, pid)
    original = runs.Execution.set_payload
    def stop_after_first(context, payload):
        original(context, payload)
        if payload["progress"]["completed"] == 1: raise runs.ResearchStopped("Synthetic local interruption")
    monkeypatch.setattr(runs.Execution, "set_payload", stop_after_first)
    with pytest.raises(runs.ResearchStopped): execute(db, ident, attempt)
    first = runs.public(db, ident, CID)
    assert first["can_resume"] and first["result"]["progress"] == {"completed": 1, "total": 2}
    assert not first["requires_unknown_confirmation"]
    monkeypatch.setattr(runs.Execution, "set_payload", original)
    scored = []; score = quality_run.score_occurrence
    monkeypatch.setattr(quality_run, "score_occurrence", lambda quote: scored.append(quote) or score(quote))
    next_attempt = resume(db, ident)
    final = execute(db, ident, next_attempt)
    assert final["outcome"] == "completed" and final["attempt_number"] == 2 and len(scored) == 1
    assert final["result"]["results"][0] == first["result"]["results"][0]
    assert final["usage"]["call_count"] == 0


@pytest.mark.parametrize("target", ["snapshot", "progress", "result", "locator", "raw"])
def test_tampered_fixed_evidence_progress_and_result_fail_closed(db, target):
    pid = source(db)
    ident, attempt, _ = create(db, pid)
    value = execute(db, ident, attempt)
    row = db.get(RunAttempt, attempt)
    checkpoint = deepcopy(row.checkpoint)
    job = checkpoint["context_archive"][runs.JOB_KEY]
    if target == "snapshot": job["source_snapshot"][0]["title"] = "Forged"
    elif target == "progress": job["quality_results"][0]["raw_text"] = "Forged"
    elif target == "result": checkpoint["result"]["payload"]["note"] = "Forged"
    elif target == "locator":
        lid = value["result"]["results"][0]["evidence"][0]["locator_id"]
        db.execute(update(SourceLocator).where(SourceLocator.id == lid).values(quote="Forged"))
    else:
        record = sources.read_revision(db, value["result"]["revision_id"], scope=sources.SourceScope("conversation", CID))
        db.execute(update(SourceSnapshot).where(SourceSnapshot.id == record["snapshot_id"]).values(raw_bytes=b"Forged"))
    row.checkpoint = checkpoint
    db.commit()
    with pytest.raises(sources.SourceCorruption): runs.public(db, ident, CID)


def test_quality_routes_scope_kind_and_run_center_target(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    pid = source(db)
    db.add(Conversation(id="quality-other", title="Other")); db.commit()
    monkeypatch.setattr(runs, "start", lambda ident: None)
    with TestClient(app) as client:
        rid = "research_" + uuid4().hex
        body = {"conversation_id": CID, "paper_id": pid, "run_id": rid, "request_id": "quality-http-request"}
        created = client.post("/api/writing/quality/runs", json=body)
        assert created.status_code == 200
        assert created.json()["model"]["mode"] == "local"
        assert client.post("/api/writing/quality/runs", json=body).json()["replayed"]
        assert len(client.get("/api/writing/quality/runs", params={"conversation_id": CID}).json()["items"]) == 1
        assert client.get(f"/api/writing/quality/runs/{rid}", params={"conversation_id": "quality-other"}).status_code == 404
        assert client.get(f"/api/writing/framework/runs/{rid}", params={"conversation_id": CID}).status_code == 404
        center = client.get("/api/run-center", params={"view": "all"}).json()
        item = next(i for i in center["items"] if i["target_run_id"] == rid)
        assert item["kind"] == item["target_kind"] == "quality"
        before = client.get(f"/api/writing/quality/runs/{rid}", params={"conversation_id": CID}).json()
        runs.recover_startup()
        after = client.get(f"/api/writing/quality/runs/{rid}", params={"conversation_id": CID}).json()
        assert before["status"] == "queued" and after["status"] == "interrupted" and after["can_resume"]


def test_full_backup_restores_local_analysis_and_original_quote(db, tmp_path):
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import config, data_backup
    from app.db import engine
    pid = source(db)
    ident, attempt, _ = create(db, pid)
    original = execute(db, ident, attempt)
    db.rollback()
    folder = tmp_path / "quality-backup-source"
    folder.mkdir()
    data_backup._database_snapshot(Path(engine.url.database), folder / config.settings.db_name)
    exported = data_backup.create_backup(folder, database=config.settings.db_name, origin_data_dir=config.DATA_DIR)
    target = tmp_path / "restored-quality"
    staged = data_backup.prepare_restore(target, exported["path"], expected_database=config.settings.db_name)
    data_backup.schedule_restore(target, staged["id"])
    data_backup.apply_pending_restore(target, expected_database=config.settings.db_name)
    restored_engine = create_engine("sqlite:///" + str(target / config.settings.db_name))
    try:
        with Session(restored_engine) as restored:
            result = runs.public(restored, ident, CID)
            assert result["result"] == original["result"] and result["status"] == "ended"
            evidence = result["result"]["results"][0]["evidence"][0]
            locator = sources.read_locator(restored, evidence["locator_id"], scope=sources.SourceScope("conversation", CID))
            assert locator["quote"] == evidence["quote"] and result["usage"]["call_count"] == 0
    finally:
        restored_engine.dispose()
