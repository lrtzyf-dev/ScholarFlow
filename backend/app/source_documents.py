"""Immutable structured source extraction and server-derived Office locators."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy.dialects.sqlite import insert

from .source_models import SourceExtraction
from . import source_store as store
from .source_parsing import Extraction, extract


def _hash(raw_hash, text_hash, parser, document):
    return hashlib.sha256(json.dumps([raw_hash, text_hash, parser, document], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def save_extraction(db, revision_id, *, scope, extraction: Extraction):
    record = store.read_revision(db, revision_id, scope=scope)
    document = extraction.document
    if (record["raw_sha256"] != document["raw_sha256"]
            or hashlib.sha256(extraction.text.encode("utf-8")).hexdigest() != document["text_sha256"]
            or (record["text"] is not None and record["text"] != extraction.text)):
        raise store.SourceConflict("结构提取结果与来源版本不一致")
    fingerprint = _hash(record["raw_sha256"], document["text_sha256"], extraction.parser, document)
    db.execute(insert(SourceExtraction).values(revision_id=revision_id, raw_sha256=record["raw_sha256"],
        text_sha256=document["text_sha256"], parser=extraction.parser, document=document, fingerprint=fingerprint)
        .on_conflict_do_nothing(index_elements=["revision_id"]))
    db.flush()
    return read_document(db, revision_id, scope=scope)


def read_document(db, revision_id, *, scope):
    record = store.read_revision(db, revision_id, scope=scope)
    row = db.get(SourceExtraction, revision_id, populate_existing=True)
    if row is None:
        raise store.SourceNotFound("此固定版本尚无结构提取结果，请保留或下载原件")
    try:
        valid = (row.raw_sha256 == record["raw_sha256"] == row.document["raw_sha256"]
            and row.text_sha256 == row.document["text_sha256"]
            and hashlib.sha256(row.document["text"].encode("utf-8")).hexdigest() == row.text_sha256
            and row.fingerprint == _hash(row.raw_sha256, row.text_sha256, row.parser, row.document))
    except (KeyError, TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise store.SourceCorruption("来源结构提取校验失败，不能作为原文证据使用")
    return {**row.document, "source_id": record["source_id"], "revision_id": revision_id,
            "fingerprint": row.fingerprint, "title": record["title"]}


def prepare_extraction(db, revision_id, *, scope):
    """Compatibility for retained old versions; caller parses outside write lock."""
    row = db.get(SourceExtraction, revision_id)
    if row is not None:
        return None
    record = store.read_revision(db, revision_id, scope=scope, include_raw=True)
    return extract(record["raw_bytes"], record["media_type"])


def add_document_locator(db, revision_id, *, scope, kind, paragraph_start=None,
                         paragraph_end=None, sheet=None, cell_range=None, block_id=None):
    doc = read_document(db, revision_id, scope=scope)
    if kind == "word_paragraphs":
        paragraphs = [b for b in doc["blocks"] if "paragraph_index" in b]
        if (doc["format"] != "word" or type(paragraph_start) is not int or type(paragraph_end) is not int
                or not 1 <= paragraph_start <= paragraph_end <= len(paragraphs)
                or paragraph_end - paragraph_start >= 200):
            raise store.SourceError("Word 段落范围无效；段落从1开始，单次最多200段")
        quote = "\n".join(b["text"] for b in paragraphs[paragraph_start - 1:paragraph_end])
        location = {"paragraph_start": paragraph_start, "paragraph_end": paragraph_end, "paragraph_index_base": 1}
    elif kind == "word_block":
        if doc["format"] != "word" or not isinstance(block_id, str) or not re.fullmatch(r"wb_[a-f0-9]{32}", block_id):
            raise store.SourceError("Word 内容部件定位无效")
        block = next((b for b in doc["blocks"] if b.get("block_id") == block_id and b["kind"] == "word_part"), None)
        if block is None:
            raise store.SourceError("Word 内容部件不属于此固定版本")
        quote = block["text"]
        location = {key: block[key] for key in ("block_id", "part_name", "xml_path", "section_kind", "part_label",
            "table_path", "table_row", "table_column", "column_span", "vertical_merge", "table_depth", "logical_grid") if key in block}
        location["rendered_page_available"] = False
    elif kind == "sheet_cells":
        if doc["format"] not in ("excel", "csv") or not isinstance(sheet, str):
            raise store.SourceError("此来源没有可定位工作表")
        if sheet not in [s["name"] for s in doc["sheets"]]:
            raise store.SourceError("工作表不属于此固定版本")
        value = cell_range.upper() if isinstance(cell_range, str) else ""
        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?", value)
        if not match:
            raise store.SourceError("单元格范围须为 A1 或 A1:C20 格式")
        from openpyxl.utils.cell import range_boundaries
        left, top, right, bottom = range_boundaries(value)
        if left > right or top > bottom or (right - left + 1) * (bottom - top + 1) > 10_000:
            raise store.SourceError("单元格范围无效或超过10000格")
        meta = next(s for s in doc["sheets"] if s["name"] == sheet)
        if right > meta["column_count"] or bottom > meta["row_count"]:
            raise store.SourceError("单元格范围超出真实工作表")
        values = {}
        for block in doc["blocks"]:
            if block.get("sheet") == sheet and top <= block["row_start"] <= bottom:
                for cell in block["cells"]:
                    col, row, _, _ = range_boundaries(cell["cell"])
                    if left <= col <= right:
                        values[(row, col)] = cell["value"]
        quote = "\n".join("\t".join(values.get((r, c), "") for c in range(left, right + 1))
                          for r in range(top, bottom + 1))
        location = {"sheet": sheet, "cell_range": value, "row_start": top, "row_end": bottom,
                    "column_start": left, "column_end": right, "index_base": 1,
                    "formula_values_recalculated": False}
        if doc.get("table_metadata"):
            table = doc["table_metadata"]
            location["table_semantics"] = {key: table[key] for key in (
                "encoding", "delimiter", "header_row", "values_coerced", "date_epoch",
                "formula_cache_verified", "formula_recalculation_performed", "display_is_excel_rendering") if key in table}
            location["value_source"] = "retained_original_cells_and_formula_text_not_recomputed_results"
    else:
        raise store.SourceError("不支持的结构定位类型")
    location.update(raw_sha256=doc["raw_sha256"], text_sha256=doc["text_sha256"],
                    extraction_fingerprint=doc["fingerprint"])
    return store._save_locator(db, revision_id, kind, location, quote)
