"""Bounded, local-only extraction. Content and formulas are data, never code.

The returned document is saved once per revision. Offsets refer to the retained
original and extraction, not a later reparse or the current version of a path.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from xml.parsers import expat

from .source_store import MAX_RAW_BYTES, MAX_TEXT_BYTES, SourceError

MEDIA = {
    "pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown", "markdown": "text/markdown",
    "csv": "text/csv", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_BLOCKS = 100_000
MAX_CELLS = 200_000
MAX_COLUMNS = 512
MAX_SHEETS = 100
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Extraction:
    text: str
    parser: str
    document: dict


def _utf8(raw):
    try:
        value = raw.decode("utf-8-sig")
        if "\0" in value:
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise SourceError("文本资料须为不含二进制空字符的 UTF-8 编码") from None


def _office_zip(raw):
    """Reject oversized/hostile OOXML before a document library inflates it."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 4096 or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise SourceError("Office 解压内容超过安全解析限制")
            if len({i.filename for i in infos}) != len(infos):
                raise SourceError("Office 包含重复内容部件")
            for item in infos:
                if item.flag_bits & 1 or item.file_size < 0:
                    raise SourceError("暂不解析加密 Office 原件")
                if item.filename.casefold().endswith((".xml", ".rels")):
                    xml = archive.read(item)
                    # A byte substring check misses UTF-16/32 declarations.
                    # Validate each XML part before an Office library sees it;
                    # Expat recognizes the encoding and rejects the DTD before
                    # parsing any entity declarations or replacement text.
                    def reject_declaration(*_args):
                        raise SourceError("Office 包含不支持的 XML DTD 或实体声明")
                    parser = expat.ParserCreate()
                    parser.StartDoctypeDeclHandler = reject_declaration
                    parser.EntityDeclHandler = reject_declaration
                    parser.ExternalEntityRefHandler = reject_declaration
                    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
                    try:
                        parser.Parse(xml, True)
                    except expat.ExpatError:
                        raise SourceError("Office XML 内容无效或编码不受支持") from None
    except SourceError:
        raise
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError):
        raise SourceError("此文件不是有效的 Office Open XML 原件") from None


def _column(index):
    result = ""
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def _string(value):
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def extract(raw: bytes, media_type: str, *, table_options=None) -> Extraction:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RAW_BYTES:
        raise SourceError("来源原件为空或超过大小限制")
    blocks, sheets, pages = [], [], []
    table_metadata = None
    size = 0

    def add(block):
        nonlocal size
        size += len(block["text"].encode("utf-8")) + 1
        if size > MAX_TEXT_BYTES or len(blocks) >= MAX_BLOCKS:
            raise SourceError("资料文本或结构超过本地解析限制，请拆分文件")
        blocks.append(block)

    try:
        if media_type in ("text/plain", "text/markdown"):
            text = _utf8(raw)
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise SourceError("文本提取结果超过大小限制")
            # Keep original line endings in text; blocks are only a reader view.
            for i, line in enumerate(text.splitlines(), 1):
                add({"kind": "text_line", "line_start": i, "line_end": i, "text": line})
            fmt, parser = "text", "utf8-sig/1"
        elif media_type == "text/csv":
            from .table_extraction import parse_csv
            text, sheets, table_metadata = parse_csv(raw, add, table_options)
            fmt, parser = "csv", "python-csv/2"
        elif media_type == MEDIA["docx"]:
            _office_zip(raw)
            from docx import Document
            from .docx_extraction import body_blocks, supplement
            document = Document(io.BytesIO(raw))
            for index, block in enumerate(body_blocks(document), 1):
                add({**block, "paragraph_index": index})
            word_metadata, word_omissions = supplement(raw, list(blocks), add)
            text, fmt, parser = "\n".join(b["text"] for b in blocks), "word", "ooxml-source/2"
        elif media_type == MEDIA["xlsx"]:
            _office_zip(raw)
            from .table_extraction import parse_excel
            sheets, table_metadata = parse_excel(raw, add)
            text, fmt, parser = "\n".join(f'[{b["sheet"]}!{b["cell_range"]}] {b["text"]}' for b in blocks), "excel", "openpyxl-source/2"
        elif media_type == "application/pdf":
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                if len(pdf.pages) > 1000:
                    raise SourceError("PDF 页数超过 1000 页解析限制")
                for i, page in enumerate(pdf.pages):
                    content = page.extract_text() or ""
                    geometry = {"page_index": i, "page_number": i + 1, "page_index_base": 0,
                                "bbox": list(page.bbox), "page_width": float(page.width),
                                "page_height": float(page.height), "rotation": page.rotation,
                                "geometry_verified": True, "coordinate_space": "pdf_points_top_left"}
                    pages.append(geometry)
                    add({"kind": "pdf_page", **geometry, "text": content})
            text, fmt, parser = "\n\f\n".join(b["text"] for b in blocks), "pdf", "pdfplumber-source/1"
        else:
            raise SourceError("此格式暂不支持本地结构解析")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise SourceError("提取文本超过大小限制")
        document = {"format": fmt, "parser": parser, "text": text, "blocks": blocks,
                    "sheets": sheets, "pages": pages, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "trust": "untrusted_source_data", "authorizes_actions": False,
                    "extraction_accuracy_verified": False, "model_understanding_verified": False}
        document["extraction_scope"] = {"word": "body_text_and_table_rows", "excel": "saved_cells_and_formulas",
            "csv": "csv_records", "pdf": "pdf_text_layer", "text": "decoded_text"}[fmt]
        document["omitted_content"] = {"word": ["images", "headers_and_footers", "footnotes_and_endnotes", "rendered_page_layout"],
            "excel": ["charts_and_shapes", "formula_recalculation", "rendered_page_layout"],
            "pdf": ["image_understanding", "scan_ocr"], "csv": [], "text": []}[fmt]
        if fmt == "word":
            document.update(word_structure=word_metadata, extraction_scope="body_and_referenced_ooxml_parts",
                            omitted_content=word_omissions,
                            paragraph_count=sum("paragraph_index" in b for b in blocks))
        if table_metadata is not None:
            document["table_metadata"] = table_metadata
        if len(json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_ARCHIVE_BYTES:
            raise SourceError("提取结构超过大小限制")
        return Extraction(text, parser, document)
    except SourceError:
        raise
    except Exception:
        # Never leak original document text, paths, XML or raw parser diagnostics.
        raise SourceError("原件已保留，但此格式的本地解析失败；请检查文件或转换格式") from None
