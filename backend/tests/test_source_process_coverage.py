"""Coverage proofs use fixed execution ranges, never prose or bounding boxes."""
from copy import deepcopy
import hashlib
from io import BytesIO
import json

import pytest

from app.agent.source_process_coverage import MAX_RECEIPTS, _covered_area, aggregate_process_receipts


REVISION = "a" * 32
RAW_HASH = "b" * 64
EXTRACTION = "c" * 64


def receipt(index, extent, *, method="pdfplumber_text_layer", revision=REVISION, raw_hash=RAW_HASH,
            extraction=EXTRACTION):
    source = {"title": "Fixed document", "revision_id": revision, "raw_sha256": raw_hash,
              "media_type": "application/pdf", "extraction_fingerprint": extraction}
    payload = {"fixed_source": source, "read_range": extent, "coverage_scope": "extracted_text_only",
        "output_is_partial": False, "text_observed": True, "model_understanding_verified": False,
        "read_contract": {"version": "source_read_execution_v1", "method": method,
            "selected_text_returned_in_full": True, "original_visual_content_observed": False,
            "source_extraction_scope": "retained_extracted_text", "omitted_content": ["image_understanding"],
            "extraction_accuracy_verified": False, "model_understanding_verified": False}}
    return wrap(index, payload)


def wrap(index, payload):
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return {"id": f"context:{index}", "tool_name": "read_attached_source", "content": content,
        "sha256": hashlib.sha256(content.encode()).hexdigest(), "scope": "process_metadata_only",
        "whole_extracted_text_returned": False}


def mutate(item, change):
    payload = json.loads(item["content"])
    change(payload)
    return wrap(item["id"].split(":")[-1], payload)


def pdf(index, total=3):
    return {"kind": "pdf_page", "page_index": index, "page_count": total, "text_observed": True,
            "full_page_region": True, "next_page_index": index + 1 if index + 1 < total else None}


def derived(items):
    extra = aggregate_process_receipts(items)
    assert len(extra) == 1
    return extra[0], json.loads(extra[0]["content"])


def test_pdf_overlapping_out_of_order_reads_cover_all_pages_without_mutating_originals():
    items = [receipt(1, pdf(2)), receipt(2, pdf(0)), receipt(3, pdf(1)), receipt(4, pdf(0))]
    before = deepcopy(items)
    output, payload = derived(items)
    assert items == before
    assert payload["read_range"]["observed_intervals"] == [[1, 3]]
    assert payload["whole_extracted_text_returned"] is True
    assert output["whole_extracted_text_returned"] is True
    assert payload["source_receipt_ids"] == [item["id"] for item in items]
    assert payload["source_receipts"] == [{"id": item["id"], "sha256": item["sha256"]} for item in items]
    assert payload["read_contract"]["omitted_content"] == ["image_understanding"]
    assert payload["original_visual_content_observed"] is False
    assert payload["model_understanding_verified"] is False
    assert aggregate_process_receipts(items + [output]) == [output]


def test_pdf_gaps_and_repeated_page_never_prove_whole_document():
    _, payload = derived([receipt(1, pdf(0)), receipt(2, pdf(2)), receipt(3, pdf(0))])
    assert payload["read_range"]["observed_intervals"] == [[1, 1], [3, 3]]
    assert payload["whole_extracted_text_returned"] is False


@pytest.mark.parametrize("change", [
    lambda p: p["fixed_source"].update(raw_sha256="d" * 64),
    lambda p: p["read_range"].update(page_count=4),
    lambda p: p["read_range"].update(page_count=True),
    lambda p: p["read_range"].update(page_index=1.0),
    lambda p: p["read_range"].update(full_page_region=False),
    lambda p: p["read_range"].update(text_observed=False),
    lambda p: p["read_range"].update(next_page_index=1),
    lambda p: p.update(output_is_partial=True),
    lambda p: p.update(text_observed=False),
    lambda p: p["read_contract"].update(selected_text_returned_in_full=False),
    lambda p: p["read_contract"].update(version="old_unknown_contract"),
    lambda p: p["read_contract"].update(method={"unexpected": "object"}),
    lambda p: p["read_range"].update(kind=["pdf_page"]),
    lambda p: p["read_contract"].update(omitted_content=None),
    lambda p: p.pop("read_contract"),
])
def test_conflicting_identity_incomplete_contract_or_unknown_range_fails_closed(change):
    items = [receipt(1, pdf(0, 2)), mutate(receipt(2, pdf(1, 2)), change)]
    assert aggregate_process_receipts(items) == []


def test_revisions_are_never_joined_and_hash_failure_or_id_collision_does_not_count_twice():
    assert aggregate_process_receipts([receipt(1, pdf(0, 2)), receipt(2, pdf(1, 2), revision="d" * 32)]) == []
    first, last = receipt(1, pdf(0, 2)), receipt(2, pdf(1, 2))
    assert aggregate_process_receipts([first, deepcopy(first)]) == []
    corrupt = deepcopy(last); corrupt["content"] += " "
    assert aggregate_process_receipts([first, last, corrupt]) == []
    duplicate_id = deepcopy(last); duplicate_id["id"] = first["id"]
    assert aggregate_process_receipts([first, duplicate_id]) == []
    assert aggregate_process_receipts([first] * (MAX_RECEIPTS + 1)) == []


def test_malformed_deep_contract_or_surrogate_metadata_is_unusable_without_crashing():
    first, second = receipt(1, pdf(0, 2)), receipt(2, pdf(1, 2))
    payload = json.loads(second["content"])
    payload["read_contract"]["unexpected"] = [[[[["not projected metadata"]]]]]
    assert aggregate_process_receipts([first, wrap(2, payload)]) == []
    # An escaped surrogate can be valid JSON but cannot be emitted as UTF-8.
    for item in (first, second):
        payload = json.loads(item["content"]); payload["fixed_source"]["title"] = "\ud800"
        content = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        item.update(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
    assert aggregate_process_receipts([first, second]) == []


def lines(start, end, total=160):
    return {"kind": "text_lines", "line_start": start, "line_end": end, "total_lines": total,
            "next_line": end + 1 if end < total else None}


@pytest.mark.parametrize("second_start,whole", [(80, True), (81, True), (82, False)])
def test_text_continuous_ranges_allow_overlap_but_never_fill_missing_line(second_start, whole):
    _, payload = derived([receipt(1, lines(1, 80), method="retained_decoded_text_lines"),
        receipt(2, lines(second_start, 160), method="retained_decoded_text_lines")])
    assert payload["whole_extracted_text_returned"] is whole


def word(index=None, start=None, end=None, paragraphs=2, blocks=3):
    totals = {"total_paragraphs": paragraphs, "total_word_blocks": blocks}
    if index is not None:
        return {"kind": "word_block", "block_id": "wb_" + f"{index:032x}", "text_observed": True, **totals}
    return {"kind": "word_paragraphs", "paragraph_start": start, "paragraph_end": end,
        "next_paragraph": end + 1 if end < paragraphs else None, **totals}


def word_receipts():
    return [receipt(1, word(start=1, end=2), method="retained_word_structure"),
        *[receipt(index + 1, word(index=index), method="retained_word_structure") for index in (1, 2, 3)]]


def test_word_requires_all_body_paragraphs_and_every_distinct_nonempty_ooxml_part():
    items = word_receipts()
    _, payload = derived(items)
    assert payload["whole_extracted_text_returned"] is True
    assert payload["read_range"]["nonempty_word_block_count_observed"] == 3
    assert payload["read_range"]["rendered_page_available"] is False
    _, no_header = derived(items[:-1] + [receipt(6, word(index=2), method="retained_word_structure")])
    assert no_header["whole_extracted_text_returned"] is False
    _, no_body = derived(items[1:])
    assert no_body["whole_extracted_text_returned"] is False


@pytest.mark.parametrize("change", [
    lambda p: p["read_range"].update(total_word_blocks=4),
    lambda p: p["read_range"].update(block_id="untrusted-document-prose"),
    lambda p: p["fixed_source"].pop("extraction_fingerprint"),
    lambda p: p["fixed_source"].update(extraction_fingerprint="e" * 64),
])
def test_word_directory_totals_real_block_identity_and_extraction_must_agree(change):
    items = word_receipts()
    items[-1] = mutate(items[-1], change)
    assert aggregate_process_receipts(items) == []


def table(index, name, bounds, *, sheets=None):
    from openpyxl.utils.cell import range_boundaries
    left, top, right, bottom = range_boundaries(bounds)
    return receipt(index, {"kind": "sheet_cells", "sheet": name, "cell_range": bounds,
        "row_start": top, "row_end": bottom, "column_start": left, "column_end": right,
        "index_base": 1, "sheets": sheets or [{"name": "Data", "row_count": 2, "column_count": 2}]},
        method="retained_table_cells")


def test_sheet_coverage_uses_rectangle_union_not_bounding_box_or_repeated_area():
    _, diagonal = derived([table(1, "Data", "A1"), table(2, "Data", "B2")])
    assert diagonal["read_range"]["sheets"][0]["observed_cells"] == 2
    assert not diagonal["whole_extracted_text_returned"]
    _, overlap = derived([table(1, "Data", "A1:B1"), table(2, "Data", "A1:B1")])
    assert overlap["read_range"]["sheets"][0]["observed_cells"] == 2
    assert not overlap["whole_extracted_text_returned"]
    _, complete = derived([table(1, "Data", "A1:B1"), table(2, "Data", "A2:B2")])
    assert complete["whole_extracted_text_returned"]
    assert complete["read_range"]["formula_recalculation_performed"] is False


def test_all_sheets_including_hidden_and_declared_empty_sheets_are_considered():
    sheets = [{"name": "Data", "row_count": 2, "column_count": 2},
        {"name": "Hidden", "row_count": 1, "column_count": 1, "state": "hidden"},
        {"name": "Empty", "row_count": 0, "column_count": 0}]
    items = [table(1, "Data", "A1:B1", sheets=sheets), table(2, "Data", "A2:B2", sheets=sheets)]
    assert not derived(items)[1]["whole_extracted_text_returned"]
    items.append(table(3, "Hidden", "A1", sheets=sheets))
    assert derived(items)[1]["whole_extracted_text_returned"]
    items[-1] = mutate(items[-1], lambda p: p["read_range"]["sheets"][1].update(row_count=2))
    assert aggregate_process_receipts(items) == []


def test_rectangle_sweep_handles_full_excel_dimensions_without_enumerating_cells():
    assert _covered_area([(1, 1, 16_384, 1_048_576), (1, 1, 8_192, 1_048_576)]) == 17_179_869_184


def actual_reads(db, name, raw, selections):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.agent.citation_scope import process_receipts
    from app.runtime_context import bind_runtime_config
    from app.source_runtime import freeze_sources
    from app.source_tools import execute_source_tool
    with TestClient(app) as client:
        cid = client.post("/api/conversations", json={"title": "Coverage proof"}).json()["id"]
        upload = client.post("/api/sources/upload", files={"file": (name, raw)},
            data={"conversation_id": cid, "request_id": "coverage-fixed-source"})
        assert upload.status_code == 200, upload.text
        rid = upload.json()["revision_id"]
        binding = freeze_sources(db, cid, [rid], {"conversation_id": cid})
        chosen = selections(binding)
        history = []
        with bind_runtime_config(binding):
            for index, selection in enumerate(chosen):
                arguments = {"source_revision_id": rid, **selection}
                output = execute_source_tool(db, "read_attached_source", arguments, conversation_id=cid)
                assert not json.loads(output).get("error"), output
                history.extend([{"role": "assistant", "tool_calls": [{"id": f"actual-{index}", "type": "function",
                    "function": {"name": "read_attached_source", "arguments": json.dumps(arguments)}}]},
                    {"role": "tool", "tool_call_id": f"actual-{index}", "content": output}])
        db.commit()
        projected, _ = process_receipts(history)
        return [item for item in projected if item["tool_name"] == "read_attached_source"]


def test_actual_multipage_pdf_requires_every_real_page(db):
    import pymupdf
    document = pymupdf.open()
    for text in ("First fabricated page.", "Second fabricated page.", "Third fabricated page."):
        document.new_page().insert_text((72, 72), text)
    raw = document.tobytes(); document.close()
    items = actual_reads(db, "multipage.pdf", raw, lambda binding: [{"page_index": 2}, {"page_index": 0}, {"page_index": 1}])
    assert derived(items)[1]["whole_extracted_text_returned"]
    assert not derived(items[:2])[1]["whole_extracted_text_returned"]


def test_actual_word_body_and_footer_cannot_be_replaced_by_repeated_body_reads(db):
    from docx import Document
    document = Document()
    document.add_paragraph("Fabricated body research paragraph.")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Retained table field."
    document.sections[0].footer.paragraphs[0].text = "Retained footer limitation."
    stream = BytesIO(); document.save(stream)
    def selections(binding):
        fixed = binding["source_bindings"][0]
        assert fixed["word_block_directory_next_offset"] is None
        return [{"paragraph_start": 1, "paragraph_end": fixed["paragraph_count"]},
                *[{"block_id": part["block_id"]} for part in fixed["word_blocks"]]]
    items = actual_reads(db, "footer.docx", stream.getvalue(), selections)
    assert len(items) >= 3
    assert derived(items)[1]["whole_extracted_text_returned"]
    assert not derived(items[:-1])[1]["whole_extracted_text_returned"]
