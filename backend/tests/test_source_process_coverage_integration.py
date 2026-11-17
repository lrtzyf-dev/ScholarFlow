"""Actual document reads through the production process-receipt projection."""
from io import BytesIO
import json

from fastapi.testclient import TestClient

from app.agent.citation_scope import process_receipts, validate_result
from app.main import app
from app.runtime_context import bind_runtime_config
from app.source_runtime import freeze_sources
from app.source_tools import execute_source_tool


def document_history(db, filename, raw, select):
    with TestClient(app) as client:
        cid = client.post("/api/conversations", json={"title": "Integrated extraction coverage"}).json()["id"]
        response = client.post("/api/sources/upload", files={"file": (filename, raw)},
            data={"conversation_id": cid, "request_id": "integrated-coverage-source"})
        assert response.status_code == 200, response.text
        rid = response.json()["revision_id"]
        binding = freeze_sources(db, cid, [rid], {"conversation_id": cid})
        history = []
        with bind_runtime_config(binding):
            for index, selection in enumerate(select(binding["source_bindings"][0])):
                arguments = {"source_revision_id": rid, **selection}
                content = execute_source_tool(db, "read_attached_source", arguments, conversation_id=cid)
                assert not json.loads(content).get("error"), content
                history.extend([{"role": "assistant", "tool_calls": [{"id": f"read-{index}", "type": "function",
                    "function": {"name": "read_attached_source", "arguments": json.dumps(arguments)}}]},
                    {"role": "tool", "tool_call_id": f"read-{index}", "content": content}])
        db.commit()
    return history


def aggregate(history):
    projected, _ = process_receipts(history)
    aggregate = [item for item in projected if item["tool_name"] == "read_attached_source_coverage"]
    assert len(aggregate) == 1
    result = aggregate[0]
    payload = json.loads(result["content"])
    assert payload["version"] == "source_process_coverage_v1"
    assert payload["source_receipt_ids"] == [item["id"] for item in projected if item["tool_name"] == "read_attached_source"]
    proof = {"index": 0, "requires_citation": False, "kind": "process", "reason": "Only actual extracted text coverage",
        "process_scope": "whole_extracted_text", "supports": [{"receipt_id": result["id"], "quote": result["content"]}]}
    return projected, result, payload, proof


def test_pdf_full_pipeline_exposes_complete_aggregate_and_missing_page_cannot_certify_full_text(db):
    import pymupdf
    document = pymupdf.open()
    for number in range(3):
        document.new_page().insert_text((72, 72), f"Synthetic page {number + 1}; no scientific claim.")
    raw = document.tobytes(); document.close()
    history = document_history(db, "three-pages.pdf", raw,
        lambda source: [{"page_index": page} for page in (0, 2, 1)])
    receipts, result, payload, proof = aggregate(history)
    assert result["whole_extracted_text_returned"] and payload["read_range"]["observed_intervals"] == [[1, 3]]
    assert payload["read_contract"]["method"] == "pdfplumber_text_layer"
    assert payload["original_visual_content_observed"] is False
    assert "image_understanding" in payload["read_contract"]["omitted_content"]
    assert validate_result(proof, receipts)
    incomplete, missing, payload, proof = aggregate(history[:-2])
    assert not missing["whole_extracted_text_returned"]
    assert payload["read_range"]["observed_intervals"] == [[1, 1], [3, 3]]
    assert not validate_result(proof, incomplete)


def test_word_full_pipeline_requires_real_footer_part_and_retains_unrendered_limitations(db):
    from docx import Document
    document = Document()
    document.add_paragraph("Synthetic study body.")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Synthetic table field."
    document.sections[0].footer.paragraphs[0].text = "Footer limitation must also be read."
    stream = BytesIO(); document.save(stream)
    def select(source):
        assert source["word_block_directory_next_offset"] is None
        assert source["word_blocks"][-1]["section_kind"] == "footer"
        return [{"paragraph_start": 1, "paragraph_end": source["paragraph_count"]},
            *[{"block_id": part["block_id"]} for part in source["word_blocks"]]]
    history = document_history(db, "footer-proof.docx", stream.getvalue(), select)
    receipts, result, payload, proof = aggregate(history)
    assert result["whole_extracted_text_returned"]
    assert payload["fixed_source"]["extraction_fingerprint"]
    assert payload["read_range"]["rendered_page_available"] is False
    assert payload["read_contract"]["omitted_content"] and payload["model_understanding_verified"] is False
    assert validate_result(proof, receipts)
    incomplete, missing, payload, proof = aggregate(history[:-2])
    assert not missing["whole_extracted_text_returned"]
    assert payload["read_range"]["nonempty_word_block_count_observed"] < payload["read_range"]["total_nonempty_word_blocks"]
    assert not validate_result(proof, incomplete)
