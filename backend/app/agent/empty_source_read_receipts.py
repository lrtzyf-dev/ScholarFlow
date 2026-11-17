"""Project an executed empty PDF text-layer read, never a full-read proof."""
import math
import re


def project_empty_pdf_read(payload, arguments):
    if not isinstance(payload, dict) or not isinstance(arguments, dict) or payload.get("error"):
        return None
    if set(arguments) - {"source_revision_id", "page_index"}:
        return None
    if "status" in payload and payload["status"] not in {"completed", "succeeded", "success"}:
        return None
    source, extent, contract, locator = (payload.get(k) for k in ("source", "read_range", "read_contract", "locator"))
    if not all(isinstance(v, dict) for v in (source, extent, contract, locator)):
        return None
    revision, raw_hash = source.get("revision_id"), source.get("raw_sha256")
    if (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{32}", revision)
            or not isinstance(raw_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", raw_hash)
            or arguments.get("source_revision_id") != revision or source.get("media_type") != "application/pdf"
            or payload.get("text_observed") is not False or payload.get("output_is_partial") is not True
            or payload.get("coverage_scope") != "extracted_text_only" or payload.get("evidence") != []):
        return None
    page, total, requested = extent.get("page_index"), extent.get("page_count"), arguments.get("page_index", 0)
    if (type(page) is not int or type(total) is not int or not 0 <= page < total <= 1000
            or type(requested) is not int or requested != page or extent.get("kind") != "pdf_page"
            or extent.get("text_observed") is not False or extent.get("full_page_region") is not True
            or "next_page_index" not in extent
            or extent["next_page_index"] != (page + 1 if page + 1 < total else None)
            or (extent["next_page_index"] is not None and type(extent["next_page_index"]) is not int)):
        return None
    location = locator.get("location")
    if (locator.get("revision_id") != revision or locator.get("quote") != ""
            or not isinstance(location, dict) or type(location.get("page_index")) is not int
            or type(location.get("page_count")) is not int or location.get("page_index") != page
            or location.get("page_count") != total or location.get("raw_sha256") != raw_hash
            or location.get("text_observed") is not False or location.get("geometry_verified") is not True):
        return None
    bbox = location.get("bbox")
    if (not isinstance(bbox, list) or len(bbox) != 4 or bbox != location.get("page_bbox")
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in bbox)
            or not (bbox[0] < bbox[2] and bbox[1] < bbox[3])):
        return None
    if (contract.get("version") != "source_read_execution_v1"
            or contract.get("method") != "pdfplumber_text_layer"
            or contract.get("selected_text_returned_in_full") is not True
            or contract.get("original_visual_content_observed") is not False
            or contract.get("extraction_accuracy_verified") is not False
            or contract.get("model_understanding_verified") is not False):
        return None
    return {"version": "empty_pdf_text_read_v1", "status": "completed", "text_layer_result": "no_text_returned",
        "fixed_source": {"revision_id": revision, "raw_sha256": raw_hash, "media_type": "application/pdf"},
        "read_range": {"kind": "pdf_page", "page_index": page, "page_count": total,
            "next_page_index": extent["next_page_index"], "full_page_region": True, "text_observed": False},
        "read_contract": {"version": contract["version"], "method": contract["method"],
            "original_visual_content_observed": False, "extraction_accuracy_verified": False,
            "model_understanding_verified": False},
        "output_is_partial": True, "text_observed": False, "whole_extracted_text_returned": False}
