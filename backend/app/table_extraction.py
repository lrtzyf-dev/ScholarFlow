"""Bounded table extraction; source strings, formulas and caches stay distinct."""
from __future__ import annotations

from collections import Counter
import codecs
import csv
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import io
import math
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from .source_store import SourceError, MAX_TEXT_BYTES

ENCODINGS = ("utf-8-sig", "utf-16", "gb18030", "cp1252")
DELIMITERS = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}
MAX_ROWS, MAX_COLUMNS, MAX_CELLS, MAX_SHEETS = 100_000, 512, 200_000, 100
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def column(index):
    from openpyxl.utils import get_column_letter
    return get_column_letter(index)


def string(value):
    return "" if value is None else value.isoformat() if hasattr(value, "isoformat") else str(value)


def csv_options(options=None):
    if options is not None and (not isinstance(options, dict) or set(options) - {"encoding", "delimiter", "header_row"}):
        raise SourceError("CSV 解析选项无效")
    value = {"encoding": "auto", "delimiter": "auto", "header_row": 1, **(options or {})}
    if value["encoding"] not in (*ENCODINGS, "auto") or value["delimiter"] not in (*DELIMITERS, "auto"):
        raise SourceError("CSV 编码或分隔符不受支持")
    if type(value["header_row"]) is not int or not 1 <= value["header_row"] <= MAX_ROWS:
        raise SourceError("CSV 表头记录须为有效正整数")
    return value


def decode_csv(raw, encoding):
    chosen = encoding
    if chosen == "auto":
        chosen = "utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    try:
        text = raw.decode(chosen, errors="strict")
        if "\x00" in text:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise SourceError("CSV 编码无法确认，请预览并选择正确编码；不使用替换字符猜测原文") from None
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise SourceError("CSV 解码文本超过大小限制")
    return text, chosen


def dialect(text, requested):
    if requested != "auto":
        return DELIMITERS[requested], "user_selected"
    try:
        value = csv.Sniffer().sniff(text[:65536], delimiters=";,\t|").delimiter
        return value, "detected_preview_required"
    except csv.Error:
        return ",", "single_column_or_undetermined"


def semantic(value):
    """Hints never turn a unit-bearing string or missing marker into a number."""
    text = value.strip()
    if not text:
        return {"value_kind": "blank", "missing": True}
    if text.casefold() in {"na", "n/a", "nan", "null", "none", "-", "—"}:
        return {"value_kind": "text", "missing_marker_candidate": True, "missing": False}
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        try:
            number = Decimal(text)
            if number.is_finite():
                return {"value_kind": "numeric_text", "decimal_text": str(number), "missing": False}
        except InvalidOperation:
            pass
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)%", text):
        return {"value_kind": "percentage_text", "percent_text": text, "missing": False,
                "conversion_applied": False}
    return {"value_kind": "text", "missing": False}


def header_metadata(cells, header_row=1):
    labels = [string(c.get("value")) for c in cells]
    duplicates = sorted(label for label, count in Counter(labels).items() if label and count > 1)
    return {"header_row": header_row, "header_is_user_declared": False,
        "columns": [{"column": column(i), "label": label,
                     "unit_hint": (re.search(r"[\[(（]([^\])）]{1,40})[\])）]\s*$", label).group(1)
                                   if re.search(r"[\[(（]([^\])）]{1,40})[\])）]\s*$", label) else None)}
                    for i, label in enumerate(labels, 1)],
        "duplicate_header_labels": duplicates, "blank_header_columns": [column(i) for i, v in enumerate(labels, 1) if not v],
        "column_identity": "cell_address_never_header_label", "unit_hint_verified": False}


def parse_csv(raw, add, options=None):
    requested = csv_options(options)
    text, encoding = decode_csv(raw, requested["encoding"])
    separator, detection = dialect(text, requested["delimiter"])
    count, maximum, headers = 0, 0, []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator, strict=True)
    total_cells, previous_line = 0, 0
    for index, values in enumerate(reader, 1):
        total_cells += len(values)
        if index > MAX_ROWS or total_cells > MAX_CELLS or len(values) > MAX_COLUMNS:
            raise SourceError("CSV 记录或单元格数量超过解析限制")
        cells = [{"cell": f"{column(j)}{index}", "value": value, "raw_value": value,
                  **semantic(value)} for j, value in enumerate(values, 1)]
        if index == requested["header_row"]:
            headers = cells
        add({"kind": "sheet_row", "sheet": "CSV", "row_start": index, "row_end": index,
            "cell_range": f"A{index}:{column(max(1, len(values)))}{index}", "cells": cells,
            "physical_line_start": previous_line + 1, "physical_line_end": reader.line_num,
            "text": "\t".join(values)})
        previous_line, count, maximum = reader.line_num, index, max(maximum, len(values))
    if count and requested["header_row"] > count:
        raise SourceError("CSV 表头记录超过原件记录数")
    header = header_metadata(headers, requested["header_row"])
    header["header_is_user_declared"] = options is not None and "header_row" in options
    metadata = {"encoding": encoding, "encoding_selection": "user_selected" if requested["encoding"] != "auto" else "bom_or_utf8",
        "delimiter": next(k for k, v in DELIMITERS.items() if v == separator), "delimiter_selection": detection,
        "requested_options": requested, "record_numbers_are_not_physical_lines": True,
        "values_coerced": False, "formula_recalculation_performed": False,
        "warnings": ["表头、单位和缺值标记只作提示；不自动改名、填零或转换单位。"], **header}
    if header["duplicate_header_labels"]:
        metadata["warnings"].append("存在重复表头；列始终按地址区分，不能按同名字段合并。")
    return text, [{"name": "CSV", "row_count": count, "column_count": maximum,
                  "headers": header}], metadata


def preview_csv(raw, options=None):
    value = csv_options(options)
    candidates = []
    for encoding in ENCODINGS:
        try:
            text, _ = decode_csv(raw, encoding)
            separator, method = dialect(text, value["delimiter"])
            rows = []
            for index, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=separator, strict=True)):
                if len(row) > MAX_COLUMNS:
                    raise SourceError("CSV 列数超过解析限制")
                rows.append([cell[:512] for cell in row[:20]])
                if index >= 9:
                    break
            candidates.append({"encoding": encoding, "delimiter": next(k for k, v in DELIMITERS.items() if v == separator),
                               "delimiter_selection": method, "rows": rows})
        except (SourceError, csv.Error):
            continue
    recommended = "utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    return {"candidates": candidates, "recommended_encoding": recommended if any(c["encoding"] == recommended for c in candidates) else None,
            "encoding_choices": list(ENCODINGS), "delimiter_choices": list(DELIMITERS), "preview_only": True,
            "source_saved": False, "preview_can_be_misdecoded": True}


def _sheet_xml(archive):
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels if r.get("TargetMode") != "External"}
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    result = {}
    total_cells, total_merges, total_rows = 0, 0, 0
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        target = targets.get(sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"))
        if not target:
            continue
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
        if not path.startswith("xl/") or path not in archive.namelist():
            raise SourceError("Excel 工作表部件无效")
        cells, merges, hidden, seen_rows = {}, [], [], set()
        current_row, previous_row = None, 0
        with archive.open(path) as stream:
            for event, node in ET.iterparse(stream, events=("start", "end")):
                tag = node.tag.rsplit("}", 1)[-1]
                if event == "start":
                    if tag == "row":
                        row = node.get("r")
                        if (current_row is not None or not isinstance(row, str)
                                or not re.fullmatch(r"[1-9][0-9]{0,6}", row) or int(row) > MAX_ROWS
                                or int(row) <= previous_row or len(seen_rows) >= MAX_ROWS):
                            raise SourceError("Excel 包含重复、乱序或无效行号，无法可靠定位")
                        current_row = int(row)
                        seen_rows.add(current_row)
                        total_rows += 1
                        if total_rows > MAX_CELLS:
                            raise SourceError("Excel 总行记录数量超过解析限制")
                    continue
                if tag == "c":
                    address = node.get("r")
                    if (not isinstance(address, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", address)
                            or address in cells or current_row is None
                            or int(re.search(r"[0-9]+$", address)[0]) != current_row):
                        raise SourceError("Excel 包含重复、缺失或与所在行不符的单元格地址，无法可靠定位")
                    total_cells += 1
                    if total_cells > MAX_CELLS:
                        raise SourceError("Excel 单元格数量超过解析限制")
                    saved = node.find("m:v", NS)
                    formula_node = node.find("m:f", NS)
                    cells[address] = {"stored_xml_value": saved.text if saved is not None else None,
                                      "value_element_present": saved is not None,
                                      "formula_xml": {**formula_node.attrib, "text": formula_node.text}
                                          if formula_node is not None else None}
                    node.clear()
                elif tag == "mergeCell":
                    total_merges += 1
                    if total_merges > MAX_CELLS:
                        raise SourceError("Excel 合并区域数量超过解析限制")
                    merges.append(node.get("ref")); node.clear()
                elif tag == "row":
                    if node.get("hidden") == "1":
                        hidden.append(current_row)
                    previous_row, current_row = current_row, None
                    node.clear()
        result[sheet.get("name")] = {"cells": cells, "merges": merges, "hidden_rows": hidden}
    return result


def parse_excel(raw, add):
    from openpyxl import load_workbook
    from openpyxl.styles.numbers import is_date_format
    from openpyxl.utils.cell import range_boundaries, coordinate_to_tuple
    formula_book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    cached_book = None
    try:
        cached_book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
        if len(formula_book.worksheets) > MAX_SHEETS:
            raise SourceError("Excel 工作表数量超过解析限制")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml_sheets = _sheet_xml(archive)
        sheets, formulas, missing_cache, cells_total = [], 0, 0, 0
        for sheet in formula_book.worksheets:
            xml = xml_sheets.get(sheet.title)
            if xml is None:
                raise SourceError("Excel 工作表结构缺失")
            merged, raw_cells = xml["merges"], xml["cells"]
            row_count, col_count = 0, 0
            special_ranges = [(address, data["formula_xml"]) for address, data in raw_cells.items()
                if data.get("formula_xml") and data["formula_xml"].get("t") in ("array", "dataTable")]
            for address in [*raw_cells, *merged, *(data.get("ref") or anchor for anchor, data in special_ranges)]:
                left, top, right, bottom = range_boundaries(address)
                if right > MAX_COLUMNS or bottom > MAX_ROWS or left < 1 or top < 1 or left > right or top > bottom:
                    raise SourceError("Excel 工作表范围超过解析限制")
                row_count, col_count = max(row_count, bottom), max(col_count, right)
            cells_total += row_count * col_count
            if cells_total > MAX_CELLS:
                raise SourceError("Excel 单元格数量超过解析限制")
            merge_map = {}
            for value in merged:
                left, top, right, bottom = range_boundaries(value)
                for row in range(top, bottom + 1):
                    for col in range(left, right + 1):
                        address = f"{column(col)}{row}"
                        if address in merge_map:
                            raise SourceError("Excel 包含重叠合并区域，无法可靠定位")
                        merge_map[address] = {"merged_range": value, "merged_anchor": f"{column(left)}{top}"}
            special_map = {}
            for anchor, data in special_ranges:
                ref = data.get("ref") or anchor
                left, top, right, bottom = range_boundaries(ref)
                if anchor != f"{column(left)}{top}":
                    raise SourceError("Excel 特殊公式锚点与声明区域不一致，无法可靠定位")
                for row in range(top, bottom + 1):
                    for col in range(left, right + 1):
                        address = f"{column(col)}{row}"
                        if address in special_map:
                            raise SourceError("Excel 包含重叠特殊公式区域，无法可靠定位")
                        special_map[address] = {"special_formula_range": ref, "special_formula_anchor": anchor,
                            "special_formula_type": data["t"], "special_formula_member": address != anchor}
            headers, sheet_formulas = [], 0
            if not row_count or not col_count:
                sheets.append({"name": sheet.title, "row_count": 0, "column_count": 0, "merged_ranges": [], "state": sheet.sheet_state})
                continue
            cached_rows = cached_book[sheet.title].iter_rows(min_row=1, max_row=row_count, min_col=1, max_col=col_count)
            for index, (row, cached_row) in enumerate(zip(sheet.iter_rows(min_row=1, max_row=row_count, min_col=1, max_col=col_count), cached_rows), 1):
                cells = []
                for col, (cell, cached_cell) in enumerate(zip(row, cached_row), 1):
                    address = f"{column(col)}{index}"
                    value, cached = cell.value, cached_cell.value
                    saved = raw_cells.get(address, {})
                    formula = cell.data_type == "f"
                    representation = "scalar" if isinstance(value, str) else getattr(value, "t", None)
                    formula_text = (value if isinstance(value, str) else getattr(value, "text", None)) if formula else None
                    formula_display = formula_text or (f"[Excel {representation or 'unsupported'} formula; ref={getattr(value, 'ref', '')}]") if formula else string(value)
                    kind = ("formula" if formula else "blank" if value is None else "boolean" if type(value) is bool else
                            "date" if isinstance(value, (date, datetime, time, timedelta)) else "error" if cell.data_type == "e" else
                            "number" if type(value) in (int, float) else "text")
                    if type(value) is float and not math.isfinite(value):
                        raise SourceError("Excel 包含无法可靠表示的非有限数值")
                    number_format = getattr(cell, "number_format", None) or "General"
                    data = {"cell": address, "value": formula_display, "raw_value": formula_display if formula else value if type(value) in (int, float, bool) or value is None else string(value),
                            "value_kind": kind, "missing": value is None, "number_format": number_format,
                            "percent_format": bool(re.search(r"(?<!\\)%", re.sub(r'"[^"]*"', "", number_format))),
                            "date_format": is_date_format(number_format), "formula_recalculated": False}
                    if saved.get("value_element_present"):
                        data["stored_xml_value"] = saved.get("stored_xml_value")
                    if formula:
                        formulas += 1; sheet_formulas += 1
                        present = saved.get("stored_xml_value") is not None
                        missing_cache += not present
                        data.update(formula=formula_display, formula_representation=representation or "unsupported",
                                    cached_value=cached if type(cached) in (int, float, bool) or cached is None else string(cached),
                                    cache_state="present_unverified" if present else "missing", cache_verified=False,
                                    external_reference="[" in (formula_text or ""))
                        formula_xml = saved.get("formula_xml") or {}
                        data.update(formula_storage_kind=formula_xml.get("t", "normal"),
                            stored_formula_text=formula_xml.get("text"),
                            formula_xml_attributes={key: value for key, value in formula_xml.items() if key != "text"},
                            formula_origin="shared_formula_expanded_to_this_cell" if formula_xml.get("t") == "shared" else "stored_formula")
                        if representation != "scalar":
                            data["formula_attributes"] = {key: getattr(value, key, None) for key in
                                ("ref", "text", "ca", "dt2D", "dtr", "r1", "r2", "del1", "del2")
                                if getattr(value, key, None) is not None}
                    data.update(merge_map.get(address, {}))
                    data.update(special_map.get(address, {}))
                    cells.append(data)
                if index == 1:
                    headers = cells
                if any(c["value_kind"] != "blank" or c.get("merged_range") or c.get("special_formula_range") for c in cells):
                    add({"kind": "sheet_row", "sheet": sheet.title, "row_start": index, "row_end": index,
                        "cell_range": f"A{index}:{column(col_count)}{index}", "cells": cells,
                        "text": "\t".join(c["value"] for c in cells)})
            sheets.append({"name": sheet.title, "row_count": row_count, "column_count": col_count,
                           "state": sheet.sheet_state, "merged_ranges": merged, "formula_count": sheet_formulas,
                           "headers": header_metadata(headers), "hidden_rows": xml["hidden_rows"]})
        calculation = formula_book.calculation
        return sheets, {"formula_count": formulas, "formula_missing_cache_count": missing_cache,
            "formula_cache_verified": False, "formula_recalculation_performed": False,
            "date_epoch": formula_book.epoch.isoformat(), "date_values": "ISO extracted value; original serial retained",
            "display_is_excel_rendering": False, "macros_executed": False, "external_links_fetched": False,
            "workbook_calculation": {k: getattr(calculation, k, None) for k in ("calcMode", "fullCalcOnLoad", "forceFullCalc", "calcId")},
            "warnings": ["公式、缓存值和重新计算结果分开保存；缓存可能过期，不能作为已复算结果。",
                         "百分比保留数值与格式，日期保留日期基准和原始序列；单位和多层表头须核对。",
                         "合并区域仅保留锚点原值，不把空白延续格填成重复数值；隐藏工作表/行也有标记。"]}
    finally:
        formula_book.close()
        if cached_book is not None:
            cached_book.close()
