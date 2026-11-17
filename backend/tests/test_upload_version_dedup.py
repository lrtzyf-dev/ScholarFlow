"""本地上传路径语义版本去重测试（对应官方用例 3.1 的真实交互方式）。

覆盖：
- 文件名含 arXiv ID 时，按归一化版本组识别同论文不同版本；
- 文件名无 arXiv ID 时，用正文 Jaccard 相似度兜底；
- 保留建议方向（新版/旧版）。
"""
from reportlab.pdfgen import canvas

from app.main import app
from app.models import Paper
from fastapi.testclient import TestClient

client = TestClient(app)


def _make_pdf(path, lines):
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 11)
    y = 780
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
    c.save()
    return path


def _upload(tmp_path, filename, lines):
    pdf_path = _make_pdf(tmp_path / filename, lines)
    with open(pdf_path, "rb") as f:
        return client.post(
            "/api/papers/upload",
            files={"file": (filename, f, "application/pdf")},
        )


def test_official_flow_v7_then_v1_flags_conflict(db, tmp_path):
    r7 = _upload(tmp_path, "1706.03762v7.pdf", [
        "Attention Is All You Need updated version seven",
        "The Transformer uses multi-head attention mechanisms.",
    ])
    assert r7.status_code == 200, r7.text
    v7 = r7.json()
    assert v7["status"] == "parsed"

    r1 = _upload(tmp_path, "1706.03762v1.pdf", [
        "Attention Is All You Need first version",
        "The dominant models are based on recurrent networks.",
    ])
    assert r1.status_code == 200, r1.text
    v1 = r1.json()
    assert v1["version_duplicate"] is True
    assert v1["existing_id"] == v7["id"]
    assert v1["incoming_version"] == 1
    assert v1["existing_version"] == 7
    assert v1["newer"] is False
    assert v1["recommend"] == "existing"

    # 两条记录均保留，归属同一版本组
    groups = {p.version_group for p in db.query(Paper).all()}
    assert groups == {"1706.03762"}


def test_v1_then_v7_recommends_incoming(db, tmp_path):
    r1 = _upload(tmp_path, "1706.03762v1.pdf", [
        "Attention Is All You Need version one",
    ])
    r7 = _upload(tmp_path, "1706.03762v7.pdf", [
        "Attention Is All You Need version seven",
    ])
    v7 = r7.json()
    assert v7["version_duplicate"] is True
    assert v7["newer"] is True
    assert v7["recommend"] == "incoming"


def test_text_similarity_fallback_without_arxiv_id(db, tmp_path):
    base = [
        "This paper studies retrieval augmented generation and traceability",
        "The experiment reports precision recall and mean reciprocal rank",
        "Results show consistent improvement across all evaluation benchmarks",
    ]
    # 第二篇仅多一个新词，token 集合 Jaccard 仍高于 0.7 阈值，且文件名无 arXiv ID
    near = base + ["appendix"]
    r_a = _upload(tmp_path, "manuscript_draft.pdf", base)
    assert r_a.json()["status"] == "parsed"

    r_b = _upload(tmp_path, "manuscript_final.pdf", near)
    conflict = r_b.json()
    assert conflict["version_duplicate"] is True
    assert conflict["existing_id"] == r_a.json()["id"]


def test_distinct_papers_do_not_conflict(db, tmp_path):
    r_a = _upload(tmp_path, "paper_one.pdf", [
        "Graph neural networks for molecular property prediction",
    ])
    r_b = _upload(tmp_path, "paper_two.pdf", [
        "Reinforcement learning for autonomous robot navigation control",
    ])
    assert r_b.json()["version_duplicate"] is False
    assert r_b.json()["status"] == "parsed"
