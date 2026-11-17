"""术语表命中检测测试。"""
from app.translate.glossary import DEFAULT_GLOSSARY, find_terms


def test_find_basic_terms():
    hits = find_terms("本文使用深度学习和神经网络进行实验。")
    sources = [h["source"] for h in hits]
    assert "深度学习" in sources
    assert "神经网络" in sources
    assert "实验" in sources
    # 目标译法正确
    by_source = {h["source"]: h["target"] for h in hits}
    assert by_source["深度学习"] == "deep learning"


def test_long_term_has_priority():
    hits = find_terms("卷积神经网络是一种神经网络。")
    sources = [h["source"] for h in hits]
    # 长术语排在短术语之前
    assert sources.index("卷积神经网络") < sources.index("神经网络")


def test_no_hit():
    assert find_terms("这句话没有任何术语。") == []


def test_custom_glossary():
    custom = {"自定义术语": "custom term"}
    hits = find_terms("包含自定义术语", custom)
    assert hits[0]["target"] == "custom term"
    # 自定义模式下不混入默认表
    assert len(hits) == 1
