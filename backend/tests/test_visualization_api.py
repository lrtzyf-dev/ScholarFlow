"""可视化 API 端点集成测试。"""
from app.api.visualization import CaptionRequest, CsvRequest, caption_api, parse_csv_api

CSV = "model,precision,recall\nBERT,0.89,0.87\nRoBERTa,0.92,0.90\nGPT,0.95,0.93\n"


def test_parse_endpoint():
    result = parse_csv_api(CsvRequest(content=CSV))
    assert result["columns"] == ["model", "precision", "recall"]
    assert result["row_count"] == 3
    assert set(result["numeric_columns"]) == {"precision", "recall"}


def test_caption_endpoint_offline():
    parsed = parse_csv_api(CsvRequest(content=CSV))
    out = caption_api(CaptionRequest(data=parsed, chart_type="bar chart"))
    # 离线默认走模板，且内容基于真实统计
    assert out["mode"] == "template"
    assert "bar chart" in out["caption"]
    assert "3 条记录" in out["caption"]
