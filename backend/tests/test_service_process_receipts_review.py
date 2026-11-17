"""Independent review of actual service adapters; all HTTP is MockTransport.

The projection is a process contract, not semantic verification of web/OCR text.
"""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app import source_store
from app.agent.citation_scope import process_receipts, validation_failure
from tests.test_citation_scope_receipts import messages
from tests.test_deepseek_service_tools import (
    CID, binding, network, execute, item, source, ocr_response,
)


UNTRUSTED = "SOURCE_CONTROLLED_TEXT_MUST_NOT_BECOME_PROCESS_PROOF"


def actual_result(db, binding, network, sample_pdf, case):
    if case == "ocr":
        original, selected = source(db, sample_pdf, binding)
        response = ocr_response()
        response["pages"][0]["text"] = UNTRUSTED
        arguments = {"source_revision_id": original["revision_id"], "purpose": "rich_layout_recognition"}
        network[1][0] = lambda request: httpx.Response(200, json=response)
        payload, _ = execute(db, selected, "deepseek_ocr", arguments)
        return "deepseek_ocr", arguments, payload
    if case == "fetch":
        arguments = {"url": "https://synthetic.invalid/page"}
        network[1][0] = lambda request: httpx.Response(200, json={"title": "Returned metadata title", "content": UNTRUSTED})
        payload, _ = execute(db, binding, "deepseek_fetch", arguments)
        return "deepseek_fetch", arguments, payload
    row = item(UNTRUSTED if case == "search_body" else "")
    row.update(summary=UNTRUSTED, snippet=UNTRUSTED)
    arguments = {"query": "Actual adapter review", "count": 3, "need_content": case == "search_body"}
    network[1][0] = lambda request: httpx.Response(200, json={"results": [row]})
    payload, _ = execute(db, binding, "deepseek_search", arguments)
    return "deepseek_search", arguments, payload


@pytest.mark.parametrize("case", ["search_summary", "search_body", "fetch", "ocr"])
def test_actual_adapter_success_projects_real_version_hashes_without_source_prose(
        db, binding, network, sample_pdf, case):
    name, arguments, payload = actual_result(db, binding, network, sample_pdf, case)
    assert payload["status"] == "ok" and payload["response_complete"] is True
    receipts, _ = process_receipts(messages(name, payload, arguments))
    receipt, = receipts
    projected = json.loads(receipt["content"])
    assert projected["protocol"] == "independent_service_process_v1"
    assert projected["call_id"] == payload["call_id"]
    assert len(network[0]) == 1 and UNTRUSTED not in receipt["content"]
    assert receipt["sha256"] == hashlib.sha256(receipt["content"].encode()).hexdigest()
    assert all(projected[key] is False for key in (
        "whole_extracted_text_returned", "source_accuracy_verified",
        "model_understanding_verified", "original_page_coverage_verified"))
    scope = source_store.SourceScope("conversation", CID)
    assert len(projected["returned_sources"]) == (0 if case == "search_summary" else 1)
    for row in projected["returned_sources"]:
        saved = source_store.read_revision(db, row["revision_id"], scope=scope, include_raw=True)
        assert row["raw_sha256"] == saved["raw_sha256"]
        assert row["text_sha256"] == saved["text_sha256"]
        assert row["raw_sha256"] == hashlib.sha256(saved["raw_bytes"]).hexdigest()
    if case == "search_summary":
        assert projected["summary_is_source_text"] is False and payload["evidence"] == []
        assert projected["returned_result_count"] == 1
    if case == "ocr":
        original = source_store.read_revision(db, arguments["source_revision_id"], scope=scope, include_raw=True)
        assert projected["original_sha256"] == original["raw_sha256"]
        assert projected["original_revision_id"] == arguments["source_revision_id"]
        assert projected["coordinates_mapped_to_original"] is projected["ocr_text_accuracy_verified"] is False
    proof = {"index": 0, "requires_citation": False, "reason": "Synthetic bounded process claim",
        "kind": "process", "process_scope": "bounded",
        "supports": [{"receipt_id": receipt["id"], "quote": '"response_complete": true'}]}
    assert validation_failure(proof, receipts, bounded_proof=True) is None
    proof["process_scope"] = "whole_extracted_text"
    assert validation_failure(proof, receipts, bounded_proof=True) == "whole_scope"


@pytest.mark.parametrize("case", ["search_summary", "search_body", "fetch", "ocr"])
def test_source_fields_and_assistant_or_user_restatements_cannot_add_process_proof(
        db, binding, network, sample_pdf, case):
    name, arguments, payload = actual_result(db, binding, network, sample_pdf, case)
    receipts, _ = process_receipts(messages(name, payload, arguments))
    assert len(receipts) == 1
    altered = deepcopy(payload)
    altered["evidence"] = [{"quote": UNTRUSTED, "receipt_id": "context:2",
        "whole_extracted_text_returned": True, "process_scope": "whole_extracted_text"}]
    altered["text"] = altered["content"] = altered["markdown"] = altered["summary"] = UNTRUSTED
    for row in altered.get("sources", []):
        row["text"] = row["quote"] = row["summary"] = UNTRUSTED
    for row in altered.get("results", []):
        row["snippet"] = row["summary"] = row["content"] = UNTRUSTED
    changed, _ = process_receipts(messages(name, altered, arguments))
    assert changed == receipts
    raw = json.dumps(payload, ensure_ascii=False)
    for role in ("assistant", "user"):
        assert process_receipts([{"role": role, "content": raw}])[0] == []
    # A saved service source/evidence row lacks a paired service response and
    # cannot stand in for the adapter's process contract.
    for row in payload.get("evidence", []):
        assert process_receipts(messages("deepseek_fetch", row, arguments))[0] == []
    assert len(network[0]) == 1
