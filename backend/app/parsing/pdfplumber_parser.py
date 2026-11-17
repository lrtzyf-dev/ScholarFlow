"""
pdfplumber 解析（MIT 许可，商用友好）：提取页面文本块（含 bbox、字号）、表格、图注。
输出结构化 JSON：
{
  page_count, needs_ocr,
  pages: [{page, width, height, text, blocks: [{bbox, text, size, kind}]}]
}
扫描件（几乎无文本层）标记 needs_ocr=True，后续由 OCR/MinerU 插件处理。

内存控制：pdfplumber/pdfminer 的页面对象存在循环引用，仅靠分代 GC 回收；
若在单个 open 内遍历整本长文档，已遍历页对象会在 GC 前累积，峰值随总页数线性增长。
因此按页批次重新打开文档，每批处理完主动 gc.collect，使峰值只与批次大小相关。
"""
from __future__ import annotations

import gc
import json
import re
from pathlib import Path

import pdfplumber
import pypdfium2
from pdfminer.pdfparser import PDFException


class InvalidPdfError(Exception):
    """PDF 文件损坏或格式无法解析（属于客户端错误，API 应返回 400 而非 500）。"""


CAPTION_RE = re.compile(r"^(figure|fig\.?|table|图|表)\s*[\d一二三四五六七八九]", re.IGNORECASE)

# 整行由同一短语重复 ≥3 次构成（水印的典型形态）：捕获该短语作为水印单元
REPEATED_LINE = re.compile(r"^\s*(.{4,}?)(?:\s+\1){2,}\s*$")

# 每批处理的页数：任意时刻内存中最多保留该数量的 pdfplumber 页面对象
PAGE_BATCH = 20


def _pdfium_page_texts(pdf_path: Path) -> list[str]:
    """用 pypdfium2 逐页提取阅读顺序文本（其对水印/复杂版式的顺序更稳健）。"""
    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    finally:
        doc.close()


def _collect_watermark_units(pdfium_pages: list[str]) -> set[str]:
    """从整本文档的重复行中收集水印单元。"""
    units: set[str] = set()
    for text in pdfium_pages:
        for line in text.splitlines():
            match = REPEATED_LINE.match(line)
            if match:
                units.add(match.group(1).strip())
    return units


def _clean_pdfium_text(text: str, units: set[str]) -> str:
    """剔除水印行与仅由水印构成的行，保留正文与结构。"""
    kept: list[str] = []
    for line in text.splitlines():
        if REPEATED_LINE.match(line):
            continue
        stripped = line
        for unit in units:
            stripped = stripped.replace(unit, "")
        if stripped.strip():
            kept.append(line.rstrip())
    return "\n".join(kept).strip()


def _count_out_of_bounds(words: list[dict], width: float, height: float) -> int:
    """统计越界（负坐标或超出页面）的词数：水印常部分溢出页面。"""
    return sum(
        1
        for w in words
        if w["x0"] < -1 or w["top"] < -1 or w["x1"] > width + 1 or w["bottom"] > height + 1
    )


# 分栏检测的 x 网格步长，以及分栏间隙宽度的合理区间（pt）
_GUTTER_STEP = 5
_GUTTER_WINDOW = 20   # 滑动窗口宽度（pt）
_GUTTER_SIDE = 25     # 窗口两侧的验证范围（pt）
# 窗口内覆盖词数占比上限（低谷），以及两侧覆盖词数占比下限（两侧都密集）
_GUTTER_INSIDE_RATIO = 0.12
_GUTTER_SIDE_RATIO = 0.20


def _detect_column_gap(words: list[dict], width: float) -> float | None:
    """检测双栏布局的分栏间隙，返回间隙中心 x；单栏/证据不足返回 None。

    判据：在页面中部用滑动窗口统计词的 x 覆盖。双栏间隙处覆盖词极少（仅少量通栏
    标题/图表词），且窗口左右两侧覆盖都密集——尤其"右侧恢复"，可排除单栏文本
    结束后右侧空白（其右侧不再有词）的假间隙。
    """
    n = len(words)
    if n < 20:
        return None

    coverage: dict[int, int] = {}
    for w in words:
        start = int(w["x0"] // _GUTTER_STEP) * _GUTTER_STEP
        stop = int(w["x1"] // _GUTTER_STEP) * _GUTTER_STEP
        for x in range(start, stop, _GUTTER_STEP):
            coverage[x] = coverage.get(x, 0) + 1

    def _count(a: float, b: float) -> int:
        return sum(coverage.get(k, 0) for k in range(int(a // _GUTTER_STEP) * _GUTTER_STEP,
                                                      int(b // _GUTTER_STEP) * _GUTTER_STEP,
                                                      _GUTTER_STEP))

    best: tuple[int, float] | None = None
    for start in range(int(width * 0.30), int(width * 0.70) - _GUTTER_WINDOW, _GUTTER_STEP):
        inside = _count(start, start + _GUTTER_WINDOW)
        left = _count(start - _GUTTER_SIDE, start)
        right = _count(start + _GUTTER_WINDOW, start + _GUTTER_WINDOW + _GUTTER_SIDE)
        if (inside <= _GUTTER_INSIDE_RATIO * n
                and left >= _GUTTER_SIDE_RATIO * n
                and right >= _GUTTER_SIDE_RATIO * n):
            if best is None or inside < best[0]:
                best = (inside, start + _GUTTER_WINDOW / 2)
    return best[1] if best is not None else None


def _sort_reading_order(blocks: list[dict], gap: float) -> list[dict]:
    """双栏页按人类阅读顺序重排：通栏块切分水平区间，每个区间内先左栏后右栏。

    块 bbox=[x0, top, x1, bottom]。横跨间隙（x0<gap<x1）的块视为通栏（标题/跨栏图表）。
    """
    def classify(b: dict) -> str:
        x0, top, x1, bottom = b["bbox"]
        if x0 < gap < x1:
            return "span"
        return "L" if (x0 + x1) / 2 < gap else "R"

    tagged = [(classify(b), b) for b in blocks]
    spans = sorted((b for c, b in tagged if c == "span"), key=lambda b: b["bbox"][1])
    lefts = [b for c, b in tagged if c == "L"]
    rights = [b for c, b in tagged if c == "R"]

    ordered: list[dict] = []
    cursor = float("-inf")
    for sb in spans:
        stop = sb["bbox"][1]
        ordered.extend(sorted((b for b in lefts if cursor <= b["bbox"][1] < stop),
                              key=lambda b: b["bbox"][1]))
        ordered.extend(sorted((b for b in rights if cursor <= b["bbox"][1] < stop),
                              key=lambda b: b["bbox"][1]))
        ordered.append(sb)
        cursor = sb["bbox"][3]
    ordered.extend(sorted((b for b in lefts if b["bbox"][1] >= cursor), key=lambda b: b["bbox"][1]))
    ordered.extend(sorted((b for b in rights if b["bbox"][1] >= cursor), key=lambda b: b["bbox"][1]))
    return ordered


# 精确聚行的 top 容差（pt）：比常规聚类更严，避免左右栏 top 相近的行被误并
_TIGHT_LINE_TOL = 2.0


def _tight_lines(words: list[dict], tol: float = _TIGHT_LINE_TOL) -> list[tuple[float, list[dict]]]:
    """按 top 精确聚行（严容差），返回 (基准 top, 行词列表) 列表。"""
    lines: list[tuple[float, list[dict]]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]) <= tol:
            lines[-1][1].append(w)
        else:
            lines.append((w["top"], [w]))
    return lines


def _classify_line(ws: list[dict], gap: float) -> list[tuple[str, list[dict]]]:
    """判断一行：有词覆盖 gap 为通栏（span）；否则按 gap 拆成左/右行，行内按 x0 排序。"""
    if any(w["x0"] < gap < w["x1"] for w in ws):
        return [("span", sorted(ws, key=lambda w: w["x0"]))]
    left = sorted((w for w in ws if (w["x0"] + w["x1"]) / 2 < gap), key=lambda w: w["x0"])
    right = sorted((w for w in ws if (w["x0"] + w["x1"]) / 2 >= gap), key=lambda w: w["x0"])
    out: list[tuple[str, list[dict]]] = []
    if left:
        out.append(("L", left))
    if right:
        out.append(("R", right))
    return out


def _two_column_blocks(words: list[dict], gap: float) -> list[dict]:
    """双栏页成块：精确聚行 + gap 分类，各栏独立按垂直 gap 合并，返回输出格式块。

    通栏标题/图表（有词覆盖 gap）保持完整；左右栏（top 即使完全对齐）因 gap 处无词
    覆盖而被正确拆分，从根源避免栏间交错。块的阅读顺序由 _sort_reading_order 统一处理。
    """
    # cls -> 行单元 (top, bottom, text, size, x0, x1)
    by_cls: dict[str, list[tuple]] = {"span": [], "L": [], "R": []}
    for top, ws in _tight_lines(words):
        for cls, cws in _classify_line(ws, gap):
            by_cls[cls].append((
                top, max(w["bottom"] for w in cws),
                " ".join(w["text"] for w in cws),
                max(float(w.get("size", 0)) for w in cws),
                min(w["x0"] for w in cws), max(w["x1"] for w in cws)))

    blocks: list[tuple] = []
    for cls in ("span", "L", "R"):
        cur: tuple | None = None
        # 合并块元组 (cls, top, bottom, lines, x0, x1, size)
        for top, bottom, text, size, x0, x1 in sorted(by_cls[cls], key=lambda r: r[0]):
            if cur is not None and top - cur[2] <= max(3.0, 0.7 * size):
                cur = (cls, cur[1], bottom, cur[3] + [text],
                       min(cur[4], x0), max(cur[5], x1), max(cur[6], size))
            else:
                if cur is not None:
                    blocks.append(cur)
                cur = (cls, top, bottom, [text], x0, x1, size)
        if cur is not None:
            blocks.append(cur)

    out: list[dict] = []
    for cls, top, bottom, lines, x0, x1, size in blocks:
        text = "\n".join(lines)
        kind = "caption" if CAPTION_RE.match(text) else "text"
        out.append({"bbox": [round(x0, 2), round(top, 2), round(x1, 2), round(bottom, 2)],
                    "text": text, "size": round(size, 2), "kind": kind})
    return out


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


def _process_page(page, page_index: int, pdfium_text: str | None = None,
                  watermark_units: set[str] | None = None) -> dict:
    """解析单页：先抽表格（排除表格词），再聚类正文词为块，返回与原 schema 一致的 page。

    若检测到水印/版式污染（越界词占比高或出现重复水印行），正文改用 pypdfium2
    的干净阅读顺序文本，表格/图注仍由 pdfplumber 提供。
    """
    width, height = float(page.width), float(page.height)

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

    # 图片区域客观检测：仅标记位置（不提取图像内容），过滤过小图标
    figure_blocks: list[dict] = []
    for img in getattr(page, "images", []) or []:
        if img["width"] < 20 or img["height"] < 20:
            continue
        figure_blocks.append({
            "bbox": [round(img["x0"], 2), round(img["top"], 2),
                     round(img["x1"], 2), round(img["bottom"], 2)],
            "text": "[image]", "size": 0.0, "kind": "figure",
        })

    all_words = page.extract_words(use_text_flow=True,
                                   extra_attrs=["size", "upright"]) or []

    def _in_table(w: dict) -> bool:
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        return any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in table_bboxes)

    # 过滤表格词与旋转/竖排文本（upright=False），避免竖排章节标题的散落字符混入正文
    words = [w for w in all_words if not _in_table(w) and w.get("upright", True)]

    # 污染判定：越界词占比 ≥10%，或本页 pdfium 文本含重复水印行
    oob = _count_out_of_bounds(words, width, height)
    has_watermark_line = bool(
        pdfium_text and any(REPEATED_LINE.match(line) for line in pdfium_text.splitlines())
    )
    contaminated = (words and oob / len(words) >= 0.10) or has_watermark_line

    body_blocks: list[dict] = []
    gap: float | None = None
    if contaminated and pdfium_text:
        clean_text = _clean_pdfium_text(pdfium_text, watermark_units or set())
        if clean_text:
            # 正文主体字号取页内未越界词的中位字号，缺省给 10.5
            in_bounds_sizes = sorted(
                float(w["size"])
                for w in words
                if not (w["x0"] < -1 or w["top"] < -1 or w["x1"] > width + 1 or w["bottom"] > height + 1)
            )
            body_size = round(in_bounds_sizes[len(in_bounds_sizes) // 2], 2) if in_bounds_sizes else 10.5
            body_blocks.append({
                "bbox": [0.0, 0.0, round(width, 2), round(height, 2)],
                "text": clean_text, "size": body_size, "kind": "text",
            })
    else:
        gap = _detect_column_gap(words, width)
        if gap is not None:
            # 双栏：精确聚行 + gap 分类，各栏独立合并，从根源避免栏间交错
            body_blocks = _two_column_blocks(words, gap)
        else:
            # 单栏：常规按 top 聚行 + 垂直 gap 合并
            for b in _lines_to_blocks(_cluster_lines(words)):
                text = b["text"].strip()
                if not text:
                    continue
                kind = "caption" if CAPTION_RE.match(text) else "text"
                body_blocks.append({"bbox": [round(b["x0"], 2), round(b["top"], 2),
                                             round(b["x1"], 2), round(b["_bottom"], 2)],
                                    "text": text, "size": round(b["size"], 2), "kind": kind})

    blocks = body_blocks + table_blocks + figure_blocks
    if gap is not None:
        # 双栏：按通栏区间内先左后右的阅读顺序重排（含表格/图块）
        blocks = _sort_reading_order(blocks, gap)
    else:
        blocks.sort(key=lambda b: (round(b["bbox"][1]), b["bbox"][0]))

    return {
        "page": page_index + 1,
        "width": round(width, 2),
        "height": round(height, 2),
        "text": "\n".join(b["text"] for b in blocks),
        "blocks": blocks,
    }


def parse_pdf(pdf_path: str | Path, out_path: str | Path | None = None,
              page_batch: int = PAGE_BATCH) -> dict:
    pdf_path = Path(pdf_path)

    try:
        # 先取 pypdfium2 全文：用于水印单元识别与污染页的干净文本兜底
        pdfium_pages = _pdfium_page_texts(pdf_path)
        watermark_units = _collect_watermark_units(pdfium_pages)

        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
    except (pypdfium2.PdfiumError, PDFException) as exc:
        # 文件损坏/语法错误属于客户端问题，统一转换，由 API 层返回 400
        raise InvalidPdfError(f"PDF 文件损坏或无法解析：{exc}") from exc

    pages: list[dict] = []
    for start in range(0, total, page_batch):
        # 每批重新打开文档，with 结束即关闭；随后主动 GC 回收该批循环引用对象
        with pdfplumber.open(pdf_path) as pdf:
            for page_index in range(start, min(start + page_batch, total)):
                pdfium_text = pdfium_pages[page_index] if page_index < len(pdfium_pages) else None
                pages.append(_process_page(pdf.pages[page_index], page_index,
                                           pdfium_text, watermark_units))
        gc.collect()

    empty_pages = sum(1 for p in pages if len(p["text"].strip()) < 20)
    # 空页（0-based）索引：无论是否过半，都交由上层逐页补 OCR，
    # 避免"一半文本页一半扫描页"因未过半而永久缺失文本。
    blank_page_indices = [
        i for i, p in enumerate(pages) if len(p["text"].strip()) < 20
    ]
    # 存在空页即需要 OCR 补全（不再要求严格过半）；具体页由 blank_page_indices 给出。
    needs_ocr = len(pages) > 0 and len(blank_page_indices) > 0
    result = {
        "page_count": len(pages),
        "needs_ocr": needs_ocr,
        "blank_page_indices": blank_page_indices,
        "empty_page_count": empty_pages,
        "pages": pages,
    }

    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
