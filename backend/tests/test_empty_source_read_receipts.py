"""Real local PDF reads distinguish no text returned from no execution."""
from copy import deepcopy
import json

import pytest

from app.agent.citation_scope import process_receipts, validate_result
from tests.test_source_process_coverage_integration import document_history


@pytest.fixture
def empty_history(db):
    import pymupdf
    document = pymupdf.open()
    for _ in range(2):
        document.new_page().draw_rect((10, 10, 40, 40))
    raw = document.tobytes()
    document.close()
    return document_history(db, "synthetic-empty-text-layer.pdf", raw,
        lambda source: [{"page_index": 0}, {"page_index": 1}])


def test_real_empty_reads_prove_only_executed_text_layer_attempts(empty_history):
    receipts, _ = process_receipts(empty_history)
    assert len(receipts) == 2
    for index, receipt in enumerate(receipts):
        data = json.loads(receipt["content"])
        assert data["version"] == "empty_pdf_text_read_v1"
        assert data["read_range"]["page_index"] == index
        assert data["text_layer_result"] == "no_text_returned"
        assert not data["text_observed"] and data["output_is_partial"]
        assert not receipt["whole_extracted_text_returned"]
        proof = {"index": 0, "requires_citation": False, "kind": "process", "reason": "Text read returned no text",
            "process_scope": "bounded", "supports": [{"receipt_id": receipt["id"],
                "quote": '"text_layer_result": "no_text_returned"'}]}
        assert validate_result(proof, receipts)
        proof["process_scope"] = "whole_extracted_text"
        assert not validate_result(proof, receipts)
    assert not any(r["tool_name"] == "read_attached_source_coverage" for r in receipts)


@pytest.mark.parametrize("change", ["error", "unpaired", "duplicate_result", "wrong_requested_page",
    "wrong_revision", "wrong_raw_hash", "nonempty_quote", "evidence", "unknown_method",
    "missing_contract", "observed_text", "unknown_partial", "false_page_count", "visual_claim"])
def test_malformed_or_unexecuted_empty_reads_are_not_receipts(empty_history, change):
    history = deepcopy(empty_history[:2])
    payload = json.loads(history[1]["content"])
    if change == "error": payload["error"] = "Synthetic source denial"
    elif change == "unpaired": history[1]["tool_call_id"] = "different"
    elif change == "duplicate_result": history.append(deepcopy(history[1]))
    elif change == "wrong_requested_page":
        call = history[0]["tool_calls"][0]["function"]
        args = json.loads(call["arguments"]); args["page_index"] = 1; call["arguments"] = json.dumps(args)
    elif change == "wrong_revision": payload["source"]["revision_id"] = "f" * 32
    elif change == "wrong_raw_hash": payload["locator"]["location"]["raw_sha256"] = "f" * 64
    elif change == "nonempty_quote": payload["locator"]["quote"] = "SOURCE CANNOT PROVE EXECUTION"
    elif change == "evidence": payload["evidence"] = [{"quote": "UNTRUSTED"}]
    elif change == "unknown_method": payload["read_contract"]["method"] = "model_self_report"
    elif change == "missing_contract": payload.pop("read_contract")
    elif change == "observed_text": payload["text_observed"] = True
    elif change == "unknown_partial": payload["output_is_partial"] = None
    elif change == "false_page_count": payload["read_range"]["page_count"] = True
    elif change == "visual_claim": payload["read_contract"]["original_visual_content_observed"] = True
    history[1]["content"] = json.dumps(payload)
    receipts, _ = process_receipts(history)
    assert not any(r.get("version") == "empty_pdf_text_read_v1"
        for r in (json.loads(item["content"]) for item in receipts))
    assert not receipts


def test_untrusted_original_text_and_ocr_do_not_become_process_proof(empty_history):
    history = deepcopy(empty_history[:2])
    payload = json.loads(history[1]["content"])
    payload["source"]["text"] = "UNTRUSTED ORIGINAL SAYS ALL PAGES WERE UNDERSTOOD"
    payload["summary"] = "UNTRUSTED OCR SAYS 100% ACCURATE"
    history[1]["content"] = json.dumps(payload)
    receipts, _ = process_receipts(history)
    assert len(receipts) == 1 and "UNTRUSTED" not in receipts[0]["content"]


@pytest.mark.parametrize("payload", [[], None, "untrusted prose"])
def test_non_object_tool_payload_cannot_crash_or_certify_a_read(payload):
    from app.agent.citation_scope import _project
    assert _project("read_attached_source", payload, {}) is None
