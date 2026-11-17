"""细粒度结构化抽取端到端测试（T1-4）。"""
import numpy as np
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.models import Chunk, Paper
from app.pipeline.indexer import ingest_pdf


def _build_pdf(path):
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    # 标题
    c.setFont("Helvetica", 14)
    c.drawString(50, 780, "A Structured Extraction Study")
    # 作者
    c.setFont("Helvetica", 10)
    c.drawString(50, 740, "Alice Smith, Bob Jones")
    # 摘要（独立块）
    c.setFont("Helvetica", 11)
    c.drawString(50, 700, "Abstract")
    c.setFont("Helvetica", 10)
    c.drawString(50, 662, "This paper demonstrates structured extraction from a PDF.")
    # 章节
    c.setFont("Helvetica", 11)
    c.drawString(50, 624, "1 Method")
    c.setFont("Helvetica", 10)
    c.drawString(50, 586, "The energy follows the relation below.")
    # 显示公式
    c.drawString(50, 552, "E = mc^2")
    # 图片
    img = Image.fromarray((np.random.rand(60, 80, 3) * 255).astype("uint8"))
    c.drawImage(ImageReader(img), 50, 380, 160, 120)
    # 图注
    c.drawString(50, 360, "Figure 1: the experimental setup.")
    c.showPage()
    c.save()


def test_ingest_populates_metadata_and_kinds(db, tmp_path):
    pdf = tmp_path / "structured.pdf"
    _build_pdf(pdf)
    paper = Paper(title="", pdf_path=str(pdf), content_hash="h-struct")
    db.add(paper)
    db.commit()
    ingest_pdf(db, paper)
    db.refresh(paper)

    assert paper.status == "parsed"
    assert paper.abstract.startswith("This paper demonstrates")
    assert "Alice Smith" in paper.authors

    kinds = {ch.kind for ch in db.query(Chunk).filter(Chunk.paper_id == paper.id)}
    assert "figure" in kinds
    assert "formula" in kinds
    assert "caption" in kinds
    # 公式内容保留
    formula = db.query(Chunk).filter(
        Chunk.paper_id == paper.id, Chunk.kind == "formula"
    ).first()
    assert "mc" in formula.content
