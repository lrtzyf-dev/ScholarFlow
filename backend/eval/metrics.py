"""检索质量评测指标（纯函数，遵循标准信息检索定义）。

所有函数不依赖数据库或网络，可独立单元测试。
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def recall_at_k(retrieved: Sequence, relevant: Iterable, k: int) -> float:
    """Recall@k = top-k 命中的相关项数 / 相关项总数。"""
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    hits = len(set(retrieved[:k]) & relevant_set)
    return hits / len(relevant_set)


def precision_at_k(retrieved: Sequence, relevant: Iterable, k: int) -> float:
    """Precision@k = top-k 命中的相关项数 / k。"""
    if k <= 0:
        return 0.0
    return len(set(retrieved[:k]) & set(relevant)) / k


def reciprocal_rank(retrieved: Sequence, relevant: Iterable) -> float:
    """第一个相关项的倒数排名（1-based）；无命中返回 0。"""
    relevant_set = set(relevant)
    for rank, item in enumerate(retrieved, 1):
        if item in relevant_set:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank(rank_lists: Sequence[tuple[Sequence, Iterable]]) -> float:
    """对一组 (retrieved, relevant) 计算平均倒数排名。"""
    if not rank_lists:
        return 0.0
    return sum(reciprocal_rank(r, rel) for r, rel in rank_lists) / len(rank_lists)


def dcg_at_k(grades: Sequence[float], k: int) -> float:
    """DCG@k：sum (2^rel - 1) / log2(rank+1)。"""
    return sum(
        (2.0 ** rel - 1.0) / math.log2(index + 2)
        for index, rel in enumerate(grades[:k])
    )


def ndcg_at_k(retrieved: Sequence, relevance: dict, k: int) -> float:
    """NDCG@k = DCG@k / IDCG@k。

    relevance 为 {item: 相关性分级}，未列入的项分级为 0。
    """
    actual_grades = [relevance.get(item, 0) for item in retrieved[:k]]
    dcg = dcg_at_k(actual_grades, k)

    ideal_grades = sorted(relevance.values(), reverse=True)[:k]
    idcg = dcg_at_k(ideal_grades, k)
    if idcg == 0.0:
        return 0.0
    return dcg / idcg
