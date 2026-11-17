"""Compare real export content to fixed inputs, independently of its renderer.

No file is rendered and no analysis program is executed here. DOCX expectations
come from Markdown blocks/source snapshots; SVG expectations describe the fixed
CSV-to-mark geometry rather than trusting an output's self-reported checksum.
"""
from __future__ import annotations

import io
import json
import math
import re
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"


def _xml(node):
    return (node.tag, tuple(sorted(node.attrib.items())), node.text or "", tuple(_xml(child) for child in node))


def _parts(values):
    result = []
    for value in values:
        if value == "":
            continue
        if isinstance(value, str) and result and isinstance(result[-1], str):
            result[-1] += value
        else:
            result.append(value)
    return result


def _paragraph_parts(paragraph):
    values = []
    def visit(node):
        if node.tag == M + "oMath":
            values.append(_xml(node))
            return
        if node.tag in {W + name for name in ("instrText", "fldChar", "altChunk", "object", "drawing")}:
            raise ValueError("unexpected dynamic document content")
        if node.tag == W + "t":
            values.append(node.text or "")
        elif node.tag == W + "br":
            values.append("\n")
        elif node.tag == W + "tab":
            values.append("\t")
        for child in node:
            visit(child)
    visit(paragraph)
    return _parts(values)


def _actual_docx(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(W + "body")
    result = []
    def paragraph(node):
        style = node.find(W + "pPr/" + W + "pStyle")
        return {"style": style.get(W + "val") if style is not None else None,
                "parts": _paragraph_parts(node)}
    for node in body:
        if node.tag == W + "p":
            result.append({"paragraph": paragraph(node)})
        elif node.tag == W + "tbl":
            rows = []
            for row in node.findall(W + "tr"):
                cells = []
                for cell in row.findall(W + "tc"):
                    if cell.find(".//" + W + "gridSpan") is not None or cell.find(".//" + W + "vMerge") is not None:
                        raise ValueError("unexpected merged source table")
                    cells.append([paragraph(p) for p in cell.findall(W + "p")])
                rows.append(cells)
            result.append({"table": rows})
        elif node.tag != W + "sectPr":
            raise ValueError("unexpected document body block")
    return result


def _expected_docx(title, content, evidence):
    """Limited documented Markdown contract; never consumes renderer output."""
    from markdown_it import MarkdownIt
    from .word_math import latex_math
    prefix = "SFVerifyMath" + __import__("uuid").uuid4().hex + "X"
    math_values = {}
    # Keep code literal while protecting the four supported math delimiters.
    pattern = re.compile(r'(^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\2[ \t]*$|`+[^`\n]*`+)|(\$\$[\s\S]+?\$\$|(?<![\\$])\$[^$\n]+?(?<!\\)\$(?!\$)|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])', re.MULTILINE)
    def protect(match):
        if match.group(1):
            return match[0]
        original = match.group(3)
        expression = original[2:-2] if original.startswith(("$$", "\\(", "\\[")) else original[1:-1]
        key = prefix + str(len(math_values)) + "END"
        try:
            expected = _xml(latex_math(expression.strip()))
        except Exception:
            expected = "公式原文（未转换）：" + expression
        math_values[key] = (original, expected)
        return key
    protected = pattern.sub(protect, content)
    refs = {str(e.get("id")) for e in evidence
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", str(e.get("id") or ""))}
    def plain(text):
        result = []
        for part in re.split("(" + prefix + r"\d+END)", text):
            if part in math_values:
                result.append(math_values[part][1])
            else:
                result.append(re.sub(r"\[\^([A-Za-z][A-Za-z0-9_-]{0,99})\]",
                    lambda match: "[" + match[1] + "]" if match[1] in refs else match[0], part))
        return result
    def literal(text):
        return re.sub(prefix + r"\d+END", lambda match: math_values[match[0]][0], text)
    def inline(token):
        result = []
        for child in token.children or []:
            if child.type == "text":
                result.extend(plain(child.content))
            elif child.type == "code_inline":
                result.append(literal(child.content))
            elif child.type in ("softbreak", "hardbreak"):
                result.append("\n")
            elif child.type == "image":
                result.append("[图片：" + child.content + "，原始链接见 Markdown]")
            elif child.type == "link_open":
                href = child.attrGet("href") or ""
                if href.startswith(("https://", "http://")):
                    result.append("(" + href + ") ")
        return _parts(result)
    result = [{"paragraph": {"style": "Title", "parts": [title]}}]
    lists, paragraph, table, row, cell = [], None, None, None, None
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(protected)
    for token in tokens:
        if token.type in ("bullet_list_open", "ordered_list_open"):
            lists.append(token.type)
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            if lists:
                lists.pop()
        elif token.type in ("heading_open", "paragraph_open"):
            style = ("Heading" + str(min(6, int(token.tag[1:])))) if token.type == "heading_open" else (
                ("ListNumber" if lists[-1] == "ordered_list_open" else "ListBullet") if lists else None)
            paragraph = {"style": style, "parts": []}
            if cell is not None:
                cell.append(paragraph)
            else:
                result.append({"paragraph": paragraph})
        elif token.type == "inline":
            if paragraph is None:
                paragraph = {"style": None, "parts": []}
                if cell is not None:
                    cell.append(paragraph)
                else:
                    result.append({"paragraph": paragraph})
            paragraph["parts"] = _parts([*paragraph["parts"], *inline(token)])
        elif token.type in ("heading_close", "paragraph_close"):
            paragraph = None
        elif token.type in ("fence", "code_block"):
            result.append({"paragraph": {"style": None, "parts": _parts([literal(token.content).rstrip("\n")])}})
            paragraph = None
        elif token.type == "table_open":
            table = []
            result.append({"table": table})
        elif token.type == "tr_open" and table is not None:
            row = []
            table.append(row)
        elif token.type in ("th_open", "td_open") and row is not None:
            cell = []
            row.append(cell)
            paragraph = None
        elif token.type in ("th_close", "td_close"):
            if cell == []:
                cell.append({"style": None, "parts": []})
            cell, paragraph = None, None
        elif token.type == "table_close":
            table, row, cell, paragraph = None, None, None, None
    if evidence:
        result.append({"paragraph": {"style": "Heading2", "parts": ["参考文献"]}})
    def add(text):
        result.append({"paragraph": {"style": None, "parts": _parts([text])}})
    verification = {"source_frozen_semantics_unverified": "来源版本已固定，结论仍待核验",
        "unverified": "未核验", "verified": "已核验", "supported": "支持", "conflict": "存在冲突",
        "not_found": "本次未找到", "not_processed": "未处理"}
    for index, item in enumerate(evidence, 1):
        identifier = str(item.get("id") or "")
        identifier = identifier if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", identifier) else str(index)
        add(f"[{identifier}] . {item.get('source_title') or item.get('paper_title') or '来源'}." + (
            " " + item["url"] if item.get("url") else ""))
        if item.get("revision_id"):
            state = item.get("verification", "unverified")
            add(f"来源版本：{item['revision_id']}；定位：{item.get('locator_id') or '整份资料'}；核验状态：{verification.get(state, state or '未核验')}")
        if item.get("quote"):
            add("原文：" + item["quote"])
        if item.get("source_fingerprint"):
            add("来源指纹：" + item["source_fingerprint"])
        if item.get("location"):
            add("原文位置：" + json.dumps(item["location"], ensure_ascii=False, sort_keys=True))
        if item.get("revision_id"):
            add("原件未包含；需在有访问权限的研序实例中按来源指纹查找原件。")
    return result


def verify_docx(payload, title, rev):
    expected = _expected_docx(title, rev.content, rev.evidence)
    actual = _actual_docx(payload)
    if actual != expected:
        raise ValueError("DOCX actual body/table/math/source blocks differ from fixed Markdown inputs")
    return {"fixed_input_blocks_checked": len(expected), "fixed_input_tables_checked": sum("table" in b for b in expected)}


def analysis_expected(rev, numeric, statistics):
    """Independent fixed-input values, without running the exported program."""
    names = rev.analysis["numeric_columns"]
    points = [{"category": values[0], "values": dict(zip(names, values[1:]))} for values in numeric[1:]]
    stats = {values[0]: dict(zip(("count", "missing", "min", "mean", "max", "unit"), values[1:]))
             for values in statistics[1:]}
    warnings = []
    if any(values["missing"] for values in stats.values()):
        warnings.append("Missing values are gaps, never zero or imputed")
    if len(points) > 200:
        warnings.append("SVG preview contains first 200 rows; statistics and result JSON contain all rows")
    visible = points[:200]
    if not any(value is not None for point in visible for value in point["values"].values()):
        warnings.append("The displayed rows contain no finite values; preview is empty, full-data statistics remain available")
    if rev.analysis["chart_type"] == "radar":
        if len(visible) > 30:
            warnings.append("Radar SVG shows first 30 rows to preserve legibility")
        warnings.append("Radar axes use per-column min/max normalization; original values and units are retained in result JSON")
    from .artifact_store import digest
    return {"row_count": len(points), "points": points, "stats": stats, "warnings": warnings,
        "method": "Descriptive finite numeric statistics; no inference, imputation or causal analysis",
        "data_sha256": digest(rev.content)}


def verify_svg(payload, config, expected):
    """Verify every visible mark/label against the selected source's mapping."""
    root = ET.fromstring(payload)
    def node(tag, attributes, text="", children=()):
        return (tag, tuple(sorted((key, str(value)) for key, value in attributes.items())), text, tuple(children))
    def actual(element):
        return node(element.tag.rsplit("}", 1)[-1], element.attrib, element.text or "", [actual(c) for c in element])
    want = [node("rect", {"width": 1000, "height": 600, "fill": "white"}),
            node("text", {"x": 60, "y": 30, "font-size": 18}, config.get("title", "Data chart"))]
    names, stats = config["numeric_columns"], expected["stats"]
    visible = expected["points"][:200]
    colors = ["#355cf6", "#7699eb", "#526b96", "#0e8174", "#a55738"]
    finite = [v for p in visible for v in p["values"].values() if v is not None]
    low, high = min(0.0, min(finite, default=0.0)), max(0.0, max(finite, default=0.0))
    span = (high - low) or 1.0
    if not math.isfinite(span):
        raise ValueError("nonfinite chart range")
    def y(value):
        return 500 - (value - low) / span * 420
    if config["chart_type"] == "radar":
        for index, name in enumerate(names):
            angle = -math.pi / 2 + 2 * math.pi * index / len(names)
            x, yy = 500 + 210 * math.cos(angle), 290 + 210 * math.sin(angle)
            want.extend([node("line", {"x1": 500, "y1": 290, "x2": f"{x:.3f}", "y2": f"{yy:.3f}", "stroke": "#ccd2dc"}),
                node("text", {"x": f"{x:.3f}", "y": f"{yy:.3f}", "font-size": 12}, name)])
        for index, point in enumerate(visible[:30]):
            if any(v is None for v in point["values"].values()):
                continue
            coordinates = []
            for ni, name in enumerate(names):
                denominator = stats[name]["max"] - stats[name]["min"]
                fraction = (point["values"][name] - stats[name]["min"]) / denominator if denominator else .5
                angle = -math.pi / 2 + 2 * math.pi * ni / len(names)
                coordinates.append(f"{500 + 210 * fraction * math.cos(angle):.3f},{290 + 210 * fraction * math.sin(angle):.3f}")
            want.append(node("polygon", {"points": " ".join(coordinates), "fill": "none", "stroke": colors[index % len(colors)]},
                children=[node("title", {}, point["category"])]))
    else:
        want.append(node("line", {"x1": 60, "y1": f"{y(0):.3f}", "x2": 960, "y2": f"{y(0):.3f}", "stroke": "#6a7485"}))
        group_width = 900 / max(1, len(visible))
        for ni, name in enumerate(names):
            color = colors[ni % len(colors)]
            segments, current = [], []
            for index, point in enumerate(visible):
                value = point["values"][name]
                if value is None:
                    if current:
                        segments.append(current)
                    current = []
                    continue
                x, yy = 60 + (index + .5) * group_width, y(value)
                if config["chart_type"] == "bar":
                    width = group_width * .8 / len(names)
                    want.append(node("rect", {"x": f"{x - group_width * .4 + ni * width:.3f}", "y": f"{min(y(0), yy):.3f}",
                        "width": f"{width:.3f}", "height": f"{abs(y(0) - yy):.3f}", "fill": color},
                        children=[node("title", {}, point["category"] + " / " + name + ": " + str(value))]))
                else:
                    current.append(f"{x:.3f},{yy:.3f}")
                    want.append(node("circle", {"cx": f"{x:.3f}", "cy": f"{yy:.3f}", "r": 2, "fill": color}))
            if current:
                segments.append(current)
            for segment in segments:
                want.append(node("polyline", {"points": " ".join(segment), "fill": "none", "stroke": color, "stroke-width": 2}))
            want.append(node("text", {"x": 60 + ni * 140, "y": 565, "fill": color, "font-size": 12}, name))
        for index, point in enumerate(visible):
            if index % max(1, len(visible) // 12) == 0:
                want.append(node("text", {"x": f"{60 + (index + .5) * group_width:.3f}", "y": 525, "font-size": 11}, point["category"][:24]))
        want.extend([node("text", {"x": 8, "y": 80, "font-size": 11}, str(high)),
            node("text", {"x": 8, "y": 500, "font-size": 11}, str(low))])
    if actual(root) != node("svg", {"viewBox": "0 0 1000 600", "role": "img"}, children=want):
        raise ValueError("SVG actual marks/labels differ from fixed CSV/config mapping")
    return {"fixed_input_visible_rows_checked": min(len(expected["points"]), 30 if config["chart_type"] == "radar" else 200),
        "fixed_input_total_rows": len(expected["points"]), "missing_policy": "gap"}
