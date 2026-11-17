"""Figure Caption 生成测试。"""
from app.parsing.caption import generate_caption, template_caption
from app.parsing.csv_parser import parse_csv


DATA = parse_csv("name,score,age\nAlice,90,20\nBob,80,21\nCarol,70,22\n")


def test_template_caption_uses_real_stats():
    caption = template_caption(DATA, "bar chart")
    assert "bar chart" in caption
    assert "3 条记录" in caption
    # 真实统计数字出现
    assert "80.00" in caption  # score 均值
    assert "70" in caption     # 最小值


def test_generate_caption_offline_mode():
    out = generate_caption(DATA, "line chart")
    assert out["mode"] == "template"
    assert out["caption"]


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def chat(self, messages, temperature=0.3):
        return _FakeResponse("Figure 1. A model-generated caption.")


def test_generate_caption_llm_mode():
    out = generate_caption(DATA, "bar chart", llm=_FakeLLM())
    assert out["mode"] == "llm"
    assert "model-generated" in out["caption"]
