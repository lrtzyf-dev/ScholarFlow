"""拓扑图脚本生成测试。"""
import pytest

from app.algorithms.diagram import render_diagram, to_dot, to_mermaid


NODES = [
    {"id": "aaa1", "title": "Attention Is All You Need"},
    {"id": "bbb2", "title": 'BERT: "Pre-training" [Deep]'},
    {"id": "ccc3", "title": "A Very Long Title " * 5},
]
EDGES = [
    {"source": "bbb2", "target": "aaa1", "via": "x"},
    {"source": "bbb2", "target": "aaa1"},  # 重复边
    {"source": "aaa1", "target": "aaa1"},  # 自环
    {"source": "ccc3", "target": "aaa1"},
]


def test_mermaid_structure():
    mm = to_mermaid(NODES, EDGES)
    assert mm.startswith("flowchart TD")
    # 每个节点一行
    assert 'aaa1["Attention Is All You Need"]' in mm
    # 去重后只有两条边，自环被移除
    assert mm.count(" --> ") == 2
    assert "bbb2 --> aaa1" in mm
    assert "ccc3 --> aaa1" in mm


def test_mermaid_escapes_quotes_and_brackets():
    mm = to_mermaid(NODES, EDGES)
    # 双引号被替换为单引号，方括号替换为圆括号
    assert "BERT: 'Pre-training' (Deep)" in mm


def test_mermaid_truncates_long_title():
    mm = to_mermaid(NODES, EDGES)
    line = [ln for ln in mm.splitlines() if "ccc3" in ln][0]
    assert line.endswith("…\"]")


def test_dot_structure():
    dot = to_dot(NODES, EDGES)
    assert dot.startswith("digraph ScholarFlow {")
    assert dot.count(" -> ") == 2
    assert dot.rstrip().endswith("}")


def test_render_dispatch_and_fallback():
    assert render_diagram(NODES, EDGES, "dot") == to_dot(NODES, EDGES)
    # 未知格式回退 mermaid
    assert render_diagram(NODES, EDGES, "tikz") == to_mermaid(NODES, EDGES)


def test_empty_graph():
    mm = to_mermaid([], [])
    assert 'empty["暂无节点"]' in mm
