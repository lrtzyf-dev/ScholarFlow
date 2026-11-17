"""Future Work API 端点集成测试。"""
from app.api.papers import future_work_api
from app.models import Chunk, Paper


def _add(db, title: str, published: str, content: str) -> None:
    paper = Paper(title=title, status="parsed", content_hash=f"h-{title}",
                  published_at=published)
    db.add(paper)
    db.commit()
    db.add(Chunk(paper_id=paper.id, chunk_index=0, content=content))
    db.commit()


def test_future_work_api(db):
    _add(
        db, "Old", "2018-01-01",
        "In future work, we plan to extend the model to multimodal data.",
    )
    _add(
        db, "New", "2021-06-01",
        "This paper presents a model and we extend it to multimodal data.",
    )
    out = future_work_api(db)
    assert out["total"] == 1
    assert len(out["possibly_addressed"]) == 1
    item = out["possibly_addressed"][0]
    assert item["resolved_by"][0]["paper_title"] == "New"
