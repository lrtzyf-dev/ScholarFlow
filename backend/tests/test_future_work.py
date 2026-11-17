"""Future Work 提取与时间线判断测试。"""
from app.algorithms.future_work import build_future_work, year_of

OLD_PAPER = {
    "id": "p1", "title": "Old Model", "year": 2018,
    "chunks": [{
        "page": 5, "bbox": [1, 2, 3, 4],
        "content": (
            "The model performs well on text. In future work, we plan to extend "
            "the model to multimodal data. We leave low-resource language adaptation "
            "as future work."
        ),
    }],
}
NEW_PAPER = {
    "id": "p2", "title": "New Model", "year": 2021,
    "chunks": [{
        "page": 2, "bbox": None,
        "content": "This paper presents a unified model and we extend it to multimodal data.",
    }],
}


def test_year_extraction():
    assert year_of("2018-05-01") == 2018
    assert year_of(None) is None


def test_extract_and_timeline():
    out = build_future_work([OLD_PAPER, NEW_PAPER])
    assert out["total"] == 2
    # 多模态 idea 被更晚论文处理
    addressed_quotes = " ".join(d["quote"] for d in out["possibly_addressed"])
    assert "multimodal" in addressed_quotes
    item = out["possibly_addressed"][0]
    assert item["resolved_by"][0]["paper_id"] == "p2"
    # 可溯源位置保留
    assert item["page"] == 5
    # 低资源语言 idea 仍开放
    open_quotes = " ".join(d["quote"] for d in out["open"])
    assert "low-resource" in open_quotes


def test_no_later_paper_keeps_all_open():
    out = build_future_work([OLD_PAPER])
    assert len(out["open"]) == 2
    assert out["possibly_addressed"] == []
