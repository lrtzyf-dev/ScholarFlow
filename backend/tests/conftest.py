"""测试夹具：隔离数据目录、动态生成样本 PDF（reportlab，BSD 许可）、每次重建数据库。"""
import os
import tempfile
import textwrap

_tmp = tempfile.mkdtemp(prefix="scholarflow_test_")
os.environ["DATA_DIR"] = _tmp
os.environ["LLM_API_KEY"] = ""
os.environ["EMBEDDING_API_KEY"] = ""
os.environ["REDIS_URL"] = ""

import pytest  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.config import VECTOR_PATH  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.models import Paper  # noqa: E402
from app.pipeline.indexer import ingest_pdf  # noqa: E402

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


SAMPLE_LINES = [
    ("Attention Is All You Need", 14),
    ("1 Introduction", 11),
    ("The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.", 10),
    ("注意力机制完全取代了循环网络，成为机器翻译任务中的主流架构。", 10),
    ("We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.", 10),
    ("2 Model Architecture", 11),
    ("We propose the Transformer, an encoder-decoder model that uses stacked self-attention and position-wise fully connected layers.", 10),
    ("Scaled dot-product attention computes the attention function as a weighted sum of values, and multi-head attention runs h projections in parallel.", 10),
    ("The model uses 8 attention heads, 6 encoder layers and 6 decoder layers, and reaches 28.4 BLEU on WMT English-to-German.", 10),
    ("Table 1 summarizes the maximum label smoothing accuracy of each model layer on the English-to-German translation task.", 10),
    ("3 Conclusion", 11),
    ("The Transformer reaches a new state of the art on WMT translation and can be trained significantly faster than architectures based on recurrent layers.", 10),
]
REF_LINES = ["References", "[1] Vaswani, A. et al. Attention Is All You Need. NeurIPS 2017.",
             "[2] Devlin, J. et al. BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding. NAACL 2019."]


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "sample.pdf"
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    y = 780
    for text, size in SAMPLE_LINES:
        font = "STSong-Light" if _is_cjk(text) else "Helvetica"
        c.setFont(font, size)
        for piece in textwrap.wrap(text, 90) or [text]:
            c.drawString(50, y, piece)
            y -= 22 if size > 12 else 16
        y -= 2
    c.showPage()
    c.setFont("Helvetica", 10)
    y = 780
    for line in REF_LINES:
        for piece in textwrap.wrap(line, 90) or [line]:
            c.drawString(50, y, piece)
            y -= 16
    c.save()
    return path


@pytest.fixture(autouse=True)
def reset_db():
    """每个测试前重建表（覆盖 API 测试用的独立 session）。"""
    init_db()
    Base.metadata.drop_all(engine)
    init_db()
    if VECTOR_PATH.exists():
        VECTOR_PATH.unlink()
    yield


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def seeded_paper(db, sample_pdf):
    paper = Paper(title="", pdf_path=str(sample_pdf), content_hash="test-hash")
    db.add(paper)
    db.commit()
    ingest_pdf(db, paper)
    db.refresh(paper)
    return paper
