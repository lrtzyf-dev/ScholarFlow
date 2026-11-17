"""CSV 解析与数值列识别测试。"""
import pytest

from app.parsing.csv_parser import parse_csv


def test_basic_parse_and_columns():
    result = parse_csv("name,score,age\nAlice,90,20\nBob,85,21\n")
    assert result["columns"] == ["name", "score", "age"]
    assert result["row_count"] == 2
    assert result["rows"][0] == ["Alice", "90", "20"]


def test_numeric_columns_and_stats():
    result = parse_csv("name,score,age\nAlice,90,20\nBob,85,21\nCarol,70,22\n")
    # name 非数值；score、age 为数值列
    assert "name" not in result["numeric_columns"]
    assert set(result["numeric_columns"]) == {"score", "age"}
    score = result["stats"]["score"]
    assert score["min"] == 70
    assert score["max"] == 90
    assert score["mean"] == pytest.approx(81.6667, rel=1e-3)


def test_mixed_column_not_numeric():
    # 多数值无法转 float，不判定为数值列
    result = parse_csv("label\n10\nunknown\nNA\n")
    assert result["numeric_columns"] == []


def test_bom_handled():
    result = parse_csv("\ufeffa,b\n1,2\n")
    assert result["columns"] == ["a", "b"]


def test_empty_csv():
    result = parse_csv("")
    assert result["columns"] == []
    assert result["row_count"] == 0


def test_truncation_flag():
    body = "\n".join(f"v{i}" for i in range(5))
    result = parse_csv("col\n" + body + "\n", max_rows=3)
    assert result["row_count"] == 3
    assert result["truncated"] is True
