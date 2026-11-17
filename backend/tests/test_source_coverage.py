"""Actual extracted text ranges can prove coverage, never semantic understanding."""
from app.agent.checkpoint import AgentState
from app.source_coverage import complete, record_source_read


def proof(rid, data):
    return {"source": {"revision_id": rid, "title": "notes.txt", "raw_sha256": "a" * 64}, "read_range": data}


def test_text_ranges_must_cover_gaps_and_every_selected_revision():
    state = AgentState(question="Read all sources", source_revision_ids=["a", "b"], todo=[{
        "title": "Read all", "phase": "read", "status": "pending", "acceptance": "source_coverage"}])
    first = {"kind": "text_lines", "line_start": 1, "line_end": 2, "total_lines": 4, "next_line": 3}
    record_source_read(state, proof("a", first))
    record_source_read(state, proof("a", {**first, "line_start": 4, "line_end": 4, "next_line": None}))
    assert not complete(state.source_reads, "a") and state.todo[0]["status"] == "pending"
    record_source_read(state, proof("a", {**first, "line_start": 2, "line_end": 3, "next_line": 4}))
    assert complete(state.source_reads, "a") and state.todo[0]["status"] == "pending"
    record_source_read(state, proof("b", {**first, "line_start": 1, "line_end": 4, "next_line": None}))
    assert state.todo[0]["status"] == "done"
    assert state.todo[0]["evidence"][0]["coverage_scope"] == "extracted_text_only"


def test_named_file_is_not_completed_by_partial_read_or_duplicate_ranges():
    state = AgentState(question="Read notes.txt", todo=[{"title": "Read notes.txt", "phase": "read", "status": "pending",
        "acceptance": "full_file:notes.txt"}])
    part = proof("a", {"kind": "text_lines", "line_start": 1, "line_end": 20, "total_lines": 40, "next_line": 21})
    record_source_read(state, part)
    record_source_read(state, part)
    assert len(state.source_reads) == 1 and state.todo[0]["status"] == "pending"
    record_source_read(state, proof("a", {"kind": "text_lines", "line_start": 21, "line_end": 40, "total_lines": 40, "next_line": None}))
    assert state.todo[0]["status"] == "done"


def test_empty_pdf_page_is_not_claimed_as_read_text_or_image_understanding():
    reads = [{"revision_id": "a", "range": {"kind": "pdf_page", "page_index": 0, "page_count": 2, "text_observed": True}},
             {"revision_id": "a", "range": {"kind": "pdf_page", "page_index": 1, "page_count": 2, "text_observed": False}}]
    assert not complete(reads, "a")
    reads[1]["range"]["text_observed"] = True
    assert complete(reads, "a")


def test_spreadsheet_coverage_requires_all_cells_and_sheets():
    sheets = [{"name": "A", "row_count": 2, "column_count": 2}, {"name": "B", "row_count": 1, "column_count": 1}]
    def entry(sheet, left, right, top, bottom):
        return {"revision_id": "a", "range": {"kind": "sheet_cells", "sheet": sheet, "column_start": left,
            "column_end": right, "row_start": top, "row_end": bottom, "sheets": sheets}}
    reads = [entry("A", 1, 1, 1, 2), entry("B", 1, 1, 1, 1)]
    assert not complete(reads, "a")
    reads.append(entry("A", 2, 2, 1, 2))
    assert complete(reads, "a")
