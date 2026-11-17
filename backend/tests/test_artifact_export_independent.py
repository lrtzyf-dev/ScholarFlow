"""Independent export invariants beyond container/checksum self-consistency.

The injected renderers stand for serialization defects, not credentials or
arbitrary attacker access. Fixed source, real computation, and authorization
stay unchanged. An actual saved file must agree with those independent inputs.
"""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4
import zipfile
from xml.etree import ElementTree as ET

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import artifact_store
from app.artifact_models import ArtifactExport


OUTPUT = Path(__file__).resolve().parents[2] / "verification" / "artifact-export-independent-current" / "receipts"
REQUIRED = "INDEPENDENT_REQUIRED_BODY_7319"
BODY = "# Method\n" + REQUIRED + "\n\n| Group | Count |\n|---|---|\n| A | 3 |\n"
CSV = "site,score\r\nA,1\r\nB,\r\nC,3\r\n"


def save_receipt(name, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / f"{name}-{uuid4().hex}.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def source(db, *, analysis=False, cid=None):
    return artifact_store.create(db, request_id=uuid4().hex, conversation_id=cid,
        title="Independent fixed export", kind="analysis" if analysis else "document",
        content=CSV if analysis else BODY, evidence=[], analysis={"category_column": "site",
            "numeric_columns": ["score"], "chart_type": "bar", "units": {"score": "count"}}
        if analysis else {})


def repack(payload, mutate):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    mutate(members)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


def damaged_svg(payload):
    root = ET.fromstring(payload)
    namespace = "{http://www.w3.org/2000/svg}"
    labels = root.findall(".//" + namespace + "title")
    assert any("A / score: 1.0" in (label.text or "") for label in labels)
    for label in labels:
        if "A / score: 1.0" in (label.text or ""):
            label.text = "A / score: 999.0"
    bars = root.findall(namespace + "rect")
    assert len(bars) > 1
    bars[1].set("height", "1.000")
    return ET.tostring(root, encoding="utf-8")


def wrong_renderer(monkeypatch):
    from app import artifact_export_formats as formats
    original = formats.render

    def render(*args, **kwargs):
        payload, generation = original(*args, **kwargs)
        generation = deepcopy(generation)
        format = args[4]
        if format == "docx":
            def mutate(members):
                raw = members["word/document.xml"]
                assert REQUIRED.encode() in raw
                raw = raw.replace(REQUIRED.encode(), b"UNRELATED_SERIALIZED_BODY_9921")
                members["word/document.xml"] = raw
                # The descriptor is deliberately self-consistent with the bad
                # serialized document. It is not independent source evidence.
                generation["descriptor"]["document_xml_sha256"] = artifact_store.digest(raw)
                generation["descriptor"]["text_sha256"] = artifact_store.digest("".join(ET.fromstring(raw).itertext()))
            payload = repack(payload, mutate)
        elif format == "xlsx":
            from openpyxl import load_workbook
            book = load_workbook(io.BytesIO(payload))
            book["NumericData"]["B2"] = 999
            output = io.BytesIO()
            book.save(output)
            book.close()
            payload = output.getvalue()
        elif format == "svg":
            payload = damaged_svg(payload)
            generation["descriptor"]["svg_sha256"] = artifact_store.digest(payload)
        elif format == "bundle":
            def mutate(members):
                result = json.loads(members["result.json"])
                result["points"][0]["values"]["score"] = 999.0
                result["stats"]["score"]["max"] = 999.0
                result["stats"]["score"]["mean"] = 501.0
                result["svg"] = damaged_svg(members["chart.svg"]).decode("utf-8")
                members["chart.svg"] = result["svg"].encode("utf-8")
                members["result.json"] = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode("utf-8")
                generation["descriptor"]["result_sha256"] = artifact_store.digest(json.dumps(
                    result, ensure_ascii=False, sort_keys=True, allow_nan=False))
                manifest = json.loads(members["manifest.json"])
                manifest["files"]["result.json"] = artifact_store.digest(members["result.json"])
                manifest["files"]["chart.svg"] = artifact_store.digest(members["chart.svg"])
                members["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            payload = repack(payload, mutate)
        return payload, generation

    monkeypatch.setattr(formats, "render", render)


@pytest.mark.parametrize("format", ["docx", "xlsx", "svg", "bundle"])
def test_self_consistent_serialization_cannot_certify_wrong_source_content(db, monkeypatch, format):
    from app import artifact_exports
    from app.artifact_compute import recompute
    item = source(db, analysis=format != "docx")
    if format != "docx":
        row = artifact_store.get(db, item["id"])
        recompute(db, row, artifact_store.revision(db, row, item["current_revision_id"]))
    wrong_renderer(monkeypatch)
    receipt = None
    error_status = None
    try:
        receipt = artifact_exports.create(db, artifact_id=item["id"], conversation_id=None,
            revision_id=item["current_revision_id"], request_id=uuid4().hex, format=format)
    except HTTPException as error:
        error_status = error.status_code
    save_receipt("self-consistent-" + format, {"artifact_id": item["id"],
        "revision_id": item["current_revision_id"], "format": format, "rejected_http": error_status,
        "export_id": receipt["export_id"] if receipt else None,
        "payload_sha256": receipt["payload_sha256"] if receipt else None,
        "validation": receipt["validation"] if receipt else None,
        "saved_export_count": db.query(ArtifactExport).count()})
    assert error_status == 422, "Structurally valid but source-inconsistent file was certified and persisted"
    assert db.query(ArtifactExport).count() == 0


def test_goal_contains_must_not_be_satisfied_only_by_source_when_docx_body_is_missing(db, monkeypatch):
    from app import goal_contracts, goal_ledger, run_lifecycle
    from app.artifact_tools import execute
    from app.main import app
    from app.models import Conversation
    from app.runtime_context import bind_runtime_config
    from app.runtime_models import RuntimeRun
    cid, rid = "independent-cid-" + uuid4().hex, "independent-run-" + uuid4().hex
    db.add(Conversation(id=cid, title="Independent export contract"))
    db.flush()
    attempt, _ = run_lifecycle.admit_attempt(db, rid, cid, "核对当前固定成果")
    goal = goal_ledger.snapshot_goal(db, db.get(RuntimeRun, rid).goal_id)
    goal = goal_contracts.user_change(db, goal["id"], expected_revision=goal["revision"],
        request_id=uuid4().hex, action="add", title="Required exported body", contract={
            "kind": "artifact_export_result", "format": "docx", "contains": [REQUIRED]})
    db.commit()
    item = source(db, cid=cid)
    binding = {"conversation_id": cid, "run_id": rid, "goal_id": goal["id"],
        "attempt_id": attempt.id, "writer_token": attempt.writer_token}
    wrong_renderer(monkeypatch)
    exported, rejected = None, None
    try:
        with bind_runtime_config(binding):
            exported = execute(db, "export_research_artifact", {"artifact_id": item["id"],
                "revision_id": item["current_revision_id"], "request_id": uuid4().hex,
                "format": "docx"}, conversation_id=cid)
    except HTTPException as error:
        rejected = error.status_code
        assert rejected == 422
    text, byte_sha256 = "", None
    if exported:
        goal_ledger.record_contract_tool_result(db, goal["id"], "export_research_artifact", {}, exported, uuid4().hex)
        client = TestClient(app)
        try:
            response = client.get(exported["download_url"])
        finally:
            client.close()
        assert response.status_code == 200
        byte_sha256 = hashlib.sha256(response.content).hexdigest()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
            text = "".join(root.itertext())
        assert REQUIRED not in text, "The counterexample must really remove the requested body"
    requirement = next(row for row in goal_ledger.snapshot_goal(db, goal["id"])["requirements"]
        if (row["tool_arguments"] or {}).get("result_contract", {}).get("kind") == "artifact_export_result")
    save_receipt("goal-source-only", {"artifact_id": item["id"], "revision_id": item["current_revision_id"],
        "export_id": exported["export_id"] if exported else None, "rejected_http": rejected,
        "actual_payload_sha256": byte_sha256, "required_text_present_in_actual_docx": REQUIRED in text,
        "goal_requirement_status": requirement["status"], "source_contains_required_text": REQUIRED in BODY})
    assert requirement["status"] == "open", "Goal completed on source text while actual DOCX lacks the required body"


def test_correct_actual_docx_still_preserves_required_body_without_semantic_claims(db):
    from app import artifact_exports
    item = source(db)
    receipt = artifact_exports.create(db, artifact_id=item["id"], conversation_id=None,
        revision_id=item["current_revision_id"], request_id=uuid4().hex, format="docx")
    row = artifact_exports.get(db, item["id"], receipt["export_id"])
    with zipfile.ZipFile(io.BytesIO(row.payload)) as archive:
        text = "".join(ET.fromstring(archive.read("word/document.xml")).itertext())
    assert REQUIRED in text and "Group" in text and "Count" in text
    assert receipt["validation"]["semantic"] == "not_assessed"


@pytest.mark.parametrize("format", ["docx", "bundle"])
def test_actual_api_response_lost_after_commit_reuses_exact_payload_on_retry(db, monkeypatch, format):
    import httpx
    from app import artifact_export_formats as formats
    from app.main import app
    item = source(db, analysis=format == "bundle")
    payload = {"revision_id": item["current_revision_id"], "request_id": uuid4().hex, "format": format}
    endpoint = f"/api/artifacts/{item['id']}/exports"
    client = TestClient(app)
    lost = False

    def lose_response(response):
        nonlocal lost
        if response.request.method == "POST" and response.request.url.path == endpoint and not lost:
            assert response.status_code == 200
            lost = True
            raise httpx.ReadError("synthetic response lost after the server commit", request=response.request)

    client.event_hooks["response"].append(lose_response)
    try:
        with pytest.raises(httpx.ReadError):
            client.post(endpoint, json=payload)
        saved = db.query(ArtifactExport).one()
        original_id, original_bytes, original_sha = saved.id, bytes(saved.payload), saved.payload_sha256
        monkeypatch.setattr(formats, "render", lambda *a, **k: pytest.fail("Lost response retry regenerated bytes"))
        response = client.post(endpoint, json=payload)
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["export_id"] == original_id and receipt["payload_sha256"] == original_sha
        downloaded = client.get(receipt["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content == original_bytes
        assert hashlib.sha256(downloaded.content).hexdigest() == original_sha
        assert db.query(ArtifactExport).count() == 1
        save_receipt("lost-response-" + format, {"artifact_id": item["id"],
            "revision_id": item["current_revision_id"], "export_id": original_id,
            "actual_payload_sha256": original_sha, "response_dropped_after_committed_export": lost,
            "replay_same_export_id_and_bytes": True, "saved_export_count": 1})
    finally:
        client.close()


@pytest.mark.parametrize("format", ["xlsx", "svg", "bundle"])
def test_corrupted_successful_computation_summary_cannot_keep_recorded_export_valid(db, format):
    from app import artifact_exports
    from app.artifact_compute import recompute
    from app.artifact_models import ArtifactComputation
    from sqlalchemy import update
    item = source(db, analysis=True)
    artifact = artifact_store.get(db, item["id"])
    computed = recompute(db, artifact, artifact_store.revision(db, artifact, item["current_revision_id"]))
    receipt = artifact_exports.create(db, artifact_id=item["id"], conversation_id=None,
        revision_id=item["current_revision_id"], request_id=uuid4().hex, format=format)
    assert receipt["validation"]["computation_recorded"] is True
    record = db.get(ArtifactComputation, computed["id"])
    result = deepcopy(record.result)
    # Leave data_sha256/result_sha256/status/id untouched. A real computation
    # summary now contradicts the same fixed source and actual saved values.
    result["stats"]["score"]["mean"] = 999.0
    db.execute(update(ArtifactComputation).where(ArtifactComputation.id == record.id).values(result=result))
    db.commit()
    rejected = None
    try:
        artifact_exports.get(db, item["id"], receipt["export_id"])
    except HTTPException as error:
        rejected = error.status_code
    save_receipt("computation-summary-" + format, {"artifact_id": item["id"],
        "revision_id": item["current_revision_id"], "export_id": receipt["export_id"],
        "payload_sha256": receipt["payload_sha256"], "source_and_result_hash_fields_unchanged": True,
        "recorded_summary_mean_corrupted": True, "read_rejected_http": rejected})
    assert rejected == 409, "Changed computation facts kept their original success claim through stale hash fields"


def test_all_actual_saved_export_gets_are_sql_read_only_and_execute_no_program(db, monkeypatch):
    from sqlalchemy import event
    from app import artifact_exports, artifact_export_formats as formats, artifact_compute
    from app.db import engine
    from app.main import app
    document, analysis = source(db), source(db, analysis=True)
    row = artifact_store.get(db, analysis["id"])
    artifact_compute.recompute(db, row, artifact_store.revision(db, row, analysis["current_revision_id"]))
    exports = []
    for format in ("docx", "md", "manifest", "xlsx", "svg", "bundle"):
        item = document if format in ("docx", "md", "manifest") else analysis
        exports.append(artifact_exports.create(db, artifact_id=item["id"], conversation_id=None,
            revision_id=item["current_revision_id"], request_id=uuid4().hex, format=format))

    def no_program(*args, **kwargs):
        pytest.fail("A saved-file GET executed a renderer or analysis program")

    for module, names in ((formats, ("render", "execute", "validate_analysis")),
                          (artifact_compute, ("execute", "recompute", "bundle"))):
        for name in names:
            monkeypatch.setattr(module, name, no_program)
    sql_verbs = []

    def no_mutation(connection, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(None, 1)[0].upper()
        sql_verbs.append(verb)
        assert verb in ("SELECT", "PRAGMA", "BEGIN", "ROLLBACK"), "Saved-file GET attempted SQL mutation"

    client = TestClient(app)
    event.listen(engine, "before_cursor_execute", no_mutation)
    try:
        for item in (document, analysis):
            response = client.get(f"/api/artifacts/{item['id']}/exports")
            assert response.status_code == 200
        for exported in exports:
            detail = client.get(f"/api/artifacts/{exported['artifact_id']}/exports/{exported['export_id']}")
            assert detail.status_code == 200
            response = client.get(exported["download_url"])
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == exported["payload_sha256"]
        assert "SELECT" in sql_verbs
    finally:
        event.remove(engine, "before_cursor_execute", no_mutation)
        client.close()
    assert db.query(ArtifactExport).count() == 6


def test_completed_goal_rechecks_each_export_preserves_history_and_reopens_only_after_all_are_invalid(db):
    from sqlalchemy import delete, update
    from app import artifact_exports, goal_contracts, goal_ledger, run_lifecycle
    from app.models import Conversation
    from app.runtime_models import RuntimeRun
    cid, rid = "independent-sequence-" + uuid4().hex, "independent-sequence-run-" + uuid4().hex
    db.add(Conversation(id=cid, title="Independent receipt invalidation sequence"))
    db.flush()
    run_lifecycle.admit_attempt(db, rid, cid, "核对当前固定成果")
    goal = goal_ledger.snapshot_goal(db, db.get(RuntimeRun, rid).goal_id)
    goal = goal_contracts.user_change(db, goal["id"], expected_revision=goal["revision"],
        request_id=uuid4().hex, action="add", title="Fixed document delivery", contract={
            "kind": "artifact_export_result", "format": "docx", "contains": [REQUIRED]})
    db.commit()
    item = source(db, cid=cid)
    exports = [artifact_exports.create(db, artifact_id=item["id"], conversation_id=cid,
        revision_id=item["current_revision_id"], request_id=uuid4().hex, format="docx",
        origin_goal_id=goal["id"], origin_run_id=rid) for _ in range(2)]
    for exported in exports:
        goal_ledger.record_contract_tool_result(db, goal["id"], "export_research_artifact", {},
            {"export_id": exported["export_id"]}, uuid4().hex)

    def requirement(snapshot):
        return next(row for row in snapshot["requirements"]
            if (row["tool_arguments"] or {}).get("result_contract", {}).get("kind") == "artifact_export_result")

    complete = goal_ledger.finalize_goal(db, goal["id"])
    history = deepcopy(requirement(complete)["evidence"])
    assert complete["outcome"] == "completed" and complete["completed_at"]
    assert len(history) == 2
    db.execute(update(ArtifactExport).where(ArtifactExport.id == exports[0]["export_id"]).values(payload=b"broken container"))
    db.flush()
    one_valid = goal_ledger.finalize_goal(db, goal["id"])
    one_history = deepcopy(requirement(one_valid)["evidence"])
    assert one_valid["outcome"] == "completed"
    assert one_valid["completed_at"] == complete["completed_at"]
    assert requirement(one_valid)["status"] == "completed"
    assert one_history[:len(history)] == history
    assert any(proof.get("export_id") == exports[0]["export_id"] and proof.get("reference_unavailable")
        for proof in one_history)
    db.execute(delete(ArtifactExport).where(ArtifactExport.id == exports[1]["export_id"]))
    db.flush()
    all_invalid = goal_ledger.finalize_goal(db, goal["id"])
    invalid_history = deepcopy(requirement(all_invalid)["evidence"])
    assert all_invalid["outcome"] == "partial" and all_invalid["completed_at"] is None
    assert requirement(all_invalid)["status"] == "open"
    assert invalid_history[:len(one_history)] == one_history
    repeated = goal_ledger.finalize_goal(db, goal["id"])
    assert repeated["outcome"] == "partial"
    assert repeated["revision"] == all_invalid["revision"]
    assert requirement(repeated)["evidence"] == invalid_history
    save_receipt("goal-invalidation-sequence", {"artifact_id": item["id"], "goal_id": goal["id"],
        "export_ids": [exported["export_id"] for exported in exports],
        "outcomes": [complete["outcome"], one_valid["outcome"], all_invalid["outcome"], repeated["outcome"]],
        "history_counts": [len(history), len(one_history), len(invalid_history), len(requirement(repeated)["evidence"])],
        "repeated_finalization_revision_unchanged": True})
