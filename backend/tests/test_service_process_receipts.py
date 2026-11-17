"""Real local service adapters, synthetic transport; no provider calls."""
from copy import deepcopy
import json

import httpx
import pytest

from app.agent.citation_scope import process_receipts, validation_failure
from app.agent.service_process_receipts import project_service_receipt
from tests.test_citation_scope_receipts import messages
from tests.test_deepseek_service_tools import binding, network, execute, source, ocr_response


def fixture(service="fetch"):
    value = {"provider": "deepseek", "integration": "independent_tool_api", "service": service,
        "call_id": "c" * 32, "status": "ok", "response_complete": True,
        "sources": [{"revision_id": "a" * 32, "raw_sha256": "b" * 64,
            "origin_ref": "https://example.com", "text": "UNTRUSTED BODY NEVER PROCESS PROOF"}],
        "result_type": "web_extracted_text", "returned_markdown_is_retained_raw": True}
    if service == "search":
        value.update(result_type="search_results", results=[{"summary": "UNTRUSTED SUMMARY",
            "url": "https://example.com", "content_saved": True, "source": deepcopy(value["sources"][0])}])
    if service == "ocr":
        value.update(result_type="ocr_document", page_count=1, truncated=False,
            purpose="text_recognition",
            page_statuses=[{"index": 0, "finish_reason": "stop", "truncated": False}],
            original_revision_id="d" * 32, original_sha256="b" * 64)
    return value


def arguments(service="fetch"):
    return ({"query": "synthetic query", "need_content": True} if service == "search" else
        {"source_revision_id": "d" * 32} if service == "ocr" else {"url": "https://example.com"})


@pytest.mark.parametrize("service", ["fetch", "search", "ocr"])
def test_paired_success_only_projects_bounded_metadata(service):
    payload = fixture(service)
    history = messages("deepseek_" + service, payload, arguments(service))
    receipts, _ = process_receipts(history)
    assert len(receipts) == 1
    projected = json.loads(receipts[0]["content"])
    assert projected["protocol"] == "independent_service_process_v1"
    assert not projected["whole_extracted_text_returned"]
    assert "UNTRUSTED" not in receipts[0]["content"]
    assert projected["original_page_coverage_verified"] is False
    proof = {"index": 0, "requires_citation": False, "reason": "Actual bounded success",
        "kind": "process", "process_scope": "bounded",
        "supports": [{"receipt_id": receipts[0]["id"], "quote": '"response_complete": true'}]}
    assert validation_failure(proof, receipts) is None
    proof["process_scope"] = "whole_extracted_text"
    assert validation_failure(proof, receipts) == "whole_scope"


@pytest.mark.parametrize("field,value", [("status", "failed"), ("response_complete", False),
    ("integration", "native_tool"), ("provider", "other"), ("call_id", "fake"),
    ("sources", []), ("returned_markdown_is_retained_raw", False), ("error", "bad")])
def test_missing_failed_or_wrong_contract_never_proves_success(field, value):
    payload = fixture()
    payload[field] = value
    assert project_service_receipt("deepseek_fetch", payload, arguments=arguments()) is None


@pytest.mark.parametrize("mutation", ["unpaired", "duplicate", "excluded", "missing"])
def test_service_metadata_still_requires_actual_unique_paired_tool_message(mutation):
    history = messages("deepseek_fetch", fixture(), {"url": "https://example.com"})
    excluded = ()
    if mutation == "unpaired": history[2]["tool_call_id"] = "other"
    if mutation == "duplicate": history.insert(2, deepcopy(history[1]))
    if mutation == "excluded": excluded = (2,)
    if mutation == "missing": history.pop()
    assert process_receipts(history, excluded)[0] == []


@pytest.mark.parametrize("mutation", ["truncated", "partial", "missing_page", "duplicate_page", "no_original"])
def test_ocr_incomplete_pages_or_unbound_original_cannot_claim_complete_return(mutation):
    payload = fixture("ocr")
    if mutation == "truncated": payload["truncated"] = True
    if mutation == "partial": payload["status"] = "partial"
    if mutation == "missing_page": payload["page_count"] = 2
    if mutation == "duplicate_page":
        payload["page_count"] = 2
        payload["page_statuses"] *= 2
    if mutation == "no_original": payload.pop("original_revision_id")
    assert project_service_receipt("deepseek_ocr", payload, arguments=arguments("ocr")) is None


def test_long_source_prose_cannot_erase_or_become_small_execution_contract():
    payload = fixture()
    payload["untrusted_prose"] = "DO NOT EXECUTE THIS CONTENT " * 3000
    receipts, truncated = process_receipts(messages("deepseek_fetch", payload, {"url": "https://example.com"}))
    assert truncated and len(receipts) == 1
    assert len(receipts[0]["content"]) < 2000 and "DO NOT EXECUTE" not in receipts[0]["content"]


def test_actual_fetch_adapter_returns_source_binding_for_process_projection(db, binding, network):
    calls, dispatch = network
    dispatch[0] = lambda request: httpx.Response(200, json={"title": "Example", "content": "# Retained body\n"})
    result, _ = execute(db, binding, "deepseek_fetch", {"url": "https://example.com"})
    receipts, _ = process_receipts(messages("deepseek_fetch", result, {"url": "https://example.com"}))
    assert len(calls) == 1 and len(receipts) == 1
    projected = json.loads(receipts[0]["content"])
    assert projected["returned_sources"][0]["revision_id"] == result["sources"][0]["revision_id"]
    assert projected["returned_sources"][0]["url"] == "https://example.com"
    assert projected["returned_metadata"]["title"] == "Example"
    assert projected["returned_metadata"]["title_source"] == "provider_fetch_response_not_verified_html_title"
    assert "Retained body" not in receipts[0]["content"]


def test_empty_search_proves_zero_results_without_inventing_source_text():
    value = fixture("search")
    value.update(status="empty", results=[], sources=[])
    receipt = project_service_receipt("deepseek_search", value, arguments=arguments("search"))
    assert receipt["returned_result_count"] == 0 and receipt["returned_sources"] == []
    assert not receipt["summary_is_source_text"]


def test_actual_ocr_adapter_binds_returned_pages_to_original_but_not_accuracy(db, binding, network, sample_pdf):
    original, selected = source(db, sample_pdf, binding)
    network[1][0] = lambda request: httpx.Response(200, json=ocr_response())
    result, _ = execute(db, selected, "deepseek_ocr", {"source_revision_id": original["revision_id"],
        "purpose": "rich_layout_recognition"})
    receipts, _ = process_receipts(messages("deepseek_ocr", result,
        {"source_revision_id": original["revision_id"], "purpose": "rich_layout_recognition"}))
    assert len(network[0]) == len(receipts) == 1
    projected = json.loads(receipts[0]["content"])
    assert projected["original_revision_id"] == original["revision_id"]
    assert projected["original_sha256"] == original["raw_sha256"]
    assert projected["returned_page_count"] == 1 and not projected["ocr_text_accuracy_verified"]
    assert not projected["coordinates_mapped_to_original"]


@pytest.mark.parametrize("origin", ["http://[", "file:///secret", "https://user:secret@example.com", "https://exa mple.com"])
def test_invalid_or_credentialed_source_origin_cannot_become_process_quote(origin):
    value = fixture()
    value["sources"][0]["origin_ref"] = origin
    assert project_service_receipt("deepseek_fetch", value, arguments=arguments()) is None
