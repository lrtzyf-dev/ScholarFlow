"""论文标识符提取与有效性校验测试。"""
from app.parsing.identifiers import (
    extract_arxiv_id,
    extract_doi,
    extract_identifiers,
)


def test_extracts_new_arxiv_with_and_without_version():
    assert extract_arxiv_id("arXiv:1706.03762v5") == "1706.03762"
    assert extract_arxiv_id("arXiv:1706.03762") == "1706.03762"


def test_extracts_arxiv_from_url():
    assert extract_arxiv_id("https://arxiv.org/abs/1810.04805") == "1810.04805"
    assert (
        extract_arxiv_id("https://arxiv.org/pdf/1706.03762.pdf") == "1706.03762"
    )


def test_rejects_invalid_year_and_month():
    # OCR 误识别的无效年份编号
    assert extract_arxiv_id("8817.34762") is None
    assert extract_arxiv_id("7495.35319") is None
    # 月份非法（20 年 26 月）
    assert extract_arxiv_id("2026.12345") is None
    # 早于 arXiv 发布年份
    assert extract_arxiv_id("9001.0001") is None


def test_extracts_legacy_arxiv_format():
    assert extract_arxiv_id("cs.AI/0102033") == "cs.AI/0102033"


def test_extracts_and_normalizes_doi():
    assert extract_doi("doi:10.1109/TPAMI.2017.2699184.") == (
        "10.1109/tpami.2017.2699184"
    )
    assert extract_doi("https://doi.org/10.1038/nature14539") == (
        "10.1038/nature14539"
    )


def test_extract_identifiers_both_fields():
    result = extract_identifiers("no identifiers here")
    assert result == {"arxiv_id": None, "doi": None}
    both = extract_identifiers("arXiv:1706.03762 doi:10.1000/xyz.1")
    assert both["arxiv_id"] == "1706.03762"
    assert both["doi"] == "10.1000/xyz.1"
