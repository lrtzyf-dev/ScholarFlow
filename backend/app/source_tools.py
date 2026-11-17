"""Model-visible reads are limited to the user's frozen attachment selection."""
from __future__ import annotations

import json

from . import source_store as store
from .source_documents import read_document, add_document_locator
from .source_runtime import require_attached_revision, conversation_scope, validate_frozen_sources, capture_bindings

SOURCE_TOOL_NAMES = {"list_attached_sources", "read_attached_source", "verify_attached_table"}
SOURCE_TOOL_SPECS = [
    {"name": "list_attached_sources", "description": "列出本轮由用户选择的附件/本地文件固定版本及可用定位方式。仅返回目录，不表示已阅读；无关聊天无需调用。",
     "parameters": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "read_attached_source", "description": "读取本轮附件/本地文件的真实原文并返回可引用eN证据（正文引用用[^eN]）。先按来源目录中的真实总行/页/段数决定范围；未知结束位置时省略end，不要猜数字。PDF page_index从0开始；文本line_start/end与Word paragraph_start/end从1开始；Word表格正文行是兼容提取行，嵌套表格、页眉页脚、脚注尾注及图片文字说明须按word_blocks中的block_id另读，目录不全用block_offset翻页；Excel/CSV用sheet与A1:C20格式cell_range。返回实际read_range和后续位置，局部读取不等于已读全文或理解原件图片。",
     "parameters": {"type": "object", "additionalProperties": False, "properties": {
         "source_revision_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
         "page_index": {"type": "integer", "minimum": 0},
         "line_start": {"type": "integer", "minimum": 1}, "line_end": {"type": "integer", "minimum": 1},
         "paragraph_start": {"type": "integer", "minimum": 1}, "paragraph_end": {"type": "integer", "minimum": 1},
         "block_id": {"type": "string", "pattern": "^wb_[a-f0-9]{32}$"},
         "block_offset": {"type": "integer", "minimum": 0, "description": "Word补充内容目录偏移，返回最多100条目录、不代表阅读。用block_id读取实际原文。"},
         "sheet": {"type": "string"}, "cell_range": {"type": "string"}}, "required": ["source_revision_id"]}},
    {"name": "verify_attached_table", "description": "实际核查本轮附件固定Excel版本中的数值公式，保存可下载复算脚本的执行记录并返回原文定位证据。须先读目录/表格，按真实sheet和范围选择，单次最多200格。仅支持保守数值算术与SUM/AVERAGE/MIN/MAX/COUNT；错误、数组、数据表和其他不支持公式保留未解决，不能声称已完成；公式缓存从不作为计算输入，匹配也不证明缓存新鲜。CSV保留原文本，不自动转换单位。",
     "parameters": {"type": "object", "additionalProperties": False, "properties": {
         "source_revision_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
         "sheet": {"type": "string", "minLength": 1, "maxLength": 128},
         "cell_range": {"type": "string", "minLength": 2, "maxLength": 32},
         "absolute_tolerance": {"type": "number", "minimum": 0, "maximum": 1, "default": 1e-9},
         "relative_tolerance": {"type": "number", "minimum": 0, "maximum": 1, "default": 1e-9}},
         "required": ["source_revision_id", "sheet", "cell_range"]}},
]


def execute_source_tool(db, name, arguments, *, conversation_id=None):
    from .runtime_context import current_runtime_config
    binding = current_runtime_config() or {}
    conversation_id = conversation_id or binding.get("conversation_id")
    available = None
    try:
        if name == "verify_attached_table":
            allowed = {"source_revision_id", "sheet", "cell_range", "absolute_tolerance", "relative_tolerance"}
            if set(arguments) - allowed or not {"source_revision_id", "sheet", "cell_range"} <= set(arguments):
                raise store.SourceError("表格核查仅接受固定来源、工作表、范围和容差")
            rid = arguments["source_revision_id"]
            require_attached_revision(db, rid, conversation_id=conversation_id, binding=binding)
            scope = conversation_scope(db, conversation_id, writing=True)
            doc = read_document(db, rid, scope=scope)
            if doc["format"] != "excel":
                raise store.SourceError("此工具仅核查Excel公式；CSV须保留原文本并由用户确认数值解释")
            from .table_analysis import canonical, compute, digest
            config = {"sheet": arguments["sheet"], "cell_range": arguments["cell_range"],
                "absolute_tolerance": arguments.get("absolute_tolerance", 1e-9),
                "relative_tolerance": arguments.get("relative_tolerance", 1e-9)}
            if any(type(config[key]) not in (int, float) or not 0 <= config[key] <= 1
                   for key in ("absolute_tolerance", "relative_tolerance")):
                raise store.SourceError("核查容差须为0到1之间的有限数值")
            from openpyxl.utils.cell import range_boundaries
            try:
                left, top, right, bottom = range_boundaries(config["cell_range"].upper())
                valid = all(type(v) is int for v in (left, top, right, bottom)) and left <= right and top <= bottom
            except (TypeError, ValueError, AttributeError):
                valid = False
            if not valid or (right - left + 1) * (bottom - top + 1) > 200:
                raise store.SourceError("单次表格核查最多200格，请提供有效A1范围并分批核查")
            request_id = "agent-table-" + digest(canonical([rid, doc["fingerprint"], config]))
            read_args = {k: arguments[k] for k in ("source_revision_id", "sheet", "cell_range")}
            observed = json.loads(execute_source_tool(db, "read_attached_source", read_args, conversation_id=conversation_id))
            if observed.get("error"):
                raise store.SourceError("表格原文定位失败，尚未计算；请缩小范围后核对")
            receipt = compute(db, rid, scope=scope, request_id=request_id,
                expected_fingerprint=doc["fingerprint"], config=config, manage_transaction=False)
            return json.dumps({"status": "succeeded", "verification_status": receipt["result"]["status"],
                "all_selected_formulas_computed": bool(receipt["result"]["formula_count"] and not receipt["result"]["unresolved_count"]),
                "computation": receipt, "evidence": observed.get("evidence", []),
                "locator": observed.get("locator"), "original_quote_is_not_computation_result": True,
                "bundle_path": f"/api/sources/revisions/{rid}/table-computations/{receipt['id']}/bundle",
                "instruction": "eN证据定位原文公式，数值结论另须注明本次实际执行记录和未解决项；不得把原缓存当复算结果或把局部核查称整表通过。"}, ensure_ascii=False)
        if name == "list_attached_sources":
            if arguments:
                raise store.SourceError("来源目录工具不接受范围覆盖参数")
            return json.dumps({"sources": validate_frozen_sources(db, conversation_id, binding),
                "text_read": False, "model_understanding_verified": False}, ensure_ascii=False)
        allowed = {"source_revision_id", "page_index", "line_start", "line_end", "paragraph_start", "paragraph_end", "sheet", "cell_range", "block_id", "block_offset"}
        if set(arguments) - allowed:
            raise store.SourceError("不允许覆盖本轮来源范围")
        rid = arguments.get("source_revision_id")
        record = require_attached_revision(db, rid, conversation_id=conversation_id, binding=binding,
                                           allow_service_derived=True)
        available = capture_bindings(db, conversation_id, [rid])[0]
        local_read = record.get("local_read_authorization")
        if local_read:
            available = {**available, "local_read_authorization": local_read,
                         "read_representation": "retained_ocr_text_lines"}
        retained_ocr = bool(local_read) or (record.get("text_origin") == "ocr_text"
                                           and available.get("document_format") == "text")
        if retained_ocr:
            available = {**available, "read_representation": "retained_ocr_text_lines",
                "extraction_scope": "retained_ocr_text_only",
                "omitted_content": list(dict.fromkeys([*available.get("omitted_content", []),
                    "original_page_geometry", "original_layout", "ocr_accuracy_verification"]))}
        scope = conversation_scope(db, conversation_id)
        read_range, adjusted = {}, False
        table_context = None
        read_method = None
        if record["media_type"] == "application/pdf" and not retained_ocr:
            if set(arguments) - {"source_revision_id", "page_index"}:
                raise store.SourceError("PDF请使用真实页面索引定位")
            locator = store.add_pdf_locator(db, rid, scope=scope, page_index=arguments.get("page_index", 0))
            location = locator["location"]
            read_range = {"kind": "pdf_page", "page_index": location["page_index"], "page_count": location["page_count"],
                "next_page_index": location["page_index"] + 1 if location["page_index"] + 1 < location["page_count"] else None,
                "text_observed": location["text_observed"],
                "full_page_region": location["bbox"] == location["page_bbox"]}
            read_method = "pdfplumber_text_layer"
        elif retained_ocr or (record["media_type"].startswith("text/") and record["media_type"] != "text/csv"):
            if set(arguments) - {"source_revision_id", "line_start", "line_end"}:
                raise store.SourceError("文本请使用起止行定位")
            start = arguments.get("line_start", 1)
            lines = (record["text"] or "").splitlines()
            end = arguments.get("line_end", min(len(lines), start + 79) if type(start) is int else 0)
            if type(end) is int and end > len(lines):
                end, adjusted = len(lines), True
            if type(start) is not int or type(end) is not int or end - start >= 200:
                raise store.SourceError("每次最多读取200行")
            locator = store.add_text_locator(db, rid, scope=scope, line_start=start, line_end=end)
            read_range = {"kind": "text_lines", "line_start": start, "line_end": end, "total_lines": len(lines),
                          "next_line": end + 1 if end < len(lines) else None}
            read_method = "retained_ocr_text_lines" if retained_ocr else "retained_decoded_text_lines"
        else:
            doc = read_document(db, rid, scope=scope)
            if doc["format"] == "word":
                read_method = "retained_word_structure"
                parts = [b for b in doc["blocks"] if b.get("kind") == "word_part"]
                total = sum("paragraph_index" in b for b in doc["blocks"])
                totals = {"total_paragraphs": total, "total_word_blocks": sum(bool(b["text"].strip()) for b in parts)}
                if "block_offset" in arguments:
                    offset = arguments["block_offset"]
                    if set(arguments) != {"source_revision_id", "block_offset"} or type(offset) is not int or not 0 <= offset <= len(parts):
                        raise store.SourceError("Word内容目录偏移无效，不能与读取范围混用")
                    return json.dumps({"word_blocks": [{k: b[k] for k in ("block_id", "part_label", "section_kind", "table_row", "table_column") if k in b}
                        for b in parts[offset:offset + 100]], "next_block_offset": offset + 100 if offset + 100 < len(parts) else None,
                        "text_read": False, "source_directory": available}, ensure_ascii=False)
                if "block_id" in arguments:
                    if set(arguments) != {"source_revision_id", "block_id"}:
                        raise store.SourceError("Word内容部件不能与段落范围混用")
                    locator = add_document_locator(db, rid, scope=scope, kind="word_block", block_id=arguments["block_id"])
                    read_range = {"kind": "word_block", "block_id": arguments["block_id"], **totals,
                                  "text_observed": bool(locator["quote"].strip())}
                else:
                    if set(arguments) - {"source_revision_id", "paragraph_start", "paragraph_end"}:
                        raise store.SourceError("Word请使用起止段落或block_id定位")
                    if not total:
                        return json.dumps({"source_directory": available, "text_read": False,
                            "instruction": "正文没有段落，请使用word_blocks中的block_id读取实际内容"}, ensure_ascii=False)
                    start = arguments.get("paragraph_start", 1)
                    end = arguments.get("paragraph_end", min(total, start + 39) if type(start) is int else 0)
                    if type(end) is int and end > total:
                        end, adjusted = total, True
                    locator = add_document_locator(db, rid, scope=scope, kind="word_paragraphs", paragraph_start=start, paragraph_end=end)
                    read_range = {"kind": "word_paragraphs", "paragraph_start": start, "paragraph_end": end,
                        **totals, "next_paragraph": end + 1 if end < total else None}
            elif doc["format"] in ("excel", "csv"):
                read_method = "retained_table_cells"
                if set(arguments) - {"source_revision_id", "sheet", "cell_range"}:
                    raise store.SourceError("表格请使用工作表和单元格范围定位")
                if not doc["sheets"]:
                    raise store.SourceError("此表格没有可读取工作表")
                sheet = arguments.get("sheet", doc["sheets"][0]["name"])
                if not arguments.get("cell_range"):
                    return json.dumps({"sheets": doc["sheets"], "text_read": False, "output_is_partial": True,
                        "instruction": "请根据实际范围提供sheet和cell_range读取原文"}, ensure_ascii=False)
                locator = add_document_locator(db, rid, scope=scope, kind="sheet_cells", sheet=sheet, cell_range=arguments["cell_range"])
                read_range = {"kind": "sheet_cells", **locator["location"], "sheets": available["sheets"]}
                from openpyxl.utils.cell import range_boundaries
                left, top, right, bottom = range_boundaries(arguments["cell_range"].upper())
                if (right - left + 1) * (bottom - top + 1) > 200:
                    raise store.SourceError("单次表格工具读取最多200格，以完整提供数值、公式和缓存状态；请缩小范围分批读取")
                selected_cells = [cell for block in doc["blocks"] if block.get("sheet") == sheet and top <= block["row_start"] <= bottom
                    for cell in block["cells"] if left <= range_boundaries(cell["cell"])[0] <= right]
                sheet_meta = next(s for s in doc["sheets"] if s["name"] == sheet)
                table_context = {"cells": selected_cells, "table_metadata": available.get("table_metadata"),
                    "selected_sheet": {**{key: sheet_meta[key] for key in ("name", "state", "row_count", "column_count", "formula_count") if key in sheet_meta},
                                       "selected_column_headers": sheet_meta.get("headers", {}).get("columns", [])[left - 1:right]},
                    "formula_cache_is_unverified": True, "formula_recalculation_performed": False,
                    "source_values_are_not_computation_results": True}
            else:
                raise store.SourceError("此版本尚不能按结构读取，请使用原件")
        if len(locator["quote"]) > 32_000:
            raise store.SourceError("定位原文超过单次读取限制，请缩小范围")
        evidence = {"source_id": record["source_id"], "revision_id": rid, "locator_id": locator["locator_id"],
            "source_title": record["title"], "source_kind": record["kind"], "source_location": locator["location"],
            "paper_id": "", "paper_title": record["title"], "chunk_id": "", "title": record["title"],
            "page": locator["location"].get("page_number", 0), "bbox": locator["location"].get("bbox"),
            "quote": locator["quote"], "score": 1.0, "trust": "untrusted_source_data"}
        return json.dumps({"source": {k: v for k, v in record.items() if k not in ("text", "raw_bytes")},
            "locator": locator, "evidence": [evidence] if locator["quote"].strip() else [],
            "read_range": read_range, "range_adjusted_to_actual_end": adjusted,
            "read_contract": {"version": "source_read_execution_v1", "method": read_method,
                "selected_text_returned_in_full": True, "original_visual_content_observed": False,
                "source_extraction_scope": available.get("extraction_scope"),
                "omitted_content": available.get("omitted_content", []),
                "extraction_accuracy_verified": False, "model_understanding_verified": False},
            **({"table_context": table_context} if table_context is not None else {}),
            "coverage_scope": "extracted_text_only", "source_directory": available,
            "output_is_partial": record["media_type"] == "application/pdf" and not bool(locator["quote"].strip()),
            "text_observed": bool(locator["quote"].strip()), "model_understanding_verified": False}, ensure_ascii=False)
    except store.SourceError as exc:
        return json.dumps({"error": str(exc), "code": "source_scope_or_content_invalid",
                           "available_range": available, "text_read": False}, ensure_ascii=False)
