"""扫描件 OCR 测试：探测、降级、坐标换算、过滤与真实引擎端到端。"""
import numpy as np

from app.parsing import ocr
from app.parsing.ocr import OcrUnavailable, normalize_items, ocr_pdf


def test_available_engine_is_detected():
    # 当前环境已安装 rapidocr-onnxruntime
    assert ocr.available_engine() == "rapidocr"
    assert ocr.is_available() is True


def test_ocr_pdf_raises_when_no_engine(monkeypatch, sample_pdf):
    monkeypatch.setattr(ocr, "_detect_engine", lambda: None)
    assert ocr.available_engine() is None
    try:
        ocr_pdf(sample_pdf)
    except OcrUnavailable as exc:
        assert "rapidocr" in str(exc)
    else:
        raise AssertionError("无引擎时应抛 OcrUnavailable")


def test_normalize_items_scales_pixel_bbox_to_pdf_points():
    scale = 200 / 72.0
    raw = [
        [
            [[100, 100], [200, 100], [200, 150], [100, 150]],
            "hello world",
            "0.90",
        ]
    ]
    blocks = normalize_items(raw, scale, min_score=0.5)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["kind"] == "ocr"
    assert b["text"] == "hello world"
    assert b["score"] == 0.9
    assert b["bbox"] == [36.0, 36.0, 72.0, 54.0]


def test_normalize_items_filters_low_score_and_empty_text():
    scale = 2.0
    raw = [
        [[[0, 0], [10, 0], [10, 10], [0, 10]], "low", "0.30"],
        [[[0, 0], [10, 0], [10, 10], [0, 10]], "  ", "0.95"],
        [[[0, 0], [10, 0], [10, 10], [0, 10]], "keep", "0.80"],
    ]
    blocks = normalize_items(raw, scale, min_score=0.5)
    assert [b["text"] for b in blocks] == ["keep"]


def test_ocr_pdf_injected_runner_returns_ocr_blocks(sample_pdf):
    scale = 200 / 72.0

    def fake_runner(array):
        assert isinstance(array, np.ndarray)
        # 像素坐标，位于渲染图左上区域
        return [
            [
                [[72, 72], [288, 72], [288, 144], [72, 144]],
                "scanned line",
                "0.92",
            ]
        ]

    result = ocr_pdf(sample_pdf, runner=fake_runner)
    assert result["needs_ocr"] is True
    assert result["ocr_engine"] == "rapidocr"
    assert result["page_count"] == 2
    # 每个页面都经过 runner（sample_pdf 共 2 页）
    assert len(result["pages"]) == 2
    first = result["pages"][0]
    assert first["page"] == 1
    assert first["blocks"][0]["kind"] == "ocr"
    assert first["text"] == "scanned line"
    # 像素 x=72..288 换算回 PDF 点
    assert first["blocks"][0]["bbox"] == [
        round(72 / scale, 2),
        round(72 / scale, 2),
        round(288 / scale, 2),
        round(144 / scale, 2),
    ]


def test_real_engine_recognizes_rendered_text(tmp_path):
    """真实 RapidOCR 端到端：生成含文字 PDF，验证离线识别出文本。"""
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "scanned.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=(595, 842))
    c.setFont("Helvetica", 16)
    c.drawString(70, 760, "Attention Is All You Need")
    c.save()

    result = ocr_pdf(pdf_path)
    all_text = " ".join(p["text"] for p in result["pages"]).lower()
    assert "attention" in all_text
    assert all(b["kind"] == "ocr" for p in result["pages"] for b in p["blocks"])
