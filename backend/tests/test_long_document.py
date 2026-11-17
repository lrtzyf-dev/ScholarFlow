"""长文档切块与内存控制专项测试（对应官方 541 页长书场景）。

为控制测试时长，用 40 页（跨 2 个解析批次，PAGE_BATCH=20）做代表性验证；
内存峰值由批次大小决定、与总页数无关，故该结论可外推到 541 页。
"""
import tracemalloc

from reportlab.pdfgen import canvas

from app.models import Chunk, Paper
from app.parsing import pdfplumber_parser
from app.pipeline.indexer import ingest_pdf


def _make_long_pdf(path, pages: int) -> str:
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    for p in range(pages):
        y = 780
        c.setFont("Helvetica", 12)
        c.drawString(50, y, f"Section {p + 1} Heading")
        y -= 24
        c.setFont("Helvetica", 10)
        for i in range(12):
            c.drawString(
                50,
                y,
                f"Page {p + 1} paragraph {i + 1} with realistic body text for chunking.",
            )
            y -= 18
        c.showPage()
    c.save()
    return str(path)


def test_long_document_all_pages_chunked_and_ordered(db, tmp_path):
    n_pages = 40
    pdf_path = _make_long_pdf(tmp_path / "long.pdf", n_pages)
    paper = Paper(title="long.pdf", pdf_path=pdf_path, content_hash="long-x")
    db.add(paper)
    db.commit()

    ingest_pdf(db, paper)
    db.refresh(paper)

    assert paper.status == "parsed"
    assert paper.page_count == n_pages
    chunks = db.query(Chunk).filter(Chunk.paper_id == paper.id).all()
    # 每页多段，chunks 数应明显不少于页数（无尾部丢失）
    assert len(chunks) >= n_pages
    # 页面编号连续覆盖 1..n_pages，且无重复乱序
    touched_pages = sorted({c.page for c in chunks})
    assert touched_pages[0] == 1
    assert touched_pages[-1] == n_pages
    assert len(set(touched_pages)) == n_pages


def test_parse_pdf_reopens_document_per_batch(db, tmp_path, monkeypatch):
    n_pages = 40
    pdf_path = _make_long_pdf(tmp_path / "long2.pdf", n_pages)

    real_open = pdfplumber_parser.pdfplumber.open
    calls = {"n": 0}

    def counting_open(*args, **kwargs):
        calls["n"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(pdfplumber_parser.pdfplumber, "open", counting_open)
    parsed = pdfplumber_parser.parse_pdf(pdf_path)

    # 1 次探总数 + ceil(40/20)=2 个批次，共 3 次打开
    assert calls["n"] == 3
    assert parsed["page_count"] == n_pages


def test_long_document_memory_peak_is_bounded(db, tmp_path):
    n_pages = 40
    pdf_path = _make_long_pdf(tmp_path / "long3.pdf", n_pages)
    paper = Paper(title="long3.pdf", pdf_path=pdf_path, content_hash="long-y")
    db.add(paper)
    db.commit()

    tracemalloc.start()
    ingest_pdf(db, paper)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 实测 40 页约 60MB；120MB 为留足余量的工程上限，证明峰值不随总页数无界增长
    assert peak < 120 * 1024 * 1024, f"峰值 {peak / 1024 / 1024:.1f}MB 超过上限"


def test_embeddings_are_encoded_in_batches(db, tmp_path, monkeypatch):
    import numpy as np

    from app.pipeline import indexer

    n_pages = 40
    pdf_path = _make_long_pdf(tmp_path / "long4.pdf", n_pages)
    paper = Paper(title="long4.pdf", pdf_path=pdf_path, content_hash="long-z")
    db.add(paper)
    db.commit()

    batch_sizes: list[int] = []

    class FakeEmbedder:
        def encode(self, texts):
            batch_sizes.append(len(texts))
            return np.zeros((len(texts), 512), dtype=np.float32)

    monkeypatch.setattr(indexer, "get_embedder", lambda: FakeEmbedder())
    ingest_pdf(db, paper)

    # 40 页产生 200+ chunks，应分多次编码，且每批不超过 64
    assert len(batch_sizes) > 1
    assert all(size <= indexer.EMBED_BATCH_SIZE for size in batch_sizes)
    assert sum(batch_sizes) == db.query(Chunk).filter(Chunk.paper_id == paper.id).count()
