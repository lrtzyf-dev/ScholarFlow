"""正文引用标记解析测试。"""
from app.parsing.citation_marks import citation_map, extract_citation_numbers


def test_single_bracket_citation():
    assert extract_citation_numbers("as shown in [11]") == [11]


def test_multiple_citations():
    assert extract_citation_numbers("prior work [12, 13]") == [12, 13]
    assert extract_citation_numbers("[12,13]") == [12, 13]


def test_range_citations():
    assert extract_citation_numbers("[12-14]") == [12, 13, 14]
    assert extract_citation_numbers("[12–14]") == [12, 13, 14]


def test_parentheses_and_cjk_brackets():
    assert extract_citation_numbers("(11)") == [11]
    assert extract_citation_numbers("（11）") == [11]


def test_mixed_citations_dedup_sorted():
    assert extract_citation_numbers("[15, 11, 13-15]") == [11, 13, 14, 15]


def test_empty_and_no_citation():
    assert extract_citation_numbers("") == []
    assert extract_citation_numbers("no citation here") == []
    # 作者-年份制不被误判
    assert extract_citation_numbers("(Smith, 2020)") == []


def test_citation_map_filters_by_reference_range():
    chunks = [
        {"page": 1, "bbox": [1, 2, 3, 4], "content": "intro [11]"},
        {"page": 2, "bbox": [5, 6, 7, 8], "content": "method [12-14]"},
        {"page": 3, "bbox": [9, 10, 11, 12], "content": "year [2023]"},
    ]
    result = citation_map(chunks, max_ref_index=20)
    assert result[11] == [{"page": 1, "bbox": [1, 2, 3, 4]}]
    assert result[12] == [{"page": 2, "bbox": [5, 6, 7, 8]}]
    assert result[13] == result[14]
    # 超出参考文献范围的 2023 被过滤
    assert 2023 not in result


def test_citation_map_dedup_same_location():
    chunks = [
        {"page": 1, "bbox": [1, 2, 3, 4], "content": "[11] and [11]"},
    ]
    result = citation_map(chunks, max_ref_index=20)
    assert result[11] == [{"page": 1, "bbox": [1, 2, 3, 4]}]
