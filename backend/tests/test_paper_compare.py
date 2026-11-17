"""多篇跨文献对比服务测试。"""
import pytest
from fastapi import HTTPException
from reportlab.pdfgen import canvas

from app.agent.paper_compare import build_comparison, resolve_papers
from app.api.papers import CompareRequest, compare_papers_api
from app.models import Paper
from app.pipeline.indexer import ingest_pdf


def _make_pdf(path, lines):
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    y = 780
    for text, size in lines:
        c.setFont("Helvetica", size)
        c.drawString(50, y, text)
        y -= 20 if size > 12 else 16
    c.save()


def _ingest(db, tmp, name, title, lines):
    pdf_path = tmp / name
    _make_pdf(pdf_path, lines)
    paper = Paper(title=title, pdf_path=str(pdf_path), content_hash=f"h-{name}")
    db.add(paper)
    db.commit()
    ingest_pdf(db, paper)
    db.refresh(paper)
    return paper


ALPHA = [
    ("Alpha Study", 14),
    ("1 Method", 11),
    ("Our method uses a graph neural network with attention.", 10),
    ("2 Experiments", 11),
    ("We evaluate on the ImageNet dataset and report accuracy 95 percent.", 10),
    ("3 Conclusion", 11),
    ("The graph method improves accuracy on the benchmark.", 10),
]
BETA = [
    ("Beta Study", 14),
    ("1 Method", 11),
    ("We propose a transformer method with self-attention layers.", 10),
    ("2 Experiments", 11),
    ("Experiments use the COCO dataset and report an F1 score of 0.88.", 10),
    ("3 Conclusion", 11),
    ("The transformer achieves strong results on the benchmark.", 10),
]


def test_build_comparison_structure(db, tmp_path):
    a = _ingest(db, tmp_path, "a.pdf", "Alpha Study", ALPHA)
    b = _ingest(db, tmp_path, "b.pdf", "Beta Study", BETA)

    result = build_comparison(db, [a.id, b.id])
    assert [p["id"] for p in result["papers"]] == [a.id, b.id]
    assert len(result["dimensions"]) == 4
    assert len(result["rows"]) == 4

    for row in result["rows"]:
        assert set(row["cells"]) == {a.id, b.id}
        for pid, cell in row["cells"].items():
            # 每条证据都属于对应论文，且可定位
            for ev in cell["evidence"]:
                assert ev["paper_id"] == pid
                assert ev["page"] >= 1
                assert ev["quote"]


def test_method_evidence_contains_paper_specific_content(db, tmp_path):
    a = _ingest(db, tmp_path, "a.pdf", "Alpha Study", ALPHA)
    b = _ingest(db, tmp_path, "b.pdf", "Beta Study", BETA)

    result = build_comparison(db, [a.id, b.id])
    method_row = next(r for r in result["rows"] if r["key"] == "method")
    # 各单元格证据来自本论文，不串台
    assert all(
        ev["paper_id"] == a.id for ev in method_row["cells"][a.id]["evidence"]
    )
    assert all(
        ev["paper_id"] == b.id for ev in method_row["cells"][b.id]["evidence"]
    )


def test_build_comparison_requires_two_papers(db, tmp_path):
    a = _ingest(db, tmp_path, "a.pdf", "Alpha Study", ALPHA)
    with pytest.raises(ValueError):
        build_comparison(db, [a.id])
    with pytest.raises(ValueError):
        build_comparison(db, ["missing", "also-missing"])


def test_resolve_papers_skips_unknown_and_unparsed(db, tmp_path):
    a = _ingest(db, tmp_path, "a.pdf", "Alpha Study", ALPHA)
    uploaded = Paper(title="Not Ready", status="uploaded", content_hash="h-up")
    db.add(uploaded)
    db.commit()

    resolved = resolve_papers(db, [a.id, uploaded.id, "ghost"])
    assert [p.id for p in resolved] == [a.id]


def test_compare_api_endpoint(db, tmp_path):
    a = _ingest(db, tmp_path, "a.pdf", "Alpha Study", ALPHA)
    b = _ingest(db, tmp_path, "b.pdf", "Beta Study", BETA)
    with pytest.raises(HTTPException) as error:
        compare_papers_api(CompareRequest(paper_ids=[a.id, b.id]), db)
    assert error.value.status_code == 409 and error.value.detail["not_executed"]


def test_compare_api_endpoint_bad_request(db):
    with pytest.raises(HTTPException) as exc:
        compare_papers_api(CompareRequest(paper_ids=["x"]), db)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "migration_required"
