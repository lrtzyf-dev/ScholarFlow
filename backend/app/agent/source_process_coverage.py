"""Derive bounded extraction coverage from already authenticated tool receipts.

This module does not authenticate arbitrary input or read documents. Its caller
must pass only the successful, paired projections built by ``process_receipts``.
Source text, model assertions, requested ranges and directory listings are not
coverage. Unknown, conflicting or oversized input is never filled in by guess.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import re


MAX_RECEIPTS = 256
MAX_CONTENT_CHARS = 32_000
MAX_AGGREGATE_CHARS = 24_000
MAX_SHEETS = 256
_REVISION = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_BLOCK = re.compile(r"wb_[a-f0-9]{32}\Z")
_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?\Z")
_KINDS = {
    "pdfplumber_text_layer": {"pdf_page"},
    "retained_decoded_text_lines": {"text_lines"},
    "retained_ocr_text_lines": {"text_lines"},
    "retained_word_structure": {"word_paragraphs", "word_block"},
    "retained_table_cells": {"sheet_cells"},
}
_CONTRACT_FIELDS = {"version", "method", "selected_text_returned_in_full", "original_visual_content_observed",
                    "source_extraction_scope", "omitted_content", "extraction_accuracy_verified", "model_understanding_verified"}


def _integer(value, minimum=0, maximum=1_000_000):
    return type(value) is int and minimum <= value <= maximum


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _merge(intervals):
    result = []
    for start, end in sorted(set(intervals)):
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _all(intervals, total):
    return intervals == [[1, total]] if total else not intervals


def _next(extent, key, end, total):
    value = extent.get(key)
    return key in extent and (value is None if end == total else type(value) is int and value == end + 1)


def _metadata(payload):
    source, contract, extent = (payload.get(key) for key in ("fixed_source", "read_contract", "read_range"))
    if not all(isinstance(value, dict) for value in (source, contract, extent)):
        return None
    revision, raw_hash = source.get("revision_id"), source.get("raw_sha256")
    if (not isinstance(revision, str) or not _REVISION.fullmatch(revision)
            or not isinstance(raw_hash, str) or not _HASH.fullmatch(raw_hash)
            or not isinstance(source.get("media_type"), str)):
        return None
    method = contract.get("method")
    if (set(contract) - _CONTRACT_FIELDS or contract.get("version") != "source_read_execution_v1"
            or not isinstance(method, str) or method not in _KINDS
            or not isinstance(extent.get("kind"), str) or extent["kind"] not in _KINDS[method]
            or contract.get("selected_text_returned_in_full") is not True
            or contract.get("original_visual_content_observed") is not False
            or contract.get("extraction_accuracy_verified") is not False
            or contract.get("model_understanding_verified") is not False
            or payload.get("coverage_scope") != "extracted_text_only"
            or payload.get("text_observed") is not True
            or payload.get("output_is_partial") is not False):
        return None
    omitted = contract.get("omitted_content")
    if (not isinstance(omitted, list) or len(omitted) > 64
            or any(not isinstance(value, str) or len(value) > 256 for value in omitted)
            or contract.get("source_extraction_scope") is not None
            and not isinstance(contract["source_extraction_scope"], str)):
        return None
    if method in {"retained_word_structure", "retained_table_cells", "retained_ocr_text_lines"}:
        fingerprint = source.get("extraction_fingerprint")
        if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
            return None
    identity = {key: source[key] for key in ("title", "revision_id", "raw_sha256", "media_type", "parser",
                                            "text_origin", "extraction_fingerprint") if key in source}
    if any(value is not None and (not isinstance(value, str) or len(value) > 1024) for value in identity.values()):
        return None
    if (extent.get("raw_sha256", raw_hash) != raw_hash
            or extent.get("extraction_fingerprint", source.get("extraction_fingerprint")) != source.get("extraction_fingerprint")):
        return None
    return identity, deepcopy(contract), extent


def _linear(rows, *, kind, total_key, start_key, end_key, next_key):
    total = rows[0][2].get(total_key)
    maximum = 1000 if kind == "pdf_pages" else 1_000_000
    if not _integer(total, 1, maximum):
        return None
    intervals = []
    for _, _, extent in rows:
        if type(extent.get(total_key)) is not int or extent[total_key] != total:
            return None
        if kind == "pdf_pages":
            page = extent.get("page_index")
            if (not _integer(page, 0, total - 1) or extent.get("full_page_region") is not True
                    or extent.get("text_observed") is not True
                    or not _next(extent, "next_page_index", page, total - 1)):
                return None
            intervals.append((page + 1, page + 1))
        else:
            start, end = extent.get(start_key), extent.get(end_key)
            if (not _integer(start, 1, total) or not _integer(end, start, total)
                    or end - start >= 200 or not _next(extent, next_key, end, total)):
                return None
            intervals.append((start, end))
    merged = _merge(intervals)
    return {"kind": kind, total_key: total, "observed_intervals": merged,
            "index_base": 1}, _all(merged, total)


def _word(rows):
    first = rows[0][2]
    paragraphs, blocks = first.get("total_paragraphs"), first.get("total_word_blocks")
    if not _integer(paragraphs) or not _integer(blocks):
        return None
    intervals, observed_blocks = [], set()
    for _, _, extent in rows:
        if (type(extent.get("total_paragraphs")) is not int or extent["total_paragraphs"] != paragraphs
                or type(extent.get("total_word_blocks")) is not int or extent["total_word_blocks"] != blocks):
            return None
        if extent["kind"] == "word_paragraphs":
            start, end = extent.get("paragraph_start"), extent.get("paragraph_end")
            if (not _integer(start, 1, paragraphs) or not _integer(end, start, paragraphs)
                    or end - start >= 200 or not _next(extent, "next_paragraph", end, paragraphs)):
                return None
            intervals.append((start, end))
        else:
            block = extent.get("block_id")
            if not isinstance(block, str) or not _BLOCK.fullmatch(block) or extent.get("text_observed") is not True:
                return None
            observed_blocks.add(block)
    if len(observed_blocks) > blocks:
        return None
    merged = _merge(intervals)
    return {"kind": "word_extracted_parts", "total_paragraphs": paragraphs, "paragraph_intervals": merged,
            "total_nonempty_word_blocks": blocks, "observed_nonempty_word_block_ids": sorted(observed_blocks),
            "nonempty_word_block_count_observed": len(observed_blocks), "rendered_page_available": False}, (
                _all(merged, paragraphs) and len(observed_blocks) == blocks)


def _column(letters):
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value


def _rectangle(extent, metadata):
    value = extent.get("cell_range")
    match = _CELL.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    left, top = _column(match[1]), int(match[2])
    right, bottom = _column(match[3] or match[1]), int(match[4] or match[2])
    if not (1 <= left <= right <= metadata["column_count"] and 1 <= top <= bottom <= metadata["row_count"]):
        return None
    expected = {"row_start": top, "row_end": bottom, "column_start": left, "column_end": right, "index_base": 1}
    if any(type(extent.get(key)) is not int or extent[key] != value for key, value in expected.items()):
        return None
    return left, top, right, bottom


def _covered_area(rectangles):
    """Sweep rectangle boundaries, never enumerate workbook cells or rows."""
    boundaries = sorted({edge for _, top, _, bottom in rectangles for edge in (top, bottom + 1)})
    area = 0
    for start, end in zip(boundaries, boundaries[1:]):
        intervals = _merge([(left, right) for left, top, right, bottom in rectangles if top <= start <= bottom])
        area += (end - start) * sum(right - left + 1 for left, right in intervals)
    return area


def _sheets(rows):
    sheets = rows[0][2].get("sheets")
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= MAX_SHEETS:
        return None
    metadata = {}
    for sheet in sheets:
        if (not isinstance(sheet, dict) or not isinstance(sheet.get("name"), str) or not sheet["name"]
                or set(sheet) - {"name", "row_count", "column_count", "state", "formula_count"}
                or len(sheet["name"]) > 128 or sheet["name"] in metadata
                or not _integer(sheet.get("row_count"), 0, 1_048_576)
                or not _integer(sheet.get("column_count"), 0, 16_384)
                or bool(sheet["row_count"]) != bool(sheet["column_count"])):
            return None
        metadata[sheet["name"]] = sheet
    rectangles = defaultdict(set)
    for _, _, extent in rows:
        if (_encoded(extent.get("sheets")) != _encoded(sheets) or not isinstance(extent.get("sheet"), str)
                or extent["sheet"] not in metadata):
            return None
        name = extent["sheet"]
        rectangle = _rectangle(extent, metadata[name])
        if rectangle is None:
            return None
        rectangles[name].add(rectangle)
    coverage = []
    for name, sheet in metadata.items():
        selected = sorted(rectangles[name])
        covered = _covered_area(selected)
        total = sheet["row_count"] * sheet["column_count"]
        coverage.append({"name": name, "row_count": sheet["row_count"], "column_count": sheet["column_count"],
            "observed_rectangles": [list(rectangle) for rectangle in selected], "observed_cells": covered,
            "retained_cells": total, "all_retained_cells_returned": covered == total})
    return {"kind": "retained_workbook_cells", "sheets": coverage,
            "formula_recalculation_performed": False, "original_excel_rendering_observed": False}, all(
                sheet["all_retained_cells_returned"] for sheet in coverage)


def aggregate_process_receipts(receipts):
    """Return extra derived receipts; leave every original receipt untouched.

    Only multiple reads of one immutable revision can be combined. Conflicting
    identity, extraction, totals or execution contracts suppress that revision's
    aggregate. Valid gaps remain explicit partial coverage. Word completion
    needs both every compatibility paragraph and every distinct nonempty OOXML
    part; a directory or repeated reads of one block cannot supply missing parts.
    """
    if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS:
        return []
    grouped, invalid, seen = defaultdict(list), set(), {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("tool_name") != "read_attached_source":
            continue
        content = receipt.get("content")
        if not isinstance(content, str) or len(content) > MAX_CONTENT_CHARS:
            continue
        try:
            payload = json.loads(content)
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("fixed_source"), dict):
            continue
        revision = payload["fixed_source"].get("revision_id")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            continue
        item = _metadata(payload)
        receipt_id = receipt.get("id")
        try:
            sha = hashlib.sha256(content.encode()).hexdigest()
        except UnicodeError:
            invalid.add(revision)
            continue
        if (item is None or receipt.get("scope") != "process_metadata_only"
                or not isinstance(receipt_id, str) or not 1 <= len(receipt_id) <= 128
                or receipt.get("sha256") != sha):
            invalid.add(revision)
            continue
        if receipt_id in seen:
            previous_revision, previous_hash = seen[receipt_id]
            if previous_revision != revision or previous_hash != sha:
                invalid.update((revision, previous_revision))
            continue
        seen[receipt_id] = revision, sha
        grouped[revision].append((receipt, *item))
    result = []
    for revision, group in grouped.items():
        if revision in invalid or len(group) < 2:
            continue
        source, contract = group[0][1:3]
        if any(item[1] != source or item[2] != contract for item in group):
            continue
        rows = [(receipt, metadata, extent) for receipt, metadata, _, extent in group]
        method = contract["method"]
        if method == "pdfplumber_text_layer":
            coverage = _linear(rows, kind="pdf_pages", total_key="page_count", start_key=None, end_key=None, next_key=None)
        elif method in {"retained_decoded_text_lines", "retained_ocr_text_lines"}:
            coverage = _linear(rows, kind="text_lines", total_key="total_lines", start_key="line_start",
                               end_key="line_end", next_key="next_line")
        elif method == "retained_word_structure":
            coverage = _word(rows)
        else:
            coverage = _sheets(rows)
        if coverage is None:
            continue
        extent, whole = coverage
        members = [{"id": row[0]["id"], "sha256": row[0]["sha256"]} for row in group]
        aggregate = {"version": "source_process_coverage_v1", "derived_from_paired_read_receipts": True,
            "fixed_source": source, "source_receipt_ids": [member["id"] for member in members],
            "source_receipts": members, "read_contract": contract, "read_range": extent,
            "coverage_scope": "extracted_text_only", "whole_extracted_text_returned": whole,
            "output_is_partial": not whole, "original_visual_content_observed": False,
            "extraction_accuracy_verified": False, "model_understanding_verified": False}
        content = _encoded(aggregate)
        if len(content) > MAX_AGGREGATE_CHARS:
            continue
        try:
            digest = hashlib.sha256(content.encode()).hexdigest()
        except UnicodeError:
            continue
        result.append({"id": "source-coverage:" + digest[:32], "tool_name": "read_attached_source_coverage",
            "content": content, "sha256": digest, "scope": "process_metadata_only",
            "whole_extracted_text_returned": whole})
    return result
