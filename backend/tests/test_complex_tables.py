"""Real source API, immutable evidence, table semantics and actual recomputation."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from openpyxl import Workbook

from app.main import app
from app.models import Conversation
from app.source_models import Source, SourceRevision
from app.source_parsing import extract, MEDIA
from app.source_store import SourceScope
from app.source_documents import read_document
from app.source_runtime import freeze_sources, validate_frozen_sources
from app.source_tools import execute_source_tool
from app.runtime_context import bind_runtime_config
from app.table_formula import execute
from app.table_models import TableComputation
from tests.complex_table_fixture import workbook_bytes

client = TestClient(app)


@pytest.fixture(autouse=True)
def conversations(db):
    db.add_all([Conversation(id="table-a"), Conversation(id="table-b")]); db.commit()


def upload(raw, name="study.xlsx", request="table-upload-initial", **options):
    result = client.post("/api/sources/upload", files={"file": (name, raw)},
        data={"conversation_id": "table-a", "request_id": request, **options})
    assert result.status_code == 200, result.text
    return result.json()


def route(item, action="", cid="table-a"):
    return f'/api/sources/revisions/{item["revision_id"]}{action}?conversation_id={cid}'


def document(item):
    result = client.get(route(item, "/document")); assert result.status_code == 200, result.text
    return result.json()


def run(item, doc, cell_range="E2:G3", request="table-compute-request", **extra):
    return client.post(route(item, "/table-computations").split("?")[0], json={
        "conversation_id": "table-a", "request_id": request, "expected_fingerprint": doc["fingerprint"],
        "sheet": "实验 数据", "cell_range": cell_range, **extra})


@pytest.mark.parametrize("encoding,text", [("utf-8-sig", "名称;值\n中文;1\n"),
    ("utf-16", "名称;值\n中文;1\n"), ("gb18030", "名称;值\n中文;1\n"), ("cp1252", "nom;valeur\ncafé;1\n")])
def test_csv_explicit_encoding_and_delimiter_preserve_original(encoding, text):
    raw = text.encode(encoding)
    item = upload(raw, "study.csv", csv_encoding=encoding, csv_delimiter="semicolon")
    doc = document(item)
    assert doc["table_metadata"]["encoding"] == encoding
    assert doc["table_metadata"]["delimiter"] == "semicolon"
    assert doc["table_metadata"]["values_coerced"] is False
    assert doc["blocks"][1]["cells"][0]["value"] in ("中文", "café")
    assert client.get(route(item, "/file")).content == raw


@pytest.mark.parametrize("delimiter,separator", [("comma", ","), ("semicolon", ";"), ("tab", "\t"), ("pipe", "|")])
def test_csv_delimiters_multiline_records_and_duplicate_header_identity(delimiter, separator):
    text = separator.join(["same", "same", "ratio (%)"]) + "\n" + separator.join(['"two\nlines"', "", "12.5%"])+"\n"
    item = upload(text.encode(), "study.csv", csv_delimiter=delimiter)
    doc = document(item)
    assert doc["table_metadata"]["duplicate_header_labels"] == ["same"]
    block = doc["blocks"][1]
    assert (block["physical_line_start"], block["physical_line_end"]) == (2, 3)
    assert block["cells"][1]["value_kind"] == "blank" and block["cells"][1]["missing"] is True
    assert block["cells"][2]["value_kind"] == "percentage_text" and block["cells"][2]["conversion_applied"] is False
    result = client.post(route(item, "/locators").split("?")[0], json={"conversation_id": "table-a", "kind": "sheet_cells", "sheet": "CSV", "cell_range": "A2:C2"})
    assert result.json()["quote"] == "two\nlines\t\t12.5%"
    assert result.json()["location"]["table_semantics"]["delimiter"] == delimiter


def test_csv_preview_and_explicit_reinterpretation_work_for_retained_local_or_uploaded_original(db):
    raw = "标题;值\n中文;NA\n".encode("gb18030")
    item = upload(raw, "study.csv")
    assert item["parse_status"] == "original_only" and "编码" in item["parse_error"]
    preview = client.post(route(item, "/csv-preview").split("?")[0], json={"conversation_id": "table-a", "encoding": "gb18030", "delimiter": "semicolon"})
    assert preview.status_code == 200
    assert next(c for c in preview.json()["candidates"] if c["encoding"] == "gb18030")["rows"][1] == ["中文", "NA"]
    assert db.scalar(select(func.count()).select_from(SourceRevision)) == 1
    args = {"conversation_id": "table-a", "encoding": "gb18030", "delimiter": "semicolon", "header_row": 1,
            "request_id": "table-reinterpret-stable", "expected_version": 1}
    result = client.post(route(item, "/csv-interpret").split("?")[0], json=args)
    assert result.status_code == 200, result.text
    newer = result.json(); assert newer["source_id"] == item["source_id"] and newer["version"] == 2
    assert client.get(route(item, "/file")).content == raw == client.get(route(newer, "/file")).content
    assert document(newer)["blocks"][1]["cells"][1]["missing_marker_candidate"] is True
    assert document(newer)["blocks"][1]["cells"][1]["missing"] is False
    assert client.post(route(item, "/csv-interpret").split("?")[0], json=args).json()["revision_id"] == newer["revision_id"]
    assert client.post(route(item, "/csv-interpret").split("?")[0], json={**args, "delimiter": "comma"}).status_code == 409
    assert client.post(route(item, "/csv-interpret").split("?")[0], json={**args, "request_id": "new-stale-request", "expected_version": 2}).status_code == 409


def test_csv_invalid_choices_and_malformed_content_never_invent_success(db):
    assert client.post("/api/sources/upload", files={"file": ("bad.csv", b"A,B\n1,2")}, data={"conversation_id": "table-a", "request_id": "invalid-encoding", "csv_encoding": "guess-anything"}).status_code == 422
    assert db.scalar(select(func.count()).select_from(Source)) == 0
    item = upload(b'A,B\n"unclosed,2', "study.csv")
    assert item["parse_status"] == "original_only"
    assert client.get(route(item, "/file")).content == b'A,B\n"unclosed,2'
    assert client.post(route(item, "/csv-preview").split("?")[0], json={"conversation_id": "table-b"}).status_code == 404


def test_csv_option_changes_are_part_of_upload_idempotency():
    raw = b"A;B\n1;2"
    first = upload(raw, "data.csv", csv_delimiter="semicolon")
    response = client.post("/api/sources/upload", files={"file": ("data.csv", raw)},
        data={"conversation_id": "table-a", "request_id": "table-upload-initial", "csv_delimiter": "comma"})
    assert response.status_code == 409
    assert document(first)["sheets"][0]["column_count"] == 2


def test_excel_golden_semantics_preserve_formulas_caches_merges_dates_percent_and_hidden_data():
    item = upload(workbook_bytes())
    doc = document(item)
    cells = {c["cell"]: c for b in doc["blocks"] if b["sheet"] == "实验 数据" for c in b["cells"]}
    assert cells["E2"]["formula"] == "=SUM(B2,'对照'!A2)" and cells["E2"]["cached_value"] == 999
    assert cells["E2"]["cache_state"] == "present_unverified" and cells["F2"]["cache_state"] == "missing"
    assert cells["C2"]["raw_value"] == 0.25 and cells["C2"]["percent_format"] is True
    assert cells["D2"]["value"] == "2020-02-29T00:00:00" and cells["D2"]["stored_xml_value"].isdigit()
    assert cells["I3"]["merged_anchor"] == "H2" and cells["I3"]["raw_value"] is None
    assert doc["sheets"][0]["merged_ranges"] == ["H2:I3"] and doc["sheets"][0]["hidden_rows"] == [3]
    assert doc["sheets"][1]["state"] == "hidden"
    assert doc["table_metadata"]["formula_recalculation_performed"] is False
    assert doc["sheets"][0]["headers"]["columns"][1]["unit_hint"] == "mg"


def test_real_local_recomputation_never_uses_stale_cache_and_preserves_unresolved(db):
    item = upload(workbook_bytes()); doc = document(item)
    response = run(item, doc); assert response.status_code == 200, response.text
    saved = response.json(); result = saved["result"]
    assert saved["execution_performed"] is True and result["status"] == "partial"
    values = {c["cell"]: c for c in result["outputs"]}
    assert values["E2"]["value"] == 5 and values["E2"]["cache_comparison"] == "mismatch"
    assert values["G3"]["value"] == 10 and values["G3"]["cache_comparison"] == "matches"
    assert values["F2"]["value"] == 4 and values["F3"]["reason"] == "blank_dependency_not_imputed"
    assert values["G2"]["reason"] == "unsupported_function:INDIRECT"
    assert result["formula_cache_used_for_computation"] is False and result["unit_conversions_applied"] is False
    assert document(item)["blocks"] == doc["blocks"]
    assert run(item, doc).json()["id"] == saved["id"]
    assert db.scalar(select(func.count()).select_from(TableComputation)) == 1
    assert run(item, doc, "E2:E2").status_code == 409


@pytest.mark.parametrize("change", [{"sheet": "Wrong"}, {"cell_range": "A1:ZZ999"},
    {"cell_range": "B2:A1"}, {"absolute_tolerance": True}, {"relative_tolerance": -1},
    {"expected_fingerprint": "0"*64}, {"conversation_id": "table-b"}, {"extra_code": "print('bad')"}])
def test_recomputation_scope_bounds_and_strict_parameters_fail_before_record(db, change):
    item = upload(workbook_bytes()); doc = document(item)
    result = run(item, doc, **change)
    assert result.status_code in (404,409,422), result.text
    assert db.scalar(select(func.count()).select_from(TableComputation)) == 0


def test_model_visible_read_receives_units_formulas_cache_and_fixed_version(db):
    item = upload(workbook_bytes()); doc = document(item)
    binding = freeze_sources(db, "table-a", [item["revision_id"]], {"conversation_id": "table-a"})
    before = deepcopy(binding)
    with bind_runtime_config(binding):
        value = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": item["revision_id"], "sheet": "实验 数据", "cell_range": "B2:F2"}))
    assert value["table_context"]["cells"][0]["raw_value"] == 2
    assert value["table_context"]["cells"][-2]["cached_value"] == 999
    assert value["evidence"][0]["quote"].endswith("=SUM(B2,'对照'!A2)\t=B2*2")
    assert value["read_range"]["formula_values_recalculated"] is False
    assert value["table_context"]["selected_sheet"]["selected_column_headers"][0]["unit_hint"] == "mg"
    assert validate_frozen_sources(db, "table-a", binding) and binding == before


def test_actual_exported_program_reproduces_record_without_excel_or_external_services(tmp_path):
    raw = workbook_bytes()
    item = upload(raw); doc = document(item); saved = run(item, doc).json()
    response = client.get(route(item, f'/table-computations/{saved["id"]}/bundle'))
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        for name in archive.namelist():
            target = (tmp_path / name).resolve()
            assert target.is_relative_to(tmp_path.resolve())
            target.write_bytes(archive.read(name))
    result = subprocess.run([sys.executable,"-X","utf8",str(tmp_path/"reproduce.py")], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    outputs = json.loads((tmp_path/"recomputed.json").read_text(encoding="utf-8"))["outputs"]
    assert outputs == saved["result"]["outputs"]
    assert (tmp_path/"original.xlsx").read_bytes() == raw
    manifest = json.loads((tmp_path/"manifest.json").read_text())
    assert manifest["raw_sha256"] == item["raw_sha256"] and manifest["recorded_execution"] is True
    (tmp_path/"table_reproduce.py").write_text("from pathlib import Path\nPath(__file__).with_name('unexpected-execution.txt').write_text('bad')\n", encoding="utf-8")
    tampered = subprocess.run([sys.executable,"-X","utf8",str(tmp_path/"reproduce.py")],capture_output=True,text=True,timeout=15)
    assert tampered.returncode != 0 and 'Bundle file changed' in tampered.stderr
    assert not (tmp_path/"unexpected-execution.txt").exists()


def test_actual_computation_tamper_is_rejected_by_history_and_export(db):
    item = upload(workbook_bytes()); saved = run(item, document(item)).json()
    row = db.get(TableComputation, saved["id"]); row.result = {**row.result, "computed_count": 99}; db.commit()
    assert client.get(route(item, "/table-computations")).status_code == 409
    assert client.get(route(item, f'/table-computations/{saved["id"]}/bundle')).status_code == 409


def test_1904_date_epoch_and_duplicate_cells_are_not_silently_rewritten():
    from openpyxl.utils.datetime import MAC_EPOCH
    from xml.etree import ElementTree as ET
    book = Workbook(); book.epoch = MAC_EPOCH; book.active.append([datetime(2020,1,1)])
    raw = BytesIO(); book.save(raw); book.close()
    doc = extract(raw.getvalue(),MEDIA["xlsx"]).document
    assert doc["table_metadata"]["date_epoch"].startswith("1904-01-01")
    assert doc["blocks"][0]["cells"][0]["value"] == "2020-01-01T00:00:00"
    patched = BytesIO()
    with zipfile.ZipFile(BytesIO(raw.getvalue())) as archive, zipfile.ZipFile(patched,"w") as target:
        for item in archive.infolist():
            value = archive.read(item)
            if item.filename == 'xl/worksheets/sheet1.xml':
                root = ET.fromstring(value); ns = {'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                row = root.find('m:sheetData/m:row',ns); row.append(deepcopy(row[0])); value = ET.tostring(root)
            target.writestr(item,value)
    from app.source_store import SourceError
    with pytest.raises(SourceError,match="重复"):
        extract(patched.getvalue(),MEDIA["xlsx"])


def test_new_computation_records_survive_credential_free_backup_and_are_removed_with_owner(db,tmp_path):
    from app.config import DATA_DIR
    from app import data_backup
    item = upload(workbook_bytes()); saved = run(item, document(item)).json()
    exported = data_backup.create_backup(DATA_DIR)
    target = tmp_path/"restore"
    preview = data_backup.prepare_restore(target,exported["path"]); data_backup.schedule_restore(target,preview["id"])
    assert data_backup.apply_pending_restore(target)["status"] == "restored"
    with closing(sqlite3.connect(target/"scholarflow.db")) as restored:
        assert restored.execute("SELECT fingerprint FROM table_computations WHERE id=?",(saved["id"],)).fetchone() == (saved["fingerprint"],)
    assert client.get('/api/conversations/table-a/deletion-preview').json()["records"]["table_computations"] == 1
    assert client.delete('/api/conversations/table-a').status_code == 200
    db.expire_all(); assert db.scalar(select(func.count()).select_from(TableComputation)) == 0


@pytest.mark.parametrize("formula,expected", [("=-2^2",4),("=2^3^2",64),("=25%*4",1),
    ("=SUM(A1:B1)",3),("=AVERAGE(A1:B1)",1.5),("=COUNT(A1:B1)",2)])
def test_formula_arithmetic_has_explicit_golden_semantics(formula,expected):
    book=Workbook(); sheet=book.active; sheet.append([1,2,formula]); output=BytesIO(); book.save(output); book.close()
    doc=extract(output.getvalue(),MEDIA["xlsx"]).document
    value=execute(doc,{"sheet":"Sheet","cell_range":"C1","absolute_tolerance":1e-9,"relative_tolerance":1e-9})
    assert value["outputs"][0]["value"] == expected


@pytest.mark.parametrize("formula,reason", [("=D1","circular_reference"),("=1/0","division_by_zero"),
    ("=0^0", "zero_to_zero_undefined"),
    ("=RAND()","unsupported_function:RAND"),("='[remote.xlsx]Data'!A1","external_reference_unsupported"),
    ("=__import__('os')","unsupported_function:__IMPORT__"),("="+"("*70+"1"+")"*70,"formula_nesting_limit")])
def test_unsupported_or_unsafe_formula_is_retained_as_unresolved(formula,reason):
    book=Workbook(); sheet=book.active; sheet.append([1,2,3,formula]); output=BytesIO(); book.save(output); book.close()
    doc=extract(output.getvalue(),MEDIA["xlsx"]).document
    result=execute(doc,{"sheet":"Sheet","cell_range":"D1","absolute_tolerance":1e-9,"relative_tolerance":1e-9})
    assert result["status"] == "partial" and result["outputs"][0]["reason"] == reason


@pytest.mark.parametrize("formula", ["=A2", "=SUM(A1:A2)", "=AVERAGE(A1:A2)",
    "=MIN(A1:A2)", "=MAX(A1:A2)", "=COUNT(A1:A2)", "=SUM(A1,SUM(A1:A2))"])
def test_error_cells_are_never_skipped_or_verified_even_with_a_matching_cache(formula):
    book = Workbook(); sheet = book.active
    sheet["A1"] = 2; sheet["A2"] = "#DIV/0!"; sheet["B1"] = formula
    sheet["C1"] = "=B1+1"
    output = BytesIO(); book.save(output); book.close()
    doc = extract(output.getvalue(), MEDIA["xlsx"]).document
    for block in doc["blocks"]:
        for cell in block["cells"]:
            if cell["cell"] == "B1":
                cell.update(cached_value=2, cache_state="present_unverified", cache_verified=False)
    result = execute(doc, {"sheet": "Sheet", "cell_range": "B1:C1",
        "absolute_tolerance": 1e-9, "relative_tolerance": 1e-9})
    assert result["unresolved_count"] == 2 and result["computed_count"] == 0
    assert all(row["reason"] == "error_cell_dependency" for row in result["outputs"])
    assert all(row["cache_verified"] is False and "cache_comparison" not in row for row in result["outputs"])


def test_special_formula_original_expression_range_and_members_remain_unresolved():
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula
    book = Workbook(); sheet = book.active
    sheet["A1"] = 2; sheet["A2"] = 3
    sheet["B1"] = ArrayFormula("B1:B2", "=SUM(A1:A2)"); sheet["B2"] = 999
    sheet["C1"] = DataTableFormula("C1:C2", r1="A1"); sheet["C2"] = 777
    sheet["D1"] = "=B1+1"; sheet["D2"] = "=B2+1"; sheet["E1"] = "=C2+1"
    output = BytesIO(); book.save(output); book.close(); raw = output.getvalue()
    doc = extract(raw, MEDIA["xlsx"]).document
    assert doc == extract(raw, MEDIA["xlsx"]).document
    cells = {cell["cell"]: cell for block in doc["blocks"] for cell in block["cells"]}
    assert cells["B1"]["formula"] == "=SUM(A1:A2)" and cells["B1"]["formula_representation"] == "array"
    assert cells["B1"]["formula_attributes"]["ref"] == "B1:B2"
    assert cells["C1"]["formula_representation"] == "dataTable"
    assert cells["C1"]["formula_attributes"]["ref"] == "C1:C2"
    assert "0x" not in cells["C1"]["value"] and "ref=C1:C2" in cells["C1"]["value"]
    assert cells["B2"]["raw_value"] == 999 and cells["B2"]["special_formula_member"] is True
    assert cells["C2"]["special_formula_anchor"] == "C1"
    result = execute(doc, {"sheet": "Sheet", "cell_range": "B1:E2",
        "absolute_tolerance": 1e-9, "relative_tolerance": 1e-9})
    assert result["formula_count"] == result["unresolved_count"] == 5
    assert all(row["reason"] == "unsupported_formula_representation" for row in result["outputs"])


def test_agent_tool_executes_real_formulas_preserves_outer_transaction_and_deduplicates(db):
    from app.agent.tools import execute_tool, get_tool_specs, tool_risk
    item = upload(workbook_bytes()); doc = document(item)
    binding = freeze_sources(db, "table-a", [item["revision_id"]], {"conversation_id": "table-a"})
    db.add(Conversation(id="outer-pending", title="must survive nested tool"))
    args = {"source_revision_id": item["revision_id"], "sheet": "实验 数据", "cell_range": "E2:G3"}
    with bind_runtime_config(binding):
        assert any(s["name"] == "verify_attached_table" for s in get_tool_specs())
        value = json.loads(execute_tool(db, "verify_attached_table", args, conversation_id="table-a"))
        repeat = json.loads(execute_tool(db, "verify_attached_table", args, conversation_id="table-a"))
    assert "error" not in value, value
    assert tool_risk("verify_attached_table") == "safe"
    assert value["computation"]["result"]["computed_count"] == 4
    assert value["computation"]["result"]["unresolved_count"] == 2
    assert value["computation"]["extraction_fingerprint"] == doc["fingerprint"]
    assert value["computation"]["id"] == repeat["computation"]["id"]
    assert value["evidence"][0]["revision_id"] == item["revision_id"]
    assert value["original_quote_is_not_computation_result"] is True
    assert db.get(Conversation, "outer-pending") is not None
    receipt_id = value["computation"]["id"]
    db.rollback()
    assert db.get(TableComputation, receipt_id) is None
    assert db.get(Conversation, "outer-pending") is None


@pytest.mark.parametrize("change", [{"source_revision_id": "f"*32}, {"cell_range": "A1:Z20"},
    {"cell_range": "A:A"}, {"cell_range": 1}, {"absolute_tolerance": True},
    {"relative_tolerance": float("nan")}, {"arbitrary_code": "raise Exception()"}])
def test_agent_formula_tool_validates_frozen_scope_ranges_and_tolerances(db, change):
    item = upload(workbook_bytes())
    binding = freeze_sources(db, "table-a", [item["revision_id"]], {"conversation_id": "table-a"})
    with bind_runtime_config(binding):
        value = json.loads(execute_source_tool(db, "verify_attached_table", {
            "source_revision_id": item["revision_id"], "sheet": "实验 数据", "cell_range": "E2:G3", **change}))
    assert value.get("error")
    assert db.scalar(select(func.count()).select_from(TableComputation)) == 0


@pytest.mark.parametrize("row", ['r="100001" hidden="1"', 'r="-1" hidden="1"', 'r="2" hidden="1"/><row r="2"'])
def test_invalid_or_duplicate_hidden_row_ids_are_refused(row):
    book = Workbook(); book.active.append([1]); book.active.append([2])
    initial = BytesIO(); book.save(initial); book.close()
    raw = initial.getvalue(); output = BytesIO(); changed = False
    with zipfile.ZipFile(BytesIO(raw)) as original, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name in original.namelist():
            data = original.read(name)
            if name == "xl/worksheets/sheet1.xml":
                assert b'<row r="2">' in data
                data = data.replace(b'<row r="2">', ("<row " + row + ">").encode())
                changed = True
            target.writestr(name, data)
    assert changed
    from app.source_store import SourceError
    with pytest.raises(SourceError):
        extract(output.getvalue(), MEDIA["xlsx"])


def test_excel_cell_coordinates_cannot_disagree_with_their_containing_row():
    book = Workbook(); book.active.append([1]); book.active.append([2])
    initial = BytesIO(); book.save(initial); book.close(); output = BytesIO()
    with zipfile.ZipFile(initial) as original, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name in original.namelist():
            data = original.read(name)
            if name == "xl/worksheets/sheet1.xml":
                assert b'<c r="A2"' in data
                data = data.replace(b'<c r="A2"', b'<c r="A3"')
            target.writestr(name, data)
    from app.source_store import SourceError
    with pytest.raises(SourceError):
        extract(output.getvalue(), MEDIA["xlsx"])


def test_agent_executor_supplies_tool_saves_actual_receipt_and_clickable_source(db, monkeypatch):
    from app.agent import loop
    from app.llm import LLMResponse
    from app.models import QARecord
    from app.source_runtime import validate_source_evidence
    item = upload(workbook_bytes())
    binding = freeze_sources(db, "table-a", [item["revision_id"]], {
        "mode": "mock", "model_id": "Synthetic table verification", "approval_mode": "full_access"})
    seen = []

    class Model:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            assert any(s["name"] == "verify_attached_table" for s in tools or [])
            if not any(m["role"] == "tool" for m in messages):
                yield "final", LLMResponse("", [{"id": "table-verify-call", "name": "verify_attached_table",
                    "arguments": {"source_revision_id": item["revision_id"], "sheet": "实验 数据", "cell_range": "E2:G3"}}])
            else:
                receipt = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
                assert receipt["computation"]["result"]["computed_count"] == 4
                assert receipt["all_selected_formulas_computed"] is False
                yield "final", LLMResponse("Selected formulas were actually computed. E2 is 5; two formulas remain unresolved.[^e1]")

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *a: "")
    monkeypatch.setattr(loop, "_load_history", lambda *a, **k: ([], {"total": 0}))
    events = list(loop.run_agent(db, "Check selected Excel formulas and report any unresolved formulas.",
        conversation_id="table-a", runtime_config=binding))
    answer = next(payload for kind, payload in events if kind == "answer")
    assert answer["refusal"] == 0 and answer["evidence"][0]["revision_id"] == item["revision_id"]
    assert db.scalar(select(func.count()).select_from(TableComputation)) == 1
    record = db.query(QARecord).one()
    validate_source_evidence(db, "table-a", record.evidence)
    assert len(seen) >= 2


def test_shared_formula_preserves_stored_xml_and_marks_expanded_effective_expression():
    from xml.etree import ElementTree as ET
    book = Workbook(); sheet = book.active
    sheet.append([1, "=A1*2"]); sheet.append([2, "=A2*2"])
    initial = BytesIO(); book.save(initial); book.close(); output = BytesIO()
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(initial) as original, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name in original.namelist():
            data = original.read(name)
            if name == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                for cell in root.findall("m:sheetData/m:row/m:c", ns):
                    if cell.get("r") in ("B1", "B2"):
                        formula = cell.find("m:f", ns)
                        formula.attrib.update(t="shared", si="0")
                        if cell.get("r") == "B1":
                            formula.set("ref", "B1:B2")
                        else:
                            formula.text = None
                data = ET.tostring(root, encoding="utf-8")
            target.writestr(name, data)
    doc = extract(output.getvalue(), MEDIA["xlsx"]).document
    cells = {cell["cell"]: cell for block in doc["blocks"] for cell in block["cells"]}
    assert cells["B1"]["stored_formula_text"] == "A1*2"
    assert cells["B1"]["formula_xml_attributes"] == {"t": "shared", "si": "0", "ref": "B1:B2"}
    assert cells["B2"]["stored_formula_text"] is None and cells["B2"]["formula"] == "=A2*2"
    assert cells["B2"]["formula_origin"] == "shared_formula_expanded_to_this_cell"
    result = execute(doc, {"sheet": "Sheet", "cell_range": "B1:B2", "absolute_tolerance": 1e-9, "relative_tolerance": 1e-9})
    assert [row["value"] for row in result["outputs"]] == [2, 4]


def test_excel_streaming_cell_budget_is_global_before_all_sheets_are_retained(monkeypatch):
    from app import table_extraction
    from app.source_store import SourceError
    book = Workbook(); book.active.append([1, 2]); book.create_sheet("Second").append([3, 4])
    output = BytesIO(); book.save(output); book.close()
    monkeypatch.setattr(table_extraction, "MAX_CELLS", 3)
    with zipfile.ZipFile(output) as archive, pytest.raises(SourceError, match="单元格数量"):
        table_extraction._sheet_xml(archive)
