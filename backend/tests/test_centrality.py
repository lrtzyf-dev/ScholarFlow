"""PageRank 与中心性算法测试。"""
import math

from app.algorithms.centrality import (
    degree_centrality,
    pagerank,
    subgraph_pagerank,
)


def test_pagerank_sums_to_one():
    nodes = ["a", "b", "c", "d"]
    edges = [("a", "c"), ("b", "c"), ("d", "c")]
    scores = pagerank(nodes, edges)
    assert set(scores) == set(nodes)
    assert abs(sum(scores.values()) - 1.0) < 1e-6
    assert all(math.isfinite(v) for v in scores.values())


def test_pagerank_hub_paper_scores_highest():
    # 基石节点 c 被其余所有节点引用
    nodes = ["a", "b", "c", "d", "e"]
    edges = [("a", "c"), ("b", "c"), ("d", "c"), ("e", "c"), ("a", "d")]
    scores = pagerank(nodes, edges)
    assert scores["c"] == max(scores.values())
    # 明显高于没有任何入链的节点
    assert scores["c"] > scores["b"]


def test_pagerank_handles_dangling_nodes_without_nan():
    # 链 a->b->c，c 无出链（dangling），不应泄漏分数或产生 NaN
    nodes = ["a", "b", "c"]
    edges = [("a", "b"), ("b", "c")]
    scores = pagerank(nodes, edges)
    assert all(math.isfinite(v) for v in scores.values())
    assert abs(sum(scores.values()) - 1.0) < 1e-6
    # 最终被引节点 c 得分最高
    assert scores["c"] > scores["a"]


def test_pagerank_ignores_self_loops_and_duplicate_edges():
    nodes = ["a", "b"]
    edges = [("a", "a"), ("a", "b"), ("a", "b")]
    scores = pagerank(nodes, edges)
    assert abs(sum(scores.values()) - 1.0) < 1e-6
    assert scores["b"] > scores["a"]


def test_pagerank_empty_and_single_node():
    assert pagerank([], []) == {}
    single = pagerank(["x"], [])
    assert abs(single["x"] - 1.0) < 1e-6


def test_pagerank_is_deterministic():
    nodes = ["a", "b", "c", "d"]
    edges = [("a", "c"), ("b", "c"), ("d", "a")]
    first = pagerank(nodes, edges)
    second = pagerank(nodes, edges)
    assert first == second


def test_degree_centrality_normalized():
    nodes = ["a", "b", "c"]
    edges = [("a", "c"), ("b", "c")]
    in_cent, out_cent = degree_centrality(nodes, edges)
    # c 入度为 2，归一化 2/(n-1)=1.0
    assert in_cent["c"] == 1.0
    assert in_cent["a"] == 0.0
    # a 出度 1，归一化 0.5
    assert out_cent["a"] == 0.5
    assert out_cent["c"] == 0.0


def test_subgraph_pagerank_keeps_internal_edges():
    # 全图 A->B->C 且 A->D；子图只取 A,B,C，应保留 A->B->C，排除 D
    all_edges = [("a", "b"), ("b", "c"), ("a", "d")]
    scores = subgraph_pagerank(["a", "b", "c"], all_edges)
    assert set(scores) == {"a", "b", "c"}
    assert abs(sum(scores.values()) - 1.0) < 1e-6
    # 被引终点 c 在子图内得分最高
    assert scores["c"] == max(scores.values())


def test_subgraph_pagerank_dedupes_nodes():
    scores = subgraph_pagerank(["x", "x"], [])
    assert list(scores) == ["x"]
    assert abs(scores["x"] - 1.0) < 1e-6
