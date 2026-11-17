"""
图论中心性算法：在引用图谱上识别基石/核心论文。

- PageRank：考虑"被谁引用"的递归权重，被高权重论文引用的节点得分更高，
  比单纯入度更能反映学术影响力；处理无出链节点（dangling）避免分数泄漏。
- 度中心性：归一化入度/出度，作为可解释的基础指标。

纯 numpy 之外无重依赖，离线可复现；全部为确定性计算，不依赖 LLM。
"""
from __future__ import annotations

from typing import Iterable, Sequence


def _build_adjacency(node_ids: Sequence[str], edges: Iterable[tuple[str, str]]):
    n = len(node_ids)
    index = {node_id: i for i, node_id in enumerate(node_ids)}
    in_links: list[list[int]] = [[] for _ in range(n)]
    out_count = [0] * n
    edge_set: set[tuple[int, int]] = set()
    for source, target in edges:
        if source not in index or target not in index:
            continue
        i, j = index[source], index[target]
        if i == j or (i, j) in edge_set:
            continue
        edge_set.add((i, j))
        out_count[i] += 1
        in_links[j].append(i)
    return in_links, out_count


def pagerank(
    node_ids: Sequence[str],
    edges: Iterable[tuple[str, str]],
    damping: float = 0.85,
    iterations: int = 100,
    tol: float = 1e-8,
) -> dict[str, float]:
    """PageRank 幂迭代，返回 {node_id: score}，分数总和为 1。"""
    n = len(node_ids)
    if n == 0:
        return {}
    in_links, out_count = _build_adjacency(node_ids, edges)

    rank = [1.0 / n] * n
    for _ in range(max(1, iterations)):
        dangling_share = sum(rank[i] for i in range(n) if out_count[i] == 0) / n
        new_rank = [0.0] * n
        for j in range(n):
            incoming_flow = sum(
                rank[i] / out_count[i] for i in in_links[j] if out_count[i] > 0
            )
            new_rank[j] = (1.0 - damping) / n + damping * (
                incoming_flow + dangling_share
            )
        delta = sum(abs(new_rank[j] - rank[j]) for j in range(n))
        rank = new_rank
        if delta < tol:
            break

    total = sum(rank)
    if total > 0:
        rank = [r / total for r in rank]
    return {node_id: rank[i] for i, node_id in enumerate(node_ids)}


def degree_centrality(
    node_ids: Sequence[str], edges: Iterable[tuple[str, str]]
) -> tuple[dict[str, float], dict[str, float]]:
    """归一化入度/出度中心性，返回 (in_centrality, out_centrality)，范围 0..1。"""
    n = len(node_ids)
    if n == 0:
        return {}, {}
    in_links, out_count = _build_adjacency(node_ids, edges)
    denom = max(1, n - 1)
    in_cent = {
        node_id: len(in_links[i]) / denom for i, node_id in enumerate(node_ids)
    }
    out_cent = {
        node_id: out_count[i] / denom for i, node_id in enumerate(node_ids)
    }
    return in_cent, out_cent


def subgraph_pagerank(
    node_subset: Sequence[str],
    all_edges: Iterable[tuple[str, str]],
    damping: float = 0.85,
    iterations: int = 100,
    tol: float = 1e-8,
) -> dict[str, float]:
    """在检索结果子图上做带阻尼的 PageRank 迭代。

    保留全图边中两端都在子集中的边，再在子图上幂迭代，用于在某次检索
    的候选集合上评估相对影响力，使基石节点由计算得出而非按出现顺序挑选。
    """
    subset = list(dict.fromkeys(node_subset))  # 去重并保持顺序
    subset_set = set(subset)
    sub_edges = [
        (source, target)
        for source, target in all_edges
        if source in subset_set and target in subset_set
    ]
    return pagerank(
        subset, sub_edges, damping=damping, iterations=iterations, tol=tol
    )
