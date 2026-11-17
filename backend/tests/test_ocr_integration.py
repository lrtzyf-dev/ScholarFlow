"""扫描件 OCR 入库集成测试：无文本层 PDF 的成功路径与无引擎降级路径。"""
import json

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.models import Chunk, Paper
from app.parsing.ocr import OcrUnavailable
from app.pipeline.indexer import ingest_pdf


def _make_scanned_pdf(path) -> str:
    """生成只含整页图像、无文本层的 PDF（模拟扫描件）。"""
    img = Image.new("RGB", (500, 700), "white")
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.drawImage(ImageReader(img), 0, 0, width=595, height=842)
    c.showPage()
    c.save()
    return str(path)


def test_scanned_pdf_ocr_success_is_parsed(db, tmp_path):
    pdf_path = _make_scanned_pdf(tmp_path / "scanned.pdf")
    paper = Paper(title="scanned.pdf", pdf_path=pdf_path, content_hash="scan-1")
    db.add(paper)
    db.commit()

    def fake_ocr_pdf(target, **kwargs):
        return {
            "page_count": 1,
            "needs_ocr": True,
            "ocr_engine": "rapidocr",
            "pages": [
                {
                    "page": 1,
                    "width": 595,
                    "height": 842,
                    "text": "OCR recognized text here",
                    "blocks": [
                        {
                            "bbox": [50, 700, 300, 720],
                            "text": "OCR recognized text here",
                            "size": 0.0,
                            "kind": "ocr",
                            "score": 0.9,
                        }
                    ],
                }
            ],
        }

    import app.parsing.ocr as ocr_module

    original = ocr_module.ocr_pdf
    ocr_module.ocr_pdf = fake_ocr_pdf
    try:
        ingest_pdf(db, paper)
    finally:
        ocr_module.ocr_pdf = original

    db.refresh(paper)
    assert paper.status == "parsed"
    assert paper.error == ""
    chunks = db.query(Chunk).filter(Chunk.paper_id == paper.id).all()
    assert len(chunks) >= 1
    assert any("OCR recognized" in c.content for c in chunks)
    with open(paper.parsed_path, encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["ocr_engine"] == "rapidocr"


def test_scanned_pdf_without_engine_marks_needs_ocr(db, tmp_path, monkeypatch):
    pdf_path = _make_scanned_pdf(tmp_path / "scanned2.pdf")
    paper = Paper(title="scanned2.pdf", pdf_path=pdf_path, content_hash="scan-2")
    db.add(paper)
    db.commit()

    def fake_raise(target, **kwargs):
        raise OcrUnavailable("未检测到 OCR 引擎。请 pip install rapidocr-onnxruntime。")

    monkeypatch.setattr("app.parsing.ocr.ocr_pdf", fake_raise)

    ingest_pdf(db, paper)
    db.refresh(paper)
    assert paper.status == "needs_ocr"
    assert "rapidocr" in paper.error
    assert db.query(Chunk).filter(Chunk.paper_id == paper.id).count() == 0


def test_scanned_pdf_empty_ocr_result_fails_closed(db, tmp_path, monkeypatch):
    """引擎可用但 OCR 返回 0 个有效条目：应标记待 OCR，而非谎报已解析。"""
    pdf_path = _make_scanned_pdf(tmp_path / "blank_ocr.pdf")
    paper = Paper(title="blank_ocr.pdf", pdf_path=pdf_path, content_hash="scan-3")
    db.add(paper)
    db.commit()

    def fake_empty_ocr(target, **kwargs):
        return {
            "page_count": 1,
            "needs_ocr": True,
            "ocr_engine": "rapidocr",
            "pages": [
                {"page": 1, "width": 595, "height": 842, "text": "", "blocks": []}
            ],
        }

    monkeypatch.setattr("app.parsing.ocr.ocr_pdf", fake_empty_ocr)
    ingest_pdf(db, paper)
    db.refresh(paper)
    assert paper.status == "needs_ocr"
    assert "OCR" in paper.error
    assert db.query(Chunk).filter(Chunk.paper_id == paper.id).count() == 0


def test_partial_text_survives_unavailable_engine(db, tmp_path, monkeypatch):
    """半文本半扫描且无引擎时，已有文本页仍可解析，不被整体标记待 OCR。"""
    from reportlab.pdfgen import canvas as rcanvas

    pdf_path = tmp_path / "partial.pdf"
    c = rcanvas.Canvas(str(pdf_path), pagesize=(595, 842))
    c.setFont("Helvetica", 14)
    c.drawString(60, 760, "First page contains a real text layer with enough words.")
    c.showPage()
    c.showPage()
    c.save()

    paper = Paper(title="partial.pdf", pdf_path=str(pdf_path), content_hash="scan-4")
    db.add(paper)
    db.commit()

    def fake_raise(target, **kwargs):
        raise OcrUnavailable("未检测到 OCR 引擎。")

    monkeypatch.setattr("app.parsing.ocr.ocr_pdf", fake_raise)
    ingest_pdf(db, paper)
    db.refresh(paper)
    assert paper.status == "parsed"
    chunks = db.query(Chunk).filter(Chunk.paper_id == paper.id).count()
    assert chunks >= 1
