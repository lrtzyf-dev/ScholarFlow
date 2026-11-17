"""引用映射接口测试。"""
import pytest
from fastapi import HTTPException

from app.api.papers import get_citations
from app.models import Chunk, Paper, Reference


def _seed_paper(db) -> Paper:
    paper = Paper(title="Demo Paper", status="parsed", content_hash="h-demo")
    db.add(paper)
    db.commit()
    db.add(
        Reference(
            paper_id=paper.id,
            ref_index=11,
            raw_text="[11] Some prior work",
            page=5,
            bbox=[72, 200, 500, 220],
        )
    )
    db.add(
        Chunk(
            paper_id=paper.id,
            chunk_index=0,
            page=1,
            bbox=[72, 300, 500, 330],
            content="as demonstrated in [11] earlier",
            char_count=28,
        )
    )
    db.commit()
    return paper


def test_get_citations_returns_reference_and_inline_map(db):
    paper = _seed_paper(db)
    result = get_citations(paper.id, db)

    assert len(result["references"]) == 1
    ref = result["references"][0]
    assert ref["ref_index"] == 11
    assert ref["page"] == 5
    assert ref["bbox"] == [72, 200, 500, 220]

    # 正文引用位置映射，键为字符串编号
    assert result["inline_citations"]["11"] == [
        {"page": 1, "bbox": [72, 300, 500, 330]}
    ]


def test_get_citations_unknown_paper_raises(db):
    with pytest.raises(HTTPException) as exc:
        get_citations("missing", db)
    assert exc.value.status_code == 404
