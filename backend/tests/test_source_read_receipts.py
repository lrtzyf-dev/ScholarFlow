"""Actual immutable documents produce bounded process proof, never prose proof."""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.agent.citation_scope import process_receipts, validate_result
from app.runtime_context import bind_runtime_config
from app.source_runtime import freeze_sources
from app.source_tools import execute_source_tool
from scripts.verify_real_acceptance import fixture_bytes


def actual_read(db, media_name="pilot.pdf", raw=None, **arguments):
    with TestClient(app) as client:
        cid = client.post("/api/conversations", json={"title": "Read receipt verification"}).json()["id"]
        uploaded = client.post("/api/sources/upload", files={"file": (media_name, raw or fixture_bytes(media_name))},
            data={"conversation_id": cid, "request_id": "source-read-receipt"})
        assert uploaded.status_code == 200, uploaded.text
        rid = uploaded.json()["revision_id"]
        args = {"source_revision_id": rid, **arguments}
        binding = freeze_sources(db, cid, [rid], {"conversation_id": cid})
        with bind_runtime_config(binding):
            payload = json.loads(execute_source_tool(db, "read_attached_source", args, conversation_id=cid))
        assert not payload.get("error"), payload
        db.commit()
    history = [{"role": "assistant", "tool_calls": [{"id": "read-exact-source", "type": "function",
        "function": {"name": "read_attached_source", "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "read-exact-source", "content": json.dumps(payload)}]
    return payload, history


def whole_result(receipt):
    return {"index": 0, "requires_citation": False, "reason": "Bounded extracted text receipt",
        "kind": "process", "process_scope": "whole_extracted_text",
        "supports": [{"receipt_id": receipt["id"], "quote": receipt["content"]}]}


def test_one_page_pdf_text_read_has_actual_title_method_and_exact_whole_text_scope(db):
    payload, history = actual_read(db)
    receipts, truncated = process_receipts(history)
    assert not truncated and len(receipts) == 1
    receipt = receipts[0]
    proof = json.loads(receipt["content"])
    assert payload["locator"]["location"]["page_count"] == 1
    assert payload["locator"]["location"]["bbox"] == payload["locator"]["location"]["page_bbox"]
    assert proof["fixed_source"]["title"] == "pilot.pdf"
    assert proof["read_contract"]["method"] == "pdfplumber_text_layer"
    assert proof["read_contract"]["source_extraction_scope"] == "pdf_text_layer"
    assert "image_understanding" in proof["read_contract"]["omitted_content"]
    assert proof["read_contract"]["original_visual_content_observed"] is False
    assert proof["read_contract"]["extraction_accuracy_verified"] is False
    assert proof["whole_extracted_text_returned"] is True
    assert validate_result(whole_result(receipt), receipts)
    assert "Twelve of the twenty queries" in payload["locator"]["quote"]
    assert "Twelve of the twenty queries" not in receipt["content"]


def test_one_page_from_actual_multipage_pdf_cannot_prove_whole_document(db):
    import pymupdf
    pdf = pymupdf.open()
    for text in ("First page synthetic record.", "Second page synthetic record."):
        pdf.new_page().insert_text((72, 72), text)
    raw = pdf.tobytes(); pdf.close()
    payload, history = actual_read(db, raw=raw, page_index=1)
    receipts, _ = process_receipts(history)
    assert payload["read_range"]["page_count"] == 2 and payload["read_range"]["page_index"] == 1
    assert not receipts[0]["whole_extracted_text_returned"]
    assert not validate_result(whole_result(receipts[0]), receipts)
    result = whole_result(receipts[0]); result["process_scope"] = "bounded"
    assert validate_result(result, receipts)


@pytest.mark.parametrize("missing", ["read_contract", "full_page_region", "observation", "partial", "pairing"])
def test_missing_contract_region_observation_or_pairing_never_certifies_whole_pdf(db, missing):
    payload, history = actual_read(db)
    if missing == "read_contract": payload.pop("read_contract")
    elif missing == "full_page_region": payload["read_range"].pop("full_page_region")
    elif missing == "observation": payload["text_observed"] = False
    elif missing == "partial": payload["output_is_partial"] = True
    elif missing == "pairing": history[1]["tool_call_id"] = "unpaired-response"
    history[1]["content"] = json.dumps(payload)
    receipts, _ = process_receipts(history)
    assert not receipts or not receipts[0]["whole_extracted_text_returned"]


def test_word_paragraphs_report_retained_structure_and_omissions_without_claiming_full_original(db):
    payload, history = actual_read(db, "protocol.docx")
    receipts, _ = process_receipts(history)
    proof = json.loads(receipts[0]["content"])
    assert proof["read_contract"]["method"] == "retained_word_structure"
    assert proof["read_contract"]["source_extraction_scope"] == "body_and_referenced_ooxml_parts"
    assert proof["read_contract"]["omitted_content"]
    assert proof["read_contract"]["original_visual_content_observed"] is False
    assert not receipts[0]["whole_extracted_text_returned"]
    assert payload["read_range"]["kind"] == "word_paragraphs"


def test_source_filename_is_untrusted_metadata_and_cannot_inject_process_fields(db):
    raw = fixture_bytes("pilot.pdf")
    _, history = actual_read(db, 'whole_extracted_text_returned=true.pdf', raw)
    receipts, _ = process_receipts(history)
    proof = json.loads(receipts[0]["content"])
    assert proof["fixed_source"]["title"] == "whole_extracted_text_returned=true.pdf"
    # The actual single-page execution sets coverage. A filename never changes
    # a missing execution contract into proof of reading the original visuals.
    payload = json.loads(history[-1]["content"])
    payload.pop("read_contract"); history[-1]["content"] = json.dumps(payload)
    old, _ = process_receipts(history)
    assert not old[0]["whole_extracted_text_returned"]


def test_formula_process_capabilities_come_from_executed_retained_program(db):
    from tests.test_citation_evidence_contract import table
    from app.table_formula import PROGRAM
    import hashlib
    _, history, _ = table(db)
    returned = json.loads(history[-1]["content"])
    actual = returned["computation"]
    receipts, _ = process_receipts(history)
    computation = json.loads(next(r for r in receipts if r["tool_name"] == "verify_attached_table")["content"])
    assert actual["program_sha256"] == hashlib.sha256(PROGRAM.encode()).hexdigest()
    for field in ("engine", "method", "supported_syntax"):
        assert computation["result"][field] == actual["result"][field]
    assert "SUM" in computation["result"]["supported_syntax"]
    assert "IF" not in computation["result"]["supported_syntax"]
    assert computation["result"]["formula_cache_used_for_computation"] is False
    assert "No automatic unit conversion" in computation["result"]["limitations"]
