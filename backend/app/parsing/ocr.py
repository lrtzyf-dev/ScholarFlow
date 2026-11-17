"""
扫描件 OCR：可插拔后端，默认 RapidOCR（Apache-2.0，纯 pip、离线、自带中英文模型）。

- 渲染：pypdfium2（BSD）逐页渲染为 numpy 数组，处理完即释放，避免一次性载入整本。
- 坐标：引擎输出的是渲染像素坐标，按 scale = dpi / 72 换算回 PDF 点坐标，
  与 pdfplumber_parser 的 bbox 口径保持一致。
- 降级：无可用引擎时抛 OcrUnavailable，由调用方显式标注，绝不静默当作成功。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

import numpy as np

DEFAULT_OCR_DPI = 200
DEFAULT_MIN_SCORE = 0.5
# 整本 OCR 结果至少应识别出的有效正文字符数：低于该值视为识别失败而非成功。
# 取 20 个字符（与空页判定阈值对齐，约 3~4 个词的下限），与具体样本无关的通用工程阈值。
DEFAULT_MIN_OCR_CHARS = 20

# runner 接收一页的 numpy 图像，返回 RapidOCR 风格的原始条目 [[box, text, score], ...]
OcrRunner = Callable[[np.ndarray], list]


class OcrUnavailable(RuntimeError):
    """没有任何可用 OCR 引擎。"""


def _detect_engine() -> str | None:
    """按优先级探测可用引擎，只做可导入性/可用性检查，不触发模型加载。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return "rapidocr"
    except Exception:
        pass
    try:
        import pytesseract  # noqa: F401

        if pytesseract.get_tesseract_version() is not None:
            return "tesseract"
    except Exception:
        pass
    return None


def available_engine() -> str | None:
    """返回当前可用引擎名（"rapidocr" / "tesseract"），无则 None。"""
    return _detect_engine()


def is_available() -> bool:
    return _detect_engine() is not None


_rapidocr_instance = None


def _get_rapidocr():
    """RapidOCR 模型实例惰性单例（初始化需加载模型，避免重复开销）。"""
    global _rapidocr_instance
    if _rapidocr_instance is None:
        from rapidocr_onnxruntime import RapidOCR

        _rapidocr_instance = RapidOCR()
    return _rapidocr_instance


def _run_rapidocr(array: np.ndarray) -> list:
    result, _elapse = _get_rapidocr()(array)
    return result or []


def normalize_items(raw: Sequence, scale: float, min_score: float) -> list[dict]:
    """把引擎原始条目转换为统一 block，并把像素 bbox 换算为 PDF 点坐标。"""
    blocks: list[dict] = []
    for entry in raw:
        box, text, raw_score = entry[0], entry[1], entry[2]
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 0.0
        text = str(text).strip()
        if not text or score < min_score:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        bbox = [
            round(min(xs) / scale, 2),
            round(min(ys) / scale, 2),
            round(max(xs) / scale, 2),
            round(max(ys) / scale, 2),
        ]
        blocks.append(
            {
                "bbox": bbox,
                "text": text,
                "size": 0.0,
                "kind": "ocr",
                "score": round(score, 4),
            }
        )
    return blocks


def ocr_pdf(
    pdf_path: str | Path,
    *,
    dpi: int = DEFAULT_OCR_DPI,
    page_indices: Sequence[int] | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    runner: OcrRunner | None = None,
) -> dict:
    """
    对扫描件 PDF 逐页 OCR，返回与 pdfplumber_parser.parse_pdf 同构的 pages，
    并额外标注 ocr_engine。无引擎时抛 OcrUnavailable。
    """
    engine_name = _detect_engine()
    if engine_name is None:
        raise OcrUnavailable(
            "未检测到 OCR 引擎。离线识别扫描件可执行 "
            "`pip install rapidocr-onnxruntime`；或安装 Tesseract 后 "
            "`pip install pytesseract`。"
        )

    import pypdfium2 as pdfium

    scale = dpi / 72.0
    run = runner or _run_rapidocr
    pdf = pdfium.PdfDocument(str(Path(pdf_path)))
    total = len(pdf)
    indices = list(range(total)) if page_indices is None else list(page_indices)

    pages: list[dict] = []
    for idx in indices:
        page = pdf[idx]
        width, height = (float(v) for v in page.get_size())
        bitmap = page.render(scale=scale)
        array = np.asarray(bitmap.to_pil())
        blocks = normalize_items(run(array), scale, min_score)
        blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        pages.append(
            {
                "page": idx + 1,
                "width": round(width, 2),
                "height": round(height, 2),
                "text": "\n".join(b["text"] for b in blocks),
                "blocks": blocks,
            }
        )
        del bitmap, array

    return {
        "page_count": total,
        "needs_ocr": True,
        "ocr_engine": engine_name,
        "pages": pages,
    }


def effective_text_chars(pages: Sequence) -> int:
    """统计解析/OCR 页面中有效正文的字符总数（忽略空白）。"""
    total = 0
    for page in pages:
        total += len("".join(str(page.get("text", "")).split()))
    return total


def has_usable_text(parsed: dict, min_chars: int = DEFAULT_MIN_OCR_CHARS) -> bool:
    """判断 OCR 结果是否真的识别出足够正文。

    引擎可用不代表识别成功：纯图片/模糊扫描件可能返回 0 个达标条目。
    仅当有效正文达到最小字符数才视为成功，否则调用方应失败关闭。
    """
    return effective_text_chars(parsed.get("pages", [])) >= min_chars
