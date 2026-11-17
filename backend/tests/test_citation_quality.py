"""引用含金量分析测试。"""
from app.algorithms.citation_quality import (
    analyze_paper_references,
    analyze_reference,
    score_occurrence,
)

SUBSTANTIVE = "Smith 等提出的方法在数据集上取得了显著性能提升，准确率明显优于基线模型 [11]。"
DENSE = "近年来的相关研究包括 [1, 2, 3, 4, 5]。"
SHORT = "具体定义见 [7]。"


def test_substantive_citation():
    out = score_occurrence(SUBSTANTIVE)
    assert out["co_cited"] == 1
    assert out["score"] >= 2
    assert "含讨论词" in out["signals"]
    assert analyze_reference(11, SUBSTANTIVE)["label"] == "substantive"


def test_dense_passing_citation():
    out = score_occurrence(DENSE)
    assert out["co_cited"] == 5
    assert "密集罗列" in out["signals"]
    assert out["score"] <= 0
    # 编号 1..5 都在密集列表里，判为应付
    for ref in range(1, 6):
        assert analyze_reference(ref, DENSE)["label"] == "passing"


def test_short_context_is_passing():
    assert analyze_reference(7, SHORT)["label"] == "passing"


def test_unmentioned_reference():
    out = analyze_reference(99, SUBSTANTIVE)
    assert out["label"] == "unmentioned"
    assert out["occurrences"] == 0


def test_batch_analysis():
    text = "\n".join([SUBSTANTIVE, DENSE, SHORT])
    results = analyze_paper_references(text, 11)
    assert len(results) == 11
    by_index = {r["ref_index"]: r["label"] for r in results}
    assert by_index[11] == "substantive"
    assert by_index[1] == "passing"
    assert by_index[7] == "passing"
