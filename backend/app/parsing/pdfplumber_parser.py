"""
pdfplumber 解析（MIT 许可，商用友好）：提取页面文本块（含 bbox、字号）、表格、图注。
输出结构化 JSON：
{
  page_count, needs_ocr,
  pages: [{page, width, height, text, blocks: [{bbox, text, size, kind}]}]
}
扫描件（几乎无文本层）标记 needs_ocr=True，后续由 MinerU 插件处理。

与早期 pymupdf_parser 输出 schema 完全一致，仅更换底层实现。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pdfplumber

CAPTION_RE = re.compile(r"^(figure|fig\.?|table|图|表)\s*[\d一二三四五六七八九]", re.IGNORECASE)


def _cluster_lines(words: list[dict]) -> list[list[dict]]:
    """将词按行聚类（top 相近视为同一行）。"""
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines:
            last = lines[-1][-1]
            tol = max(1.5, 0.6 * float(last.get("size", 10)))
            if abs(w["top"] - last["top"]) <= tol:
                lines[-1].append(w)
                continue
        lines.append([w])
    return lines


def _lines_to_blocks(lines: list[list[dict]]) -> list[dict]:
    """相邻行按垂直间距合并为块，块内行间换行。"""
    blocks: list[dict] = []
    for line_words in lines:
        line_words = sorted(line_words, key=lambda w: w["x0"])
        top = min(w["top"] for w in line_words)
        bottom = max(w["bottom"] for w in line_words)
        size = max(float(w.get("size", 0)) for w in line_words)
        text = " ".join(w["text"] for w in line_words).strip()
        if not text:
            continue
        line = {"text": text, "x0": min(w["x0"] for w in line_words), "x1": max(w["x1"] for w in line_words),
                "top": top, "bottom": bottom, "size": size}
        if blocks:
            gap = top - blocks[-1]["_bottom"]
            block_size = blocks[-1]["size"]
            if gap <= max(3.0, 0.7 * block_size):
                blocks[-1]["text"] = blocks[-1]["text"] + "\n" + text
                blocks[-1]["x0"] = min(blocks[-1]["x0"], line["x0"])
                blocks[-1]["x1"] = max(blocks[-1]["x1"], line["x1"])
                blocks[-1]["_bottom"] = max(blocks[-1]["_bottom"], bottom)
                blocks[-1]["size"] = max(block_size, size)
                continue
        blocks.append({"text": text, "x0": line["x0"], "x1": line["x1"], "top": top,
                       "_bottom": bottom, "size": size})
    return blocks


def parse_pdf(pdf_path: str | Path, out_path: str | Path | None = None) -> dict:
    pdf_path = Path(pdf_path)
    pages: list[dict]

    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for page_index, page in enumerate(pdf.pages):
            # 先抽表格，得到表格区域，避免表格词混入正文块
            table_bboxes: list[tuple] = []
            table_blocks: list[dict] = []
            try:
                for table in page.find_tables():
                    rows = table.extract()
                    bbox = tuple(table.bbox)
                    table_bboxes.append(bbox)
                    if not rows:
                        continue
                    md = "\n".join("| " + " | ".join((c or "").replace("\n", " ") for c in row) + " |"
                                   for row in rows)
                    table_blocks.append({"bbox": [round(v, 2) for v in bbox], "text": md,
                                         "size": 0.0, "kind": "table"})
            except Exception:
                pass

            words = page.extract_words(use_text_flow=True, extra_attrs=["size"]) or []

            def _in_table(w: dict) -> bool:
                cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
                return any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in table_bboxes)

            words = [w for w in words if not _in_table(w)]
            raw_blocks = _lines_to_blocks(_cluster_lines(words))

            blocks: list[dict] = []
            for b in raw_blocks:
                text = b["text"].strip()
                if not text:
                    continue
                kind = "caption" if CAPTION_RE.match(text) else "text"
                blocks.append({"bbox": [round(b["x0"], 2), round(b["top"], 2),
                                        round(b["x1"], 2), round(b["_bottom"], 2)],
                               "text": text, "size": round(b["size"], 2), "kind": kind})
            blocks.extend(table_blocks)
            blocks.sort(key=lambda b: (round(b["bbox"][1]), b["bbox"][0]))

            pages.append(
                {
                    "page": page_index + 1,
                    "width": round(float(page.width), 2),
                    "height": round(float(page.height), 2),
                    "text": "\n".join(b["text"] for b in blocks),
                    "blocks": blocks,
                }
            )

    empty_pages = sum(1 for p in pages if len(p["text"].strip()) < 20)
    result = {
        "page_count": len(pages),
        "needs_ocr": len(pages) > 0 and empty_pages > max(1, len(pages) // 2),
        "pages": pages,
    }

    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
