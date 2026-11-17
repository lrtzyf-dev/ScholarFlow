"""Check a restored local-read body against authoritative fixed-source records.

A checkpoint result hash is only a corruption hint, not source correspondence.
Valid hits keep their expensive PDF parse; the persisted server-derived locator,
fixed extraction and every public result field must still agree independently.
Unknown/directory-only representations take the ordinary producer path.
"""
from __future__ import annotations

import json

from . import source_store as store
from .source_documents import read_document
from .source_runtime import capture_bindings, conversation_scope


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def matches_saved_read(db, result, arguments, *, conversation_id):
    """No cache field supplies an authoritative quote, range, or read contract."""
    try:
        payload = json.loads(result)
        if not isinstance(payload, dict) or not isinstance(payload.get("locator"), dict):
            return False
        scope = conversation_scope(db, conversation_id)
        rid = arguments.get("source_revision_id")
        record = store.read_revision(db, rid, scope=scope)
        available = capture_bindings(db, conversation_id, [rid])[0]
        locator = store.read_locator(db, payload["locator"].get("locator_id"), scope=scope)
        if locator["revision_id"] != rid or locator["source_id"] != record["source_id"]:
            return False
        location, table_context, adjusted = locator["location"], None, False
        if record["media_type"] == "application/pdf":
            if (set(arguments) - {"source_revision_id", "page_index"} or locator["kind"] != "pdf_page"
                    or type(arguments.get("page_index", 0)) is not int
                    or arguments.get("page_index", 0) != location["page_index"]
                    or location["raw_sha256"] != record["raw_sha256"]
                    or location["bbox"] != location["page_bbox"]):
                return False
            extent = {"kind": "pdf_page", "page_index": location["page_index"], "page_count": location["page_count"],
                "next_page_index": location["page_index"] + 1 if location["page_index"] + 1 < location["page_count"] else None,
                "text_observed": location["text_observed"], "full_page_region": True}
            method = "pdfplumber_text_layer"
        elif record["media_type"].startswith("text/") and record["media_type"] != "text/csv":
            if set(arguments) - {"source_revision_id", "line_start", "line_end"} or locator["kind"] != "text_lines":
                return False
            lines = (record["text"] or "").splitlines(keepends=True)
            start = arguments.get("line_start", 1)
            if type(start) is not int:
                return False
            end = arguments.get("line_end", min(len(lines), start + 79))
            if type(end) is not int:
                return False
            if end > len(lines):
                end, adjusted = len(lines), True
            if not 1 <= start <= end <= len(lines) or end - start >= 200:
                return False
            expected_location = {"line_start": start, "line_end": end, "line_index_base": 1,
                                 "text_sha256": record["text_sha256"]}
            if location != expected_location or locator["quote"] != "".join(lines[start - 1:end]):
                return False
            extent = {"kind": "text_lines", "line_start": start, "line_end": end, "total_lines": len(lines),
                      "next_line": end + 1 if end < len(lines) else None}
            method = "retained_decoded_text_lines"
        else:
            doc = read_document(db, rid, scope=scope)
            if (location.get("raw_sha256") != doc["raw_sha256"]
                    or location.get("text_sha256") != doc["text_sha256"]
                    or location.get("extraction_fingerprint") != doc["fingerprint"]):
                return False
            if doc["format"] == "word":
                method = "retained_word_structure"
                parts = [b for b in doc["blocks"] if b.get("kind") == "word_part"]
                paragraphs = [b for b in doc["blocks"] if "paragraph_index" in b]
                total = len(paragraphs)
                totals = {"total_paragraphs": total, "total_word_blocks": sum(bool(b["text"].strip()) for b in parts)}
                if "block_id" in arguments:
                    if (set(arguments) != {"source_revision_id", "block_id"} or locator["kind"] != "word_block"
                            or location.get("block_id") != arguments["block_id"]):
                        return False
                    part = next((b for b in parts if b.get("block_id") == arguments["block_id"]), None)
                    if part is None or part["text"] != locator["quote"]:
                        return False
                    extent = {"kind": "word_block", "block_id": arguments["block_id"], **totals,
                              "text_observed": bool(locator["quote"].strip())}
                else:
                    if (set(arguments) - {"source_revision_id", "paragraph_start", "paragraph_end"}
                            or locator["kind"] != "word_paragraphs"):
                        return False
                    start = arguments.get("paragraph_start", 1)
                    if type(start) is not int:
                        return False
                    end = arguments.get("paragraph_end", min(total, start + 39))
                    if type(end) is not int:
                        return False
                    if end > total:
                        end, adjusted = total, True
                    if (not 1 <= start <= end <= total or end - start >= 200
                            or location.get("paragraph_start") != start or location.get("paragraph_end") != end
                            or locator["quote"] != "\n".join(b["text"] for b in paragraphs[start - 1:end])):
                        return False
                    extent = {"kind": "word_paragraphs", "paragraph_start": start, "paragraph_end": end,
                              **totals, "next_paragraph": end + 1 if end < total else None}
            elif doc["format"] in ("excel", "csv"):
                if (set(arguments) - {"source_revision_id", "sheet", "cell_range"}
                        or locator["kind"] != "sheet_cells" or not doc["sheets"]):
                    return False
                sheet = arguments.get("sheet", doc["sheets"][0]["name"])
                cell_range = arguments.get("cell_range")
                if (not isinstance(cell_range, str) or location.get("sheet") != sheet
                        or location.get("cell_range") != cell_range.upper()):
                    return False
                from openpyxl.utils.cell import range_boundaries
                left, top, right, bottom = range_boundaries(cell_range.upper())
                if ((right - left + 1) * (bottom - top + 1) > 200 or left > right or top > bottom):
                    return False
                selected = [cell for block in doc["blocks"] if block.get("sheet") == sheet and top <= block["row_start"] <= bottom
                            for cell in block["cells"] if left <= range_boundaries(cell["cell"])[0] <= right]
                meta = next(s for s in doc["sheets"] if s["name"] == sheet)
                table_context = {"cells": selected, "table_metadata": available.get("table_metadata"),
                    "selected_sheet": {**{key: meta[key] for key in ("name", "state", "row_count", "column_count", "formula_count") if key in meta},
                        "selected_column_headers": meta.get("headers", {}).get("columns", [])[left - 1:right]},
                    "formula_cache_is_unverified": True, "formula_recalculation_performed": False,
                    "source_values_are_not_computation_results": True}
                extent = {"kind": "sheet_cells", **location, "sheets": available["sheets"]}
                method = "retained_table_cells"
            else:
                return False
        public_locator = {key: value for key, value in locator.items() if key not in {"source_id", "raw_sha256"}}
        evidence = {"source_id": record["source_id"], "revision_id": rid, "locator_id": locator["locator_id"],
            "source_title": record["title"], "source_kind": record["kind"], "source_location": location,
            "paper_id": "", "paper_title": record["title"], "chunk_id": "", "title": record["title"],
            "page": location.get("page_number", 0), "bbox": location.get("bbox"),
            "quote": locator["quote"], "score": 1.0, "trust": "untrusted_source_data"}
        expected = {"source": {key: value for key, value in record.items() if key not in {"text", "raw_bytes"}},
            "locator": public_locator, "evidence": [evidence] if locator["quote"].strip() else [],
            "read_range": extent, "range_adjusted_to_actual_end": adjusted,
            "read_contract": {"version": "source_read_execution_v1", "method": method,
                "selected_text_returned_in_full": True, "original_visual_content_observed": False,
                "source_extraction_scope": available.get("extraction_scope"),
                "omitted_content": available.get("omitted_content", []),
                "extraction_accuracy_verified": False, "model_understanding_verified": False},
            **({"table_context": table_context} if table_context is not None else {}),
            "coverage_scope": "extracted_text_only", "source_directory": available,
            "output_is_partial": record["media_type"] == "application/pdf" and not bool(locator["quote"].strip()),
            "text_observed": bool(locator["quote"].strip()), "model_understanding_verified": False}
        return _canonical(payload) == _canonical(expected)
    except (store.SourceError, KeyError, TypeError, ValueError, StopIteration, UnicodeError, OverflowError):
        return False
