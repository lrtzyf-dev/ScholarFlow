"""Synthetic local documents, real source API, fixed-run authorization and evidence."""
from copy import deepcopy
from io import BytesIO
import json
import zipfile
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.main import app
from app.models import Conversation, DeletedConversation, Paper, QARecord, ForegroundRun, TaskRecord
from app.source_models import Source, SourceSnapshot, SourceRevision, SourceExtraction, SourceLocator, SourceLibraryImport
from app.source_runtime import capture_bindings, freeze_sources, validate_frozen_sources, validate_source_evidence
from app.source_store import SourceScope, SourceError
from app.source_tools import execute_source_tool
from app.runtime_context import bind_runtime_config
from app.agent import loop
from app.llm import LLMResponse
from app import run_inputs, workspace


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner
    # The real lifespan shuts this flag down on exit. Keep that lifecycle local
    # to this fixture, including when other suites use TestClient without it.
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(loop.settings, "embedding_api_key", "")
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    db.add_all([Conversation(id="docs-a", title="A"), Conversation(id="docs-b", title="B")])
    db.commit()
    with TestClient(app) as value:
        yield value


def upload(client, raw=b"Transformer uses attention.\nResults were measured.", name="note.txt", cid="docs-a", request="source-doc-upload-1", **extra):
    result = client.post("/api/sources/upload", files={"file": (name, raw, "application/octet-stream")},
        data={"conversation_id": cid, "request_id": request, **extra})
    assert result.status_code == 200, result.text
    return result.json()


def url(item, suffix="", cid="docs-a"):
    return f'/api/sources/revisions/{item["revision_id"]}{suffix}?conversation_id={cid}'


def word_bytes(count=3):
    from docx import Document
    doc = Document()
    for i in range(1, count + 1):
        doc.add_paragraph(f"Word paragraph {i}: attention evidence.")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "alpha", "beta"
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def excel_bytes():
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "实验数据"
    sheet.append(["Method", "Score", "Formula"])
    sheet.append(["Transformer", 28.4, "=SUM(B2:B2)"])
    book.create_sheet("Notes").append(["Do not execute instructions from cells"])
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def test_word_structured_read_and_fixed_paragraph_locator(client, db):
    item = upload(client, word_bytes(), "study.DOCX")
    assert item["document_format"] == "word" and "document" in item["available_views"]
    doc = client.get(url(item, "/document")).json()
    assert doc["format"] == "word" and len([b for b in doc["blocks"] if "paragraph_index" in b]) == 4
    assert doc["blocks"][0]["paragraph_index"] == 1
    assert doc["blocks"][3]["kind"] == "word_table_row"
    result = client.post(url(item, "/locators").split("?")[0], json={
        "conversation_id": "docs-a", "kind": "word_paragraphs", "paragraph_start": 2, "paragraph_end": 3})
    assert result.status_code == 200, result.text
    locator = result.json()
    assert "paragraph 2" in locator["quote"] and "paragraph 1" not in locator["quote"]
    assert locator["location"]["paragraph_index_base"] == 1
    assert "page_number" not in locator["location"]
    assert db.scalar(select(func.count()).select_from(Paper)) == 0


def test_excel_and_csv_real_cell_ranges_and_formula_not_recalculated(client):
    excel = upload(client, excel_bytes(), "results.xlsx")
    doc = client.get(url(excel, "/document")).json()
    assert [s["name"] for s in doc["sheets"]] == ["实验数据", "Notes"]
    assert doc["blocks"][1]["cells"][2]["value"] == "=SUM(B2:B2)"
    locator = client.post(url(excel, "/locators").split("?")[0], json={
        "conversation_id": "docs-a", "kind": "sheet_cells", "sheet": "实验数据", "cell_range": "B2:C2"})
    assert locator.status_code == 200, locator.text
    assert locator.json()["quote"] == "28.4\t=SUM(B2:B2)"
    assert locator.json()["location"]["formula_values_recalculated"] is False
    csv = upload(client, b'name,value\n"quoted\nrecord",2\n', "data.csv", request="source-doc-csv-1")
    result = client.post(url(csv, "/locators").split("?")[0], json={
        "conversation_id": "docs-a", "kind": "sheet_cells", "sheet": "CSV", "cell_range": "A2:B2"})
    assert result.status_code == 200 and result.json()["quote"] == "quoted\nrecord\t2"


@pytest.mark.parametrize("change", [
    {"paragraph_start": 0, "paragraph_end": 1}, {"paragraph_start": 1, "paragraph_end": 999},
    {"paragraph_start": 1, "paragraph_end": 1, "page_index": 0},
    {"paragraph_start": 1, "paragraph_end": 1, "quote": "forged evidence"},
])
def test_word_locator_rejects_forged_or_out_of_range_values(client, change):
    item = upload(client, word_bytes(), "study.docx")
    response = client.post(url(item, "/locators").split("?")[0], json={
        "conversation_id": "docs-a", "kind": "word_paragraphs", **change})
    assert response.status_code == 422


@pytest.mark.parametrize("sheet,cell_range", [("Other", "A1"), ("实验数据", "A0"),
    ("实验数据", "Z999"), ("实验数据", "B2:A1"), ("实验数据", "=COMMAND()")])
def test_spreadsheet_locator_cannot_invent_cells(client, sheet, cell_range):
    item = upload(client, excel_bytes(), "study.xlsx")
    response = client.post(url(item, "/locators").split("?")[0], json={
        "conversation_id": "docs-a", "kind": "sheet_cells", "sheet": sheet, "cell_range": cell_range})
    assert response.status_code == 422


def test_document_paging_jumps_to_real_locator_and_validates_sheet_scope(client):
    item = upload(client, word_bytes(240), "long.docx")
    page = client.get(url(item, "/document") + "&limit=20").json()
    assert len(page["blocks"]) == 20 and page["next_offset"] == 20
    locator = client.post(url(item, "/locators").split("?")[0], json={
        "conversation_id": "docs-a", "kind": "word_paragraphs", "paragraph_start": 230, "paragraph_end": 231}).json()
    jumped = client.get(url(item, "/document") + "&locator_id=" + locator["locator_id"]).json()
    assert jumped["offset"] == 229 and jumped["blocks"][0]["paragraph_index"] == 230
    other = upload(client, word_bytes(), "other.docx", request="other-doc-upload")
    assert client.get(url(other, "/document") + "&locator_id=" + locator["locator_id"]).status_code == 404
    assert client.get(url(item, "/document", "docs-b")).status_code == 404
    excel = upload(client, excel_bytes(), "sheet.xlsx", request="sheet-upload-2")
    filtered = client.get(url(excel, "/document") + "&sheet=Notes").json()
    assert filtered["total_blocks"] == 1 and filtered["blocks"][0]["sheet"] == "Notes"
    assert client.get(url(excel, "/document") + "&sheet=Missing").status_code == 422


def test_malformed_office_preserves_original_without_false_text_or_evidence(client):
    item = upload(client, b"not an office package", "bad.docx")
    assert item["parse_status"] == "original_only" and item["parse_error"]
    assert "document" not in item["available_views"]
    assert client.get(url(item, "/file")).content == b"not an office package"
    assert client.get(url(item, "/document")).status_code == 404


def test_office_zip_entity_and_size_limits_fail_without_false_parse(client, monkeypatch):
    from app import source_parsing
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE a [<!ENTITY b "bad">]><doc/>')
    item = upload(client, output.getvalue(), "unsafe.docx")
    assert item.get("parse_error") and "document" not in item["available_views"]
    monkeypatch.setattr(source_parsing, "MAX_ARCHIVE_BYTES", 10)
    item = upload(client, word_bytes(), "oversized.docx", request="oversized-office-1")
    assert item.get("parse_error") and "document" not in item["available_views"]


def test_extraction_tamper_and_wrong_scope_fail_closed(client, db):
    item = upload(client, word_bytes(), "study.docx")
    document = db.get(SourceExtraction, item["revision_id"]).document
    changed = deepcopy(document)
    changed["blocks"][0]["text"] = "forged"
    db.execute(text("UPDATE source_extractions SET document=:doc WHERE revision_id=:rid"),
        {"doc": json.dumps(changed), "rid": item["revision_id"]})
    db.commit()
    assert client.get(url(item, "/document")).status_code == 409
    with pytest.raises(SourceError):
        capture_bindings(db, "docs-a", [item["revision_id"]])
    with pytest.raises(SourceError):
        capture_bindings(db, "docs-b", [item["revision_id"]])


def test_ensure_conversation_keeps_client_id_and_never_revives_deleted(client, db):
    cid = "c_draft_source_client_generated"
    result = client.post("/api/sources/ensure-conversation", json={"conversation_id": cid})
    assert result.status_code == 200 and result.json()["conversation_id"] == cid
    item = upload(client, cid=cid)
    assert item["scope_id"] == cid
    db.add(DeletedConversation(id="c_source_tombstone"))
    db.commit()
    assert client.post("/api/sources/ensure-conversation", json={"conversation_id": "c_source_tombstone"}).status_code == 410


def test_frozen_selection_retains_old_version_and_denies_unselected_sources(client, db):
    first = upload(client)
    binding = freeze_sources(db, "docs-a", [first["revision_id"]], {"mode": "mock", "conversation_id": "docs-a"})
    second = upload(client, b"A changed source.", request="source-doc-update-2",
        source_id=first["source_id"], expected_version=1)
    assert validate_frozen_sources(db, "docs-a", binding)[0]["revision_id"] == first["revision_id"]
    with bind_runtime_config(binding):
        read = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": first["revision_id"], "line_end": 1}))
        denied = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": second["revision_id"]}))
    assert read["evidence"][0]["quote"] == "Transformer uses attention.\n"
    assert denied["code"] == "source_scope_or_content_invalid"
    validate_source_evidence(db, "docs-a", read["evidence"])
    read["evidence"][0]["quote"] = "forged"
    with pytest.raises(SourceError):
        validate_source_evidence(db, "docs-a", read["evidence"])


def test_additive_directory_hints_do_not_break_old_frozen_bindings(client, db):
    item = upload(client)
    binding = freeze_sources(db, "docs-a", [item["revision_id"]], {"mode": "mock"})
    old = deepcopy(binding)
    for key in ("total_lines", "page_count", "paragraph_count", "sheets", "extraction_scope", "omitted_content"):
        old["source_bindings"][0].pop(key)
    before = deepcopy(old)
    actual = validate_frozen_sources(db, "docs-a", old)
    assert actual[0]["total_lines"] == 2 and old == before
    old["source_bindings"][0]["raw_sha256"] = "0" * 64
    with pytest.raises(SourceError):
        validate_frozen_sources(db, "docs-a", old)


def test_directory_and_read_results_report_actual_ranges_and_do_not_guess_end(client, db):
    item = upload(client, b"one\ntwo\nthree\nfour")
    binding = freeze_sources(db, "docs-a", [item["revision_id"]], {"conversation_id": "docs-a"})
    with bind_runtime_config(binding):
        directory = json.loads(execute_source_tool(db, "list_attached_sources", {}))
        read = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": item["revision_id"], "line_end": 99}))
        bad = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": item["revision_id"], "line_start": 20}))
    assert directory["sources"][0]["total_lines"] == 4 and directory["text_read"] is False
    assert read["read_range"] == {"kind": "text_lines", "line_start": 1, "line_end": 4, "total_lines": 4, "next_line": None}
    assert read["range_adjusted_to_actual_end"] is True
    assert bad["available_range"]["total_lines"] == 4 and bad["text_read"] is False
    assert read["model_understanding_verified"] is False


def test_draft_and_run_admission_reject_cross_conversation_sources(client, db):
    item = upload(client)
    response = client.put("/api/desktop-state/conversations/docs-b", json={"revision": 0,
        "value": {"source_revision_ids": [item["revision_id"]]}})
    assert response.status_code == 409
    response = client.post("/api/foreground-runs", json={"id": "chat_source_admission_cross", "conversation_id": "docs-b",
        "question": "Read the attached source", "source_revision_ids": [item["revision_id"]]})
    assert response.status_code == 409
    assert db.get(ForegroundRun, "chat_source_admission_cross") is None
    response = client.post("/api/tasks/start", json={"conversation_id": "docs-b", "question": "Read",
        "source_revision_ids": [item["revision_id"]]})
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(TaskRecord).where(TaskRecord.type == "agent")) == 0


def test_foreground_source_selection_and_chat_request_must_match_before_admission(client, db):
    item = upload(client)
    payload = {"id": "chat_source_admission_valid", "conversation_id": "docs-a", "question": "Read the source",
               "source_revision_ids": [item["revision_id"]]}
    result = client.post("/api/foreground-runs", json=payload)
    assert result.status_code == 200, result.text
    listed = client.get("/api/foreground-runs?conversation_id=docs-a").json()
    assert listed["runs"][0]["source_revision_ids"] == [item["revision_id"]]
    response = client.post("/api/chat/stream", json={"run_id": payload["id"], "conversation_id": "docs-a", "question": payload["question"]})
    assert response.status_code == 409
    db.expire_all()
    assert db.get(ForegroundRun, payload["id"]).started_at is None


def test_steering_and_next_turn_freeze_attachments_and_deduplicate_ids(client, db):
    item = upload(client)
    binding = freeze_sources(db, "docs-a", [item["revision_id"]], {"mode": "mock"})
    row = ForegroundRun(id="chat_source_input_binding", conversation_id="docs-a", question="Study", runtime_config=binding,
                        images=[], skills=[], status="running")
    db.add(row)
    db.commit()
    result = run_inputs.submit_input(db, row.id, input_id="source-steer-1", conversation_id="docs-a", kind="steer",
        content="Read this attachment", expected_revision=0, workspace_revision=0, source_revision_ids=[item["revision_id"]])
    assert result["input"]["source_revision_ids"] == [item["revision_id"]]
    db.commit()
    with pytest.raises(run_inputs.RunInputError):
        run_inputs.submit_input(db, row.id, input_id="source-steer-1", conversation_id="docs-a", kind="steer",
            content="Read this attachment", expected_revision=1, workspace_revision=0, source_revision_ids=[])
    second = run_inputs.submit_input(db, row.id, input_id="source-next-turn-2", conversation_id="docs-a", kind="next_turn",
        content="Summarize next", expected_revision=1, workspace_revision=0, source_revision_ids=[item["revision_id"]])
    assert second["input"]["source_revision_ids"] == [item["revision_id"]]
    db.commit()
    applied = run_inputs.consume_steering_inputs(db, row.id, "docs-a")
    assert applied[0]["source_binding"]["source_bindings"][0]["raw_sha256"] == item["raw_sha256"]


def test_real_model_tool_loop_receives_fixed_source_and_saves_clickable_evidence(client, db, monkeypatch):
    item = upload(client)
    binding = freeze_sources(db, "docs-a", [item["revision_id"]], {"mode": "mock", "model_id": "Synthetic", "approval_mode": "full_access"})
    seen = []

    class Model:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            if not any(m["role"] == "tool" for m in messages):
                yield "final", LLMResponse("", [{"id": "source-read-call", "name": "read_attached_source", "arguments": {
                    "source_revision_id": item["revision_id"], "line_start": 1, "line_end": 1}}])
            else:
                yield "final", LLMResponse("Transformer uses attention.[^e1]")

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *a: "")
    monkeypatch.setattr(loop, "_load_history", lambda *a, **k: ([], {"total": 0}))
    events = list(loop.run_agent(db, "What does the attached note say?", conversation_id="docs-a", runtime_config=binding))
    answer = next(event for kind, event in events if kind == "answer")
    assert answer["refusal"] == 0 and answer["evidence"][0]["locator_id"]
    assert answer["source_revision_ids"] == [item["revision_id"]]
    assert "[^eN]" in seen[0][0]["content"]
    record = db.query(QARecord).one()
    assert record.runtime_config["source_bindings"][0]["revision_id"] == item["revision_id"]
    validate_source_evidence(db, "docs-a", record.evidence)


def test_local_project_capture_reads_only_authorized_root_and_retains_old_bytes(client, db, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    file = root / "article.txt"
    file.write_text("old local bytes", encoding="utf-8")
    project = workspace.register(db, str(root))
    conv = db.get(Conversation, "docs-a")
    conv.project_id, conv.workspace_revision = project.id, 1
    db.commit()
    payload = {"conversation_id": "docs-a", "request_id": "project-capture-1", "project_id": project.id,
               "workspace_revision": 1, "path": "article.txt"}
    result = client.post("/api/sources/capture-project", json=payload)
    assert result.status_code == 200, result.text
    item = result.json()
    assert item["kind"] == "local"
    file.write_text("new local bytes", encoding="utf-8")
    assert client.get(url(item, "/file")).content == b"old local bytes"
    assert client.post("/api/sources/capture-project", json=payload).status_code == 409
    assert client.post("/api/sources/capture-project", json={**payload, "request_id": "project-capture-2", "path": "../outside.txt"}).status_code in (400, 422)
    assert client.post("/api/sources/capture-project", json={**payload, "request_id": "project-capture-3", "workspace_revision": 0}).status_code == 409


def test_project_capture_rechecks_physical_root_after_parsing(client, db, tmp_path, monkeypatch):
    from app import source_capture
    root = tmp_path / "project-race"
    root.mkdir()
    (root / "article.txt").write_text("original root data", encoding="utf-8")
    project = workspace.register(db, str(root))
    conv = db.get(Conversation, "docs-a")
    conv.project_id, conv.workspace_revision = project.id, 1
    db.commit()
    original = source_capture.extract

    def replace_root(raw, media):
        result = original(raw, media)
        root.rename(tmp_path / "prior-project")
        root.mkdir()
        (root / "article.txt").write_text("replacement root data", encoding="utf-8")
        return result

    monkeypatch.setattr(source_capture, "extract", replace_root)
    response = client.post("/api/sources/capture-project", json={"conversation_id": "docs-a",
        "request_id": "project-root-race-1", "project_id": project.id, "workspace_revision": 1, "path": "article.txt"})
    assert response.status_code == 409, response.text
    assert db.scalar(select(func.count()).select_from(Source)) == 0


def test_explicit_multi_library_import_is_idempotent_and_nonpdf_reader_uses_source(client, db):
    first = upload(client, word_bytes(), "study.docx")
    second = upload(client, excel_bytes(), "results.xlsx", request="import-source-excel")
    assert db.scalar(select(func.count()).select_from(Paper)) == 0
    payload = {"conversation_id": "docs-a", "request_id": "source-library-import-1", "revision_ids": [first["revision_id"], second["revision_id"]]}
    result = client.post("/api/sources/library-import", json=payload)
    assert result.status_code == 200, result.text
    items = result.json()["items"]
    assert [i["status"] for i in items] == ["imported", "imported"], items
    assert db.scalar(select(func.count()).select_from(Paper)) == 2
    assert all(i["status"] == "existing" for i in client.post("/api/sources/library-import", json=payload).json()["items"])
    papers = client.get("/api/papers").json()
    assert len(papers) == 2 and {p["document_format"] for p in papers} == {"word", "excel"}
    assert all(p["page_count"] == 0 and p["source_revision_id"] for p in papers)
    opened = client.post(f'/api/sources/library/{items[0]["paper_id"]}/open', json={"conversation_id": "c_new_library_reader"})
    assert opened.status_code == 200, opened.text
    alias = opened.json()
    assert alias["conversation_id"] == "c_new_library_reader" and alias["revision_id"] != first["revision_id"]
    assert client.get(url(alias, "/document", "c_new_library_reader")).json()["format"] == "word"
    assert db.scalar(select(func.count()).select_from(Paper)) == 2


def test_library_item_failure_does_not_import_unselected_or_lose_other_selection(client, db):
    valid = upload(client)
    invalid = upload(client, b"broken office", "broken.docx", request="broken-office-source")
    result = client.post("/api/sources/library-import", json={"conversation_id": "docs-a", "request_id": "partial-library-import",
        "revision_ids": [valid["revision_id"], invalid["revision_id"]]}).json()["items"]
    assert result[0]["status"] == "imported" and result[1]["status"] == "failed"
    assert result[1]["error"]
    assert db.scalar(select(func.count()).select_from(Paper)) == 1


def test_source_library_retrieval_copies_verified_locator_into_current_conversation(client, db):
    from app.source_library import augment_library_evidence
    item = upload(client, word_bytes(), "study.docx")
    result = client.post("/api/sources/library-import", json={"conversation_id": "docs-a", "request_id": "library-retrieval-1",
        "revision_ids": [item["revision_id"]]}).json()["items"][0]
    assert result["status"] == "imported", result
    receipt = db.get(SourceLibraryImport, item["revision_id"])
    chunk_id = next(iter(receipt.chunk_locators))
    evidence = augment_library_evidence(db, [{"paper_id": result["paper_id"], "chunk_id": chunk_id, "quote": "excerpt"}], "docs-b")
    assert evidence[0]["revision_id"] != item["revision_id"] and evidence[0]["page"] == 0 and evidence[0]["bbox"] is None
    validate_source_evidence(db, "docs-b", evidence)


def test_pdf_explicit_import_reuses_existing_pipeline_and_same_bytes_do_not_duplicate(client, db, sample_pdf):
    first = upload(client, sample_pdf.read_bytes(), "article.pdf")
    result = client.post("/api/sources/library-import", json={"conversation_id": "docs-a", "request_id": "pdf-library-import-1",
        "revision_ids": [first["revision_id"]]}).json()["items"][0]
    assert result["status"] == "imported" and result["index_status"] == "parsed", result
    paper = db.get(Paper, result["paper_id"])
    assert paper.page_count == 2 and paper.pdf_path.endswith(".pdf")
    second = upload(client, sample_pdf.read_bytes(), "article-again.pdf", request="duplicate-pdf-source")
    replay = client.post("/api/sources/library-import", json={"conversation_id": "docs-a", "request_id": "pdf-library-import-2",
        "revision_ids": [second["revision_id"]]}).json()["items"][0]
    assert replay["status"] == "existing" and replay["paper_id"] == paper.id
    assert db.scalar(select(func.count()).select_from(Paper)) == 1


def test_document_embedding_does_not_hold_database_writer(client, db, monkeypatch):
    from app import source_library
    from app.db import SessionLocal
    item = upload(client)
    original = source_library._prepare_vectors
    observed = []

    def encode(locators):
        with SessionLocal() as independent:
            independent.execute(text("BEGIN IMMEDIATE"))
            observed.append(independent.scalar(select(func.count()).select_from(Paper)))
            independent.rollback()
        return original(locators)

    monkeypatch.setattr(source_library, "_prepare_vectors", encode)
    result = client.post("/api/sources/library-import", json={"conversation_id": "docs-a", "request_id": "writer-free-import",
        "revision_ids": [item["revision_id"]]}).json()["items"][0]
    assert result["status"] == "imported" and observed == [0]


def test_fork_clones_fixed_revision_and_locator_without_cross_scope_links(client, db):
    item = upload(client)
    binding = freeze_sources(db, "docs-a", [item["revision_id"]], {"mode": "mock", "model_id": "Synthetic"})
    with bind_runtime_config({**binding, "conversation_id": "docs-a"}):
        evidence = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": item["revision_id"], "line_end": 1}))["evidence"]
    evidence[0]["id"] = "e1"
    record = QARecord(conversation_id="docs-a", question="Source question", answer="Transformer uses attention.[^e1]",
                      runtime_config=binding, evidence=evidence, claims=[], trace=[])
    db.add(record)
    db.commit()
    result = client.post("/api/conversations/docs-a/fork", json={"after_message_id": record.id})
    assert result.status_code == 200, result.text
    cid = result.json()["id"]
    forked = db.query(QARecord).filter(QARecord.conversation_id == cid).one()
    assert forked.evidence[0]["revision_id"] != item["revision_id"]
    validate_source_evidence(db, cid, forked.evidence)
    validate_frozen_sources(db, cid, forked.runtime_config)
