"""语义版本去重的入库流程测试：mock 掉 HTTP，验证 v1/v7 识别与版本建议。"""
from reportlab.pdfgen import canvas

from app.models import Paper
from app.pipeline import importer


def _pdf_bytes(tmp_path, name, text):
    path = tmp_path / name
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 12)
    c.drawString(50, 780, text)
    c.save()
    return path.read_bytes()


def _fake_client(pdf_bytes_list):
    state = {"calls": 0}

    class _Response:
        headers = {"content-type": "application/pdf"}

        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            idx = min(state["calls"], len(pdf_bytes_list) - 1)
            state["calls"] += 1
            return _Response(pdf_bytes_list[idx])

    return _Client


def test_newer_version_detected_and_recommended(db, tmp_path, monkeypatch):
    pdf_v1 = _pdf_bytes(tmp_path, "v1.pdf", "Attention baseline results version one")
    pdf_v7 = _pdf_bytes(tmp_path, "v7.pdf", "Attention updated results version seven with new data")
    monkeypatch.setattr(importer.httpx, "Client", _fake_client([pdf_v1, pdf_v7]))
    # 本用例聚焦版本去重，HTTP 已整体 mock；SSRF 校验依赖本机 DNS，这里一并隔离
    monkeypatch.setattr(importer, "assert_public_url", lambda url: None)

    v1 = importer.import_paper_from_url(db, "https://arxiv.org/pdf/2401.12345v1", title="Paper")
    assert v1["duplicate"] is False
    assert v1.get("version_duplicate") is not True

    v7 = importer.import_paper_from_url(db, "https://arxiv.org/pdf/2401.12345v7", title="Paper")
    assert v7["version_duplicate"] is True
    assert v7["existing_id"] == v1["paper_id"]
    assert v7["incoming_version"] == 7
    assert v7["existing_version"] == 1
    assert v7["newer"] is True
    assert v7["recommend"] == "incoming"

    # 两条记录都保留，且归属同一版本组
    papers = db.query(Paper).all()
    assert len(papers) == 2
    assert {p.version_group for p in papers} == {"2401.12345"}


def test_older_version_recommends_keeping_existing(db, tmp_path, monkeypatch):
    pdf_v7 = _pdf_bytes(tmp_path, "v7.pdf", "Attention updated results version seven")
    pdf_v1 = _pdf_bytes(tmp_path, "v1.pdf", "Attention baseline results version one")
    monkeypatch.setattr(importer.httpx, "Client", _fake_client([pdf_v7, pdf_v1]))
    # 本用例聚焦版本去重，HTTP 已整体 mock；SSRF 校验依赖本机 DNS，这里一并隔离
    monkeypatch.setattr(importer, "assert_public_url", lambda url: None)

    v7 = importer.import_paper_from_url(db, "https://arxiv.org/pdf/2401.12345v7", title="Paper")
    v1 = importer.import_paper_from_url(db, "https://arxiv.org/pdf/2401.12345v1", title="Paper")

    assert v1["version_duplicate"] is True
    assert v1["newer"] is False
    assert v1["recommend"] == "existing"
    assert v1["existing_id"] == v7["paper_id"]
