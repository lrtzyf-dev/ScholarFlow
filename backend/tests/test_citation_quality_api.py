"""引用含金量 API 端点集成测试。"""
import pytest

from app.api.papers import citation_quality_api
from app.models import Chunk, Paper, Reference


def _setup(db) -> str:
    paper = Paper(title="测试论文", status="parsed", content_hash="h-q")
    db.add(paper)
    db.commit()

    db.add(Reference(paper_id=paper.id, ref_index=1, raw_text="Smith, A Good Method"))
    db.add(Reference(paper_id=paper.id, ref_index=2, raw_text="Listed Work"))
    db.add(
        Chunk(
            paper_id=paper.id,
            chunk_index=0,
            content=(
                "Smith 等提出的方法在数据集上取得了显著性能提升，准确率优于基线 [1]。"
                "其他相关工作见 [2, 3, 4, 5]。"
            ),
        )
    )
    db.commit()
    return paper.id


def test_quality_endpoint_structure(db):
    paper_id = _setup(db)
    out = citation_quality_api(paper_id, None, db)
    assert out["paper_id"] == paper_id
    assert len(out["results"]) == 2
    by_index = {r["ref_index"]: r for r in out["results"]}
    # ref1 实质讨论，ref2 处于密集罗列
    assert by_index[1]["label"] == "substantive"
    assert by_index[2]["label"] == "passing"
    # raw_text 已合并
    assert by_index[1]["raw_text"] == "Smith, A Good Method"
    # 汇总计数
    assert out["summary"]["substantive"] == 1
    assert out["summary"]["passing"] == 1


def test_quality_endpoint_filter(db):
    paper_id = _setup(db)
    out = citation_quality_api(paper_id, "passing", db)
    assert len(out["results"]) == 1
    assert out["results"][0]["ref_index"] == 2


def test_quality_endpoint_missing_paper(db):
    with pytest.raises(Exception) as exc:
        citation_quality_api("nope", None, db)
    assert getattr(exc.value, "status_code", None) == 404
