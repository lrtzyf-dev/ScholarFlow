"""测试夹具：隔离数据目录、动态生成样本 PDF（reportlab，BSD 许可）、每次重建数据库。"""
import os
from pathlib import Path
import json
import secrets
import shutil
import tempfile
import textwrap

_temp_root = Path(tempfile.gettempdir()).resolve()
_tmp = tempfile.mkdtemp(prefix="scholarflow_test_", dir=_temp_root)
_temp_scope = Path(_tmp).resolve()
_temp_identity = (_temp_scope.stat().st_dev, _temp_scope.stat().st_ino)
_temp_owner = {"pid": os.getpid(), "token": secrets.token_hex(24), "scope": str(_temp_scope)}
(_temp_scope / ".test-temp-owner.json").write_text(json.dumps(_temp_owner), encoding="utf-8")
# Keep test-storage metadata outside the application's DATA_DIR so backup,
# deletion and source manifests still exercise ordinary application data.
os.environ["DATA_DIR"] = str(_temp_scope / "data")
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_DEMO_MODE"] = "1"  # Tests explicitly opt into synthetic responses.
os.environ["EMBEDDING_API_KEY"] = ""
os.environ["REDIS_URL"] = ""
os.environ["SCHOLARFLOW_TEST_TRANSPORT"] = "1"  # In-process TestClient only; TCP still requires loopback.

import pytest  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.config import VECTOR_PATH  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.models import Paper  # noqa: E402
from app.pipeline.indexer import ingest_pdf  # noqa: E402

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    result = yield
    _cleanup_test_temp(session, session.exitstatus)
    return result


def _cleanup_test_temp(session, exitstatus):
    """Delete only this interpreter's verified scratch after a successful run.

    No scan, glob, other pytest directory, production path or user backup is
    involved. Interrupted/failed sessions and explicit debug retention stay.
    """
    def retained(reason):
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        if reporter:
            reporter.write_line(f"Test scratch retained ({reason}): {_temp_scope}")
    try:
        engine.dispose()
        if int(exitstatus) != 0:
            retained("failed or interrupted session")
            return
        if os.environ.get("KEEP_TEST_TEMP", "").strip().casefold() in ("1", "true", "yes", "on"):
            retained("KEEP_TEST_TEMP")
            return
        if not _temp_scope.exists():
            return
        if (_temp_scope.is_symlink() or getattr(_temp_scope, "is_junction", lambda: False)()
                or _temp_scope.resolve(strict=True) != _temp_scope
                or _temp_scope.parent != _temp_root
                or not _temp_scope.name.startswith("scholarflow_test_")
                or (_temp_scope.stat().st_dev, _temp_scope.stat().st_ino) != _temp_identity
                or json.loads((_temp_scope / ".test-temp-owner.json").read_text(encoding="utf-8")) != _temp_owner):
            retained("scope identity changed")
            return
        shutil.rmtree(_temp_scope)
    except Exception as exc:
        # A test-owned active file may remain locked on Windows. Preserve the
        # remaining scratch and report its path without changing test results.
        retained(type(exc).__name__)


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
    # FTS5 virtual/shadow tables are outside ORM metadata. Clearing only the
    # ORM tables leaves old postings that can crowd out current chunk IDs in
    # a broad suite, especially when semantic search deliberately falls back.
    from sqlalchemy import text
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM chunks_fts"))
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
