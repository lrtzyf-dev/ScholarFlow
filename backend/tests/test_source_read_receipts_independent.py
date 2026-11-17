"""Actual raster/text mixtures never turn text extraction into visual proof."""
import json

import pymupdf
from fastapi.testclient import TestClient

from app.agent.citation_scope import process_receipts
from app.main import app
from app.runtime_context import bind_runtime_config
from app.source_runtime import freeze_sources
from app.source_tools import execute_source_tool


def raster_pdf(*, with_text):
    # Make a real raster image containing words that text extraction cannot see.
    image_document = pymupdf.open()
    image_document.new_page(width=300, height=100).insert_text((20, 50), "RASTER-ONLY: 999 participants")
    raster = image_document[0].get_pixmap().tobytes("png")
    image_document.close()
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(50, 100, 350, 200), stream=raster)
    if with_text:
        page.insert_text((50, 50), "Text layer: fabricated mixed-content acceptance record.")
    result = document.tobytes()
    document.close()
    return result


def observed_read(db, raw):
    with TestClient(app) as client:
        cid = client.post("/api/conversations", json={"title": "Raster boundary"}).json()["id"]
        upload = client.post("/api/sources/upload", files={"file": ("mixed.pdf", raw)},
            data={"conversation_id": cid, "request_id": "independent-raster-source"})
        assert upload.status_code == 200, upload.text
        rid = upload.json()["revision_id"]
        arguments = {"source_revision_id": rid, "page_index": 0}
        with bind_runtime_config(freeze_sources(db, cid, [rid], {"conversation_id": cid})):
            response = json.loads(execute_source_tool(db, "read_attached_source", arguments, conversation_id=cid))
        assert "error" not in response, response
        db.commit()
    messages = [{"role": "assistant", "tool_calls": [{"id": "actual-pdf-read", "type": "function",
        "function": {"name": "read_attached_source", "arguments": json.dumps(arguments)}}]},
        {"role": "tool", "tool_call_id": "actual-pdf-read", "content": json.dumps(response)}]
    return response, process_receipts(messages)


def test_real_scanned_only_page_never_provides_a_whole_read_receipt(db):
    response, (receipts, _) = observed_read(db, raster_pdf(with_text=False))
    assert response["locator"]["location"]["page_count"] == 1
    assert response["locator"]["quote"] == ""
    assert response["text_observed"] is False
    assert response["evidence"] == []
    assert response["output_is_partial"] is True
    assert receipts == []


def test_real_mixed_page_proves_only_text_layer_and_keeps_visual_limitations(db):
    response, (receipts, truncated) = observed_read(db, raster_pdf(with_text=True))
    assert not truncated and len(receipts) == 1
    receipt = json.loads(receipts[0]["content"])
    assert "Text layer:" in response["locator"]["quote"]
    assert "999 participants" not in response["locator"]["quote"]
    assert receipt["whole_extracted_text_returned"] is True
    assert receipt["coverage_scope"] == "extracted_text_only"
    assert receipt["read_contract"]["original_visual_content_observed"] is False
    assert receipt["read_contract"]["model_understanding_verified"] is False
    assert "image_understanding" in receipt["read_contract"]["omitted_content"]
    # No source prose is promoted into authoritative process metadata, including
    # the image's number and the visible text layer that actually was returned.
    assert "999 participants" not in receipts[0]["content"]
    assert "fabricated mixed-content" not in receipts[0]["content"]
