"""Completed OCR -> fixed local text reads; synthetic HTTP only, no network."""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest
from sqlalchemy import delete, update

from app import source_store, source_runtime, source_tools, service_tools
from app.agent import readonly_cache
from app.runtime_context import bind_runtime_config
from app.runtime_models import RuntimeRun, RunAttempt
from app.service_tool_models import ServiceToolInvocation
from app.models import Conversation, ConversationOptions
from app.source_models import SourceSnapshot, SourceExtraction
from app.timeutil import utcnow
from .test_deepseek_service_tools import binding, network, source, execute, ocr_response, CID


@pytest.fixture
def completed_ocr(db, binding, network, sample_pdf):
    run_id, attempt_id = "controlled-ocr-read-run", "a" * 32
    from app.cancel_store import clear
    clear(run_id)
    from app.goal_ledger import ensure_run_goal
    goal = ensure_run_goal(db, run_id, CID, "Read the selected original with OCR and review its retained text.")
    db.add(RuntimeRun(id=run_id, kind="foreground", conversation_id=CID,
                      goal_id=goal["id"], current_attempt_id=attempt_id, status="running"))
    db.add(RunAttempt(id=attempt_id, run_id=run_id, number=1, writer_token="synthetic-ocr-reader",
                      status="running"))
    db.commit()
    selected, frozen = source(db, sample_pdf, {**binding, "run_id": run_id, "attempt_id": attempt_id,
                                            "goal_id": goal["id"]})
    response = ocr_response()
    response["pages"][0]["text"] = "\n".join(f"Retained OCR line {i}: synthetic evidence." for i in range(1, 261))
    network[1][0] = lambda request: httpx.Response(200, json=response)
    result, prepared = execute(db, frozen, "deepseek_ocr", {"source_revision_id": selected["revision_id"],
                               "purpose": "rich_layout_recognition"},
                               operation="controlled-ocr-local-read")
    invocation = db.get(ServiceToolInvocation, prepared.receipt_id, populate_existing=True)
    assert invocation.state == "completed" and not result.get("error"), result
    assert len(network[0]) == 1
    assert result["sources"][0]["media_type"] == "application/pdf"
    return {"original": selected, "binding": frozen, "result": result, "receipt_id": prepared.receipt_id,
            "derived_id": result["sources"][0]["revision_id"], "mock_http": network[0]}


def read(db, data, *, binding=None, cache=None, action=None):
    frozen = binding or data["binding"]
    arguments = {"source_revision_id": data["derived_id"], "line_start": 121, "line_end": 130}
    with bind_runtime_config(frozen):
        actual = action or (lambda: source_tools.execute_source_tool(db, "read_attached_source", arguments,
                                                                     conversation_id=CID))
        if cache is None:
            return json.loads(actual())
        return json.loads(readonly_cache.attached_read(db, arguments, binding=frozen,
            conversation_id=CID, cache=cache, action=actual))


@pytest.mark.parametrize("cached", [False, True])
def test_completed_ocr_derived_pdf_reads_retained_text_lines_beyond_initial_evidence(db, completed_ocr, cached):
    data = completed_ocr
    frozen = deepcopy(data["binding"])
    payload = read(db, data, cache={} if cached else None)
    assert not payload.get("error"), payload
    assert payload["evidence"][0]["revision_id"] == data["derived_id"]
    assert payload["read_range"] == {"kind": "text_lines", "line_start": 121, "line_end": 130,
                                     "total_lines": 260, "next_line": 131}
    assert "Retained OCR line 121:" in payload["evidence"][0]["quote"]
    assert "Retained OCR line 130:" in payload["evidence"][0]["quote"]
    assert payload["evidence"][0]["page"] == 0 and payload["evidence"][0]["bbox"] is None
    assert payload["read_contract"]["method"] == "retained_ocr_text_lines"
    assert data["binding"] == frozen
    assert data["derived_id"] not in frozen["source_revision_ids"]
    assert len(data["mock_http"]) == 1
    lineage = payload["source"]["local_read_authorization"]
    assert lineage["service_receipt_id"] == data["receipt_id"]
    assert lineage["original_revision_id"] == data["original"]["revision_id"]
    assert lineage["original_raw_sha256"] == data["original"]["raw_sha256"]
    assert lineage["derived_revision_id"] == data["derived_id"]
    assert not lineage["authorizes_external_transfer"] and not lineage["authorizes_paid_calls"]


@pytest.mark.parametrize("state", ["prepared", "started", "failed", "unknown"])
def test_unfinished_or_failed_invocation_never_opens_derived_text(db, completed_ocr, state):
    data = completed_ocr
    db.get(ServiceToolInvocation, data["receipt_id"]).state = state
    db.commit()
    result = read(db, data)
    assert result["error"] and not result.get("evidence") and result["text_read"] is False
    assert len(data["mock_http"]) == 1


@pytest.mark.parametrize("change", ["policy", "archive", "cancel", "run_stopped", "attempt_stopped", "missing_finished_at"])
def test_current_authority_revocation_blocks_the_local_derived_permit(db, completed_ocr, change):
    data = completed_ocr
    receipt = db.get(ServiceToolInvocation, data["receipt_id"])
    if change == "policy":
        db.get(ConversationOptions, CID).revision += 1
    elif change == "archive":
        db.get(Conversation, CID).archived_at = utcnow()
    elif change == "cancel":
        from app import cancel_store
        cancel_store.request_cancel(data["binding"]["run_id"])
    elif change == "run_stopped":
        db.get(RuntimeRun, data["binding"]["run_id"]).status = "cancelled"
    elif change == "attempt_stopped":
        db.get(RunAttempt, data["binding"]["attempt_id"]).status = "cancelled"
    else:
        receipt.finished_at = None
    db.commit()
    result = read(db, data)
    assert result["error"] and not result.get("evidence")
    assert len(data["mock_http"]) == 1


@pytest.mark.parametrize("change", ["credential_revoke", "credential_delete", "configuration", "service_disabled"])
def test_external_credential_lifecycle_does_not_revoke_retained_local_ocr(db, completed_ocr, monkeypatch, change):
    data = completed_ocr
    receipt = db.get(ServiceToolInvocation, data["receipt_id"])
    from app.service_tool_models import ServiceToolCredential, ServiceToolConfiguration
    if change == "credential_revoke":
        db.get(ServiceToolCredential, receipt.credential_ref).revoked_at = utcnow()
    elif change == "credential_delete":
        db.execute(delete(ServiceToolCredential).where(ServiceToolCredential.id == receipt.credential_ref))
    elif change == "configuration":
        db.get(ServiceToolConfiguration, "deepseek").version += 1
    else:
        db.get(ServiceToolConfiguration, "deepseek").enabled_services = []
    db.commit()
    from app import service_tool_config
    monkeypatch.setattr(service_tool_config, "unprotect", lambda *args: pytest.fail("local OCR read cannot decrypt a key"))
    assert read(db, data)["evidence"][0]["revision_id"] == data["derived_id"]
    assert len(data["mock_http"]) == 1


def test_local_derivative_permission_does_not_authorize_default_or_paid_source_access(db, completed_ocr):
    data = completed_ocr
    with pytest.raises(source_store.SourceAccessDenied):
        source_runtime.require_attached_revision(db, data["derived_id"], binding=data["binding"])
    with pytest.raises(source_store.SourceAccessDenied):
        service_tools.prepare_deepseek_tool(db, "deepseek_ocr", {"source_revision_id": data["derived_id"]},
            conversation_id=CID, operation_key="must-not-resubmit-derived", binding=data["binding"])
    assert db.query(ServiceToolInvocation).count() == 1
    assert len(data["mock_http"]) == 1


@pytest.mark.parametrize("damaged", ["original_raw", "derived_raw", "derived_text", "extraction"])
def test_cached_derived_reads_revalidate_original_and_derivative_and_discard_stale_body(db, completed_ocr, damaged):
    data = completed_ocr
    cache = {}
    assert read(db, data, cache=cache)["evidence"]
    db.commit()
    assert len(cache) == 1
    if damaged == "extraction":
        row = db.get(SourceExtraction, data["derived_id"])
        document = deepcopy(row.document)
        document["text"] += "\nwrong"
        db.execute(update(SourceExtraction).where(SourceExtraction.revision_id == row.revision_id).values(document=document))
    else:
        rid = data["original"]["revision_id"] if damaged == "original_raw" else data["derived_id"]
        record = source_store.read_revision(db, rid, scope=source_store.SourceScope("conversation", CID))
        values = {"text": "damaged OCR text"} if damaged == "derived_text" else {"raw_bytes": b"damaged original bytes"}
        db.execute(update(SourceSnapshot).where(SourceSnapshot.id == record["snapshot_id"]).values(**values))
    db.commit()
    with pytest.raises(source_store.SourceError):
        read(db, data, cache=cache, action=lambda: pytest.fail("damaged cache cannot dispatch a read"))
    assert not cache
    assert len(data["mock_http"]) == 1


def test_same_origin_new_selection_never_borrows_prior_ocr_result(db, completed_ocr):
    data = completed_ocr
    scope = source_store.SourceScope("conversation", CID)
    original = data["original"]
    new = source_store.capture_source(db, scope=scope,
        authorization=source_store.SourceAuthorization(scope, "explicit-original-update", "upload"), kind="upload",
        title="New fixed original.pdf", raw_bytes=original["raw_sha256"].encode(), media_type="application/pdf",
        source_id=original["source_id"], expected_version=original["version"])
    db.commit()
    newer = source_runtime.freeze_sources(db, CID, [new["revision_id"]], data["binding"])
    assert read(db, data, binding=newer)["error"]
    # An explicit old-version selection remains the original immutable version;
    # changing the mutable head alone never silently substitutes the new bytes.
    assert read(db, data)["evidence"][0]["revision_id"] == data["derived_id"]
    assert len(data["mock_http"]) == 1


def test_explicit_user_selection_of_retained_ocr_pdf_uses_ocr_lines(db, completed_ocr):
    data = completed_ocr
    explicit = source_runtime.freeze_sources(db, CID, [data["derived_id"]], data["binding"])
    result = read(db, data, binding=explicit)
    assert result["read_contract"]["method"] == "retained_ocr_text_lines"
    assert result["evidence"][0]["bbox"] is None
    assert "local_read_authorization" not in result["source"]
    assert len(data["mock_http"]) == 1


@pytest.mark.parametrize("missing_line", [False, True])
def test_actual_ocr_segment_reads_aggregate_only_retained_text_and_never_original_pages(db, completed_ocr, missing_line):
    data = completed_ocr
    history = []
    with bind_runtime_config(data["binding"]):
        for index, (start, end) in enumerate([(1, 129 if missing_line else 130), (131, 260)]):
            arguments = {"source_revision_id": data["derived_id"], "line_start": start, "line_end": end}
            result = source_tools.execute_source_tool(db, "read_attached_source", arguments, conversation_id=CID)
            assert not json.loads(result).get("error"), result
            history.extend([{"role": "assistant", "tool_calls": [{"id": f"ocr-read-{index}", "type": "function",
                "function": {"name": "read_attached_source", "arguments": json.dumps(arguments)}}]},
                {"role": "tool", "tool_call_id": f"ocr-read-{index}", "content": result}])
    from app.agent.citation_scope import process_receipts, validate_result
    receipts, _ = process_receipts(history)
    aggregates = [item for item in receipts if item["tool_name"] == "read_attached_source_coverage"]
    assert len(aggregates) == 1
    aggregate = aggregates[0]
    payload = json.loads(aggregate["content"])
    assert aggregate["whole_extracted_text_returned"] is not missing_line
    assert payload["read_contract"]["method"] == "retained_ocr_text_lines"
    assert payload["read_contract"]["source_extraction_scope"] == "retained_ocr_text_only"
    assert payload["fixed_source"]["revision_id"] == data["derived_id"]
    assert payload["read_range"]["kind"] == "text_lines" and "page_count" not in payload["read_range"]
    assert payload["read_range"]["observed_intervals"] == ([[1, 129], [131, 260]] if missing_line else [[1, 260]])
    assert payload["original_visual_content_observed"] is False
    assert payload["extraction_accuracy_verified"] is False
    assert {"original_page_geometry", "original_layout", "ocr_accuracy_verification"} <= set(payload["read_contract"]["omitted_content"])
    proof = {"index": 0, "requires_citation": False, "kind": "process", "reason": "Retained OCR text coverage only",
        "process_scope": "whole_extracted_text", "supports": [{"receipt_id": aggregate["id"], "quote": aggregate["content"]}]}
    assert validate_result(proof, receipts) is not missing_line
    assert len(data["mock_http"]) == 1


def test_older_pdf_textlayer_cache_cannot_replace_selected_ocr_text_representation(db, completed_ocr):
    data = completed_ocr
    explicit = source_runtime.freeze_sources(db, CID, [data["derived_id"]], data["binding"])
    cache = {}
    good = read(db, data, binding=explicit, cache=cache)
    assert good["read_contract"]["method"] == "retained_ocr_text_lines"
    db.commit()
    entry = deepcopy(next(iter(cache.values())))
    entry["identity"]["tool_version"] = "source-reader-v2"
    entry["result"] = json.dumps({"read_contract": {"method": "pdfplumber_text_layer"},
                                  "evidence": [{"quote": "stale original PDF layer"}]})
    entry["result_sha256"] = hashlib.sha256(entry["result"].encode()).hexdigest()
    old_key = "attached/" + hashlib.sha256(json.dumps(entry["identity"], sort_keys=True, ensure_ascii=False,
                                                    separators=(",", ":")).encode()).hexdigest()
    old_cache = {old_key: entry}
    refreshed = read(db, data, binding=explicit, cache=old_cache)
    assert refreshed["read_contract"]["method"] == "retained_ocr_text_lines"
    assert "Retained OCR line 121" in refreshed["evidence"][0]["quote"]
