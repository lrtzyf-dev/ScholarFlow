"""Mechanical text-range coverage; this never certifies understanding/images."""
from __future__ import annotations

from copy import deepcopy


def _covers(intervals, total, *, first=1):
    if type(total) is not int or total < 0:
        return False
    end = first
    for left, right in sorted(intervals):
        if type(left) is not int or type(right) is not int or left > end:
            return False
        end = max(end, right + 1)
    return end >= first + total


def complete(reads, rid):
    ranges = [item["range"] for item in reads if item.get("revision_id") == rid]
    if not ranges:
        return False
    kind = ranges[0]["kind"]
    if kind in ("word_paragraphs", "word_block"):
        word = [r for r in ranges if r.get("kind") in ("word_paragraphs", "word_block")]
        total = word[0]["total_paragraphs"]
        total_parts = word[0].get("total_word_blocks", 0)
        if any(r["total_paragraphs"] != total or r.get("total_word_blocks", 0) != total_parts for r in word):
            return False
        parts = {r["block_id"] for r in word if r["kind"] == "word_block" and r.get("text_observed")}
        return len(parts) == total_parts and _covers([(r["paragraph_start"], r["paragraph_end"])
            for r in word if r["kind"] == "word_paragraphs"], total)
    ranges = [item for item in ranges if item.get("kind") == kind]
    if kind == "text_lines":
        return _covers([(r["line_start"], r["line_end"]) for r in ranges], ranges[0]["total_lines"])
    if kind == "pdf_page":
        return _covers([(r["page_index"], r["page_index"]) for r in ranges if r.get("text_observed")],
                       ranges[0]["page_count"], first=0)
    if kind == "sheet_cells":
        for sheet in ranges[0]["sheets"]:
            rows, columns = sheet["row_count"], sheet["column_count"]
            if not rows or not columns:
                continue
            selected = [r for r in ranges if r.get("sheet") == sheet["name"]]
            for row in range(1, rows + 1):
                if not _covers([(r["column_start"], r["column_end"]) for r in selected
                                if r["row_start"] <= row <= r["row_end"]], columns):
                    return False
        return True
    return False


def record_source_read(state, payload):
    source, read_range = payload.get("source"), payload.get("read_range")
    if not isinstance(source, dict) or not isinstance(read_range, dict) or not source.get("revision_id"):
        return
    proof = {"revision_id": source["revision_id"], "raw_sha256": source["raw_sha256"],
             "range": deepcopy(read_range), "coverage_scope": "extracted_text_only"}
    if proof not in state.source_reads:
        state.source_reads.append(proof)
    full = complete(state.source_reads, source["revision_id"])
    for step in state.todo:
        acceptance = step.get("acceptance", "")
        if full and acceptance.startswith("full_file:") and acceptance[10:] == source["title"].casefold():
            step.update(status="done", evidence=[proof])
        if (acceptance == "source_coverage" and state.source_revision_ids
                and not (state.workspace or {}).get("files")
                and all(complete(state.source_reads, rid) for rid in state.source_revision_ids)):
            step.update(status="done", evidence=[{"kind": "source_text_coverage",
                "revision_ids": list(state.source_revision_ids), "coverage_scope": "extracted_text_only"}])
