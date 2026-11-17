"""旋转/越界水印 PDF 的解析兜底回归。"""
import pdfplumber

from app.parsing.pdfplumber_parser import _count_out_of_bounds, parse_pdf


def _make_watermark_pdf(path):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 11)
    c.drawString(50, 760, "Submission deadline: 2026-10-20.")
    c.saveState()
    c.setFont("Helvetica", 36)
    c.setFillGray(0.8)
    for y in range(120, 760, 140):
        c.saveState()
        c.translate(200, y)
        c.rotate(30)
        c.drawString(-320, 0, "WATERMARK WATERMARK WATERMARK")
        c.restoreState()
    c.restoreState()
    c.save()


def test_out_of_bounds_detection(tmp_path):
    pdf = tmp_path / "wm.pdf"
    _make_watermark_pdf(pdf)
    with pdfplumber.open(pdf) as doc:
        page = doc.pages[0]
        words = page.extract_words(use_text_flow=True, extra_attrs=["size"])
        oob = _count_out_of_bounds(words, page.width, page.height)
    assert oob / len(words) >= 0.10


def test_parse_falls_back_to_clean_text(tmp_path):
    pdf = tmp_path / "wm.pdf"
    _make_watermark_pdf(pdf)
    parsed = parse_pdf(pdf)
    assert parsed["needs_ocr"] is False
    page = parsed["pages"][0]
    assert "2026-10-20" in page["text"]
    assert "WATERMARK" not in page["text"]
    # 兜底正文为单一阅读顺序文本块
    assert [b["kind"] for b in page["blocks"] if b["kind"] == "text"]


def test_clean_pdf_has_no_watermark_residue(tmp_path):
    pdf = tmp_path / "wm.pdf"
    _make_watermark_pdf(pdf)
    parsed = parse_pdf(pdf)
    text = parsed["pages"][0]["text"]
    assert text.strip().startswith("Submission deadline")
