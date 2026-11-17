"""检索评测指标纯函数测试。"""
import math

from eval.metrics import (
    dcg_at_k,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_recall_at_k():
    assert recall_at_k(["a", "b", "c"], ["a", "d"], 3) == 0.5
    assert recall_at_k(["a", "b"], ["a", "b"], 2) == 1.0
    # 无相关项时返回 0，避免除零
    assert recall_at_k(["a"], [], 2) == 0.0


def test_precision_at_k():
    assert precision_at_k(["a", "x", "y"], ["a", "b"], 3) == 1 / 3


def test_reciprocal_rank():
    assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert reciprocal_rank(["a"], ["a"]) == 1.0
    assert reciprocal_rank(["x", "y"], ["a"]) == 0.0


def test_mean_reciprocal_rank():
    lists = [
        (["a"], ["a"]),
        (["x", "b"], ["b"]),
    ]
    assert mean_reciprocal_rank(lists) == 0.75


def test_dcg_basic():
    # 位置 1，grade=3：(2^3-1)/log2(2) = 7
    assert dcg_at_k([3], 1) == 7.0


def test_ndcg_perfect_and_imperfect():
    relevance = {"a": 3, "b": 2}
    # 理想排序 NDCG = 1
    assert ndcg_at_k(["a", "b"], relevance, 2) == 1.0
    # 反转排序 NDCG < 1
    reversed_score = ndcg_at_k(["b", "a"], relevance, 2)
    assert 0.0 < reversed_score < 1.0
    # 无相关项时 IDCG=0，返回 0
    assert ndcg_at_k(["x"], {}, 2) == 0.0
