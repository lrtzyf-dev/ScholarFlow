"""OCR purpose receipts from the production adapter; synthetic transport only.

The purpose is a bounded parameter of a completed service invocation. It is not
proof of OCR accuracy, page coordinates, whole-page observation or understanding.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
import socket

import httpx
import pytest

from app.agent.citation_scope import process_receipts, validation_failure
from tests.test_citation_scope_receipts import messages
from tests.test_deepseek_service_tools import (
    binding, execute, network, ocr_response, source,
)


OCR_BODY = "OCR-BODY-IS-UNTRUSTED-NOT-PROCESS-PROOF-42"


@pytest.fixture(autouse=True)
def reject_socket_traffic(monkeypatch):
    # Windows asyncio creates its own loopback socket pair. It is not provider
    # traffic; every actual service request still uses the MockTransport fixture.
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guard(original):
        def connect(sock, address):
            if isinstance(address, tuple) and address:
                try:
                    if ipaddress.ip_address(address[0]).is_loopback:
                        return original(sock, address)
                except ValueError:
                    pass
            pytest.fail("Unexpected external socket connection in synthetic OCR purpose tests")
        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(original_connect_ex))


@pytest.fixture
def ocr_run(db, binding, network, sample_pdf):
    original, selected = source(db, sample_pdf, binding)

    def run(purpose="rich_layout_recognition", *, response_purpose=None, finish="stop"):
        expected = purpose or "text_recognition"
        arguments = {"source_revision_id": original["revision_id"]}
        if purpose is not None:
            arguments["purpose"] = purpose
        raw = ocr_response()
        raw["purpose"] = response_purpose or expected
        raw["pages"][0]["text"] = (
            "<｜ref｜>" + OCR_BODY + "<｜/ref｜><｜box｜>[[10,20,200,100]]<｜/box｜>"
        )
        raw["pages"][0]["finish_reason"] = finish
        network[1][0] = lambda request: httpx.Response(200, json=raw)
        result, _ = execute(db, selected, "deepseek_ocr", arguments,
            operation="d05-ocr-purpose-author")
        assert len(network[0]) == 1
        request = network[0][0]
        assert b'name="purpose"' in request.content
        assert expected.encode() in request.content
        assert sample_pdf.read_bytes() in request.content
        return result, arguments, original

    return run


def projected(result, arguments, *, history_mutation=None, excluded=()):
    history = messages("deepseek_ocr", result, arguments)
    if history_mutation is not None:
        history_mutation(history)
    receipts, _ = process_receipts(history, excluded)
    return receipts


@pytest.mark.parametrize("purpose", ["text_recognition", "rich_layout_recognition", None],
    ids=["text", "layout", "default-text"])
def test_successful_adapter_purpose_survives_bounded_projection(ocr_run, purpose):
    result, arguments, original = ocr_run(purpose)
    expected = purpose or "text_recognition"
    assert result["status"] == "ok" and result["purpose"] == expected
    receipts = projected(result, arguments)
    assert len(receipts) == 1
    receipt = receipts[0]
    contract = json.loads(receipt["content"])
    assert contract["purpose"] == expected
    assert contract["original_revision_id"] == original["revision_id"]
    assert contract["original_sha256"] == original["raw_sha256"]
    assert receipt["sha256"] == hashlib.sha256(receipt["content"].encode()).hexdigest()


def test_projected_purpose_is_an_actual_bounded_quote_not_whole_text_proof(ocr_run):
    result, arguments, _ = ocr_run()
    receipts = projected(result, arguments)
    assert len(receipts) == 1
    verdict = {"index": 0, "requires_citation": False, "reason": "Bound completed OCR invocation parameter",
        "kind": "process", "process_scope": "bounded", "supports": [{
            "receipt_id": receipts[0]["id"], "quote": '"purpose": "rich_layout_recognition"'}]}
    assert validation_failure(verdict, receipts, bounded_proof=True) is None
    verdict["process_scope"] = "whole_extracted_text"
    assert validation_failure(verdict, receipts, bounded_proof=True) == "whole_scope"


def test_purpose_never_promotes_body_geometry_accuracy_or_understanding(ocr_run):
    result, arguments, _ = ocr_run()
    result["untrusted_body"] = OCR_BODY * 2000
    result["provider_promise"] = "Everything was read and perfectly understood"
    receipts = projected(result, arguments)
    assert len(receipts) == 1
    contract = json.loads(receipts[0]["content"])
    assert OCR_BODY not in receipts[0]["content"]
    assert "perfectly understood" not in receipts[0]["content"]
    assert contract["coverage_scope"] == "returned_ocr_text_only"
    for field in ("original_page_coverage_verified", "source_accuracy_verified",
            "model_understanding_verified", "whole_extracted_text_returned",
            "coordinates_mapped_to_original", "ocr_text_accuracy_verified"):
        assert contract[field] is False
    assert receipts[0]["scope"] == "process_metadata_only"
    assert receipts[0]["whole_extracted_text_returned"] is False
    assert "pages" not in contract and "coordinates" not in contract and "evidence" not in contract


@pytest.mark.parametrize("kind", ["source", "derived", "mixed", "uncertain"])
def test_purpose_cannot_waive_source_or_mixed_claims(ocr_run, kind):
    result, arguments, _ = ocr_run()
    receipts = projected(result, arguments)
    verdict = {"index": 0, "requires_citation": False, "reason": "Source content still needs source evidence",
        "kind": kind, "process_scope": "bounded", "supports": [{
            "receipt_id": receipts[0]["id"], "quote": '"response_complete": true'}]}
    assert validation_failure(verdict, receipts, bounded_proof=True) == "source_requirement"


@pytest.mark.parametrize("response_purpose,finish", [
    ("text_recognition", "stop"), ("rich_layout_recognition", "length")],
    ids=["provider-purpose-mismatch", "actual-partial-response"])
def test_actual_adapter_rejects_mismatched_or_partial_success_proof(ocr_run, response_purpose, finish):
    result, arguments, _ = ocr_run(response_purpose=response_purpose, finish=finish)
    assert result.get("status") != "ok"
    assert projected(result, arguments) == []


@pytest.mark.parametrize("mutation", [
    "purpose-mismatch", "purpose-missing", "purpose-invalid", "purpose-unhashable",
    "arguments-mismatch", "arguments-invalid", "partial", "truncated", "response-incomplete",
    "execution-unknown", "service-error", "missing-page", "duplicate-page", "bad-call-id",
    "original-revision-mismatch", "original-hash-invalid", "source-hash-mismatch", "missing-source",
])
def test_invalid_success_contract_never_projects_purpose(ocr_run, mutation):
    result, arguments, _ = ocr_run()
    result, arguments = deepcopy(result), deepcopy(arguments)
    if mutation == "purpose-mismatch": result["purpose"] = "text_recognition"
    elif mutation == "purpose-missing": result.pop("purpose")
    elif mutation == "purpose-invalid": result["purpose"] = "understand_pixels"
    elif mutation == "purpose-unhashable": result["purpose"] = ["rich_layout_recognition"]
    elif mutation == "arguments-mismatch": arguments["purpose"] = "text_recognition"
    elif mutation == "arguments-invalid": arguments["purpose"] = "understand_pixels"
    elif mutation == "partial": result["status"] = "partial"
    elif mutation == "truncated": result["truncated"] = True
    elif mutation == "response-incomplete": result["response_complete"] = False
    elif mutation == "execution-unknown": result["execution_unknown"] = True
    elif mutation == "service-error": result["error"] = "synthetic failure"
    elif mutation == "missing-page": result["page_count"] += 1
    elif mutation == "duplicate-page":
        result["page_count"] = 2
        result["page_statuses"] *= 2
    elif mutation == "bad-call-id": result["call_id"] = "model-self-report"
    elif mutation == "original-revision-mismatch": result["original_revision_id"] = "e" * 32
    elif mutation == "original-hash-invalid": result["original_sha256"] = "not-a-content-hash"
    elif mutation == "source-hash-mismatch":
        wrong = "f" * 64 if result["original_sha256"] != "f" * 64 else "e" * 64
        result["sources"][0]["raw_sha256"] = wrong
    elif mutation == "missing-source": result["sources"] = []
    else: raise AssertionError(mutation)
    assert projected(result, arguments) == []


@pytest.mark.parametrize("mutation", ["unpaired", "duplicate-call", "duplicate-result", "missing-result", "excluded-result"])
def test_only_unique_actual_paired_result_may_prove_purpose(ocr_run, mutation):
    result, arguments, _ = ocr_run()

    def alter(history):
        if mutation == "unpaired": history[2]["tool_call_id"] = "not-the-actual-call"
        elif mutation == "duplicate-call": history.insert(2, deepcopy(history[1]))
        elif mutation == "duplicate-result": history.append(deepcopy(history[2]))
        elif mutation == "missing-result": history.pop()

    assert projected(result, arguments, history_mutation=alter,
        excluded=(2,) if mutation == "excluded-result" else ()) == []
