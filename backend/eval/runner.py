"""评测运行：对标注集计算聚合指标，并支持参数网格搜索。

检索能力以 search_fn 抽象：search_fn(query, limit) -> 有序 item id 列表，
从而与具体数据库 / 向量库解耦，便于离线测试与参数对比。
"""
from __future__ import annotations

from itertools import product
from typing import Callable

from .metrics import ndcg_at_k, recall_at_k, reciprocal_rank


def evaluate(search_fn: Callable, labels, k: int) -> dict:
    if not labels:
        return {"k": k, "queries": 0, "recall_at_k": 0.0,
                "mrr": 0.0, "ndcg_at_k": 0.0}

    recalls, rrs, ndcgs = [], [], []
    for labeled in labels:
        retrieved = search_fn(labeled.query, k)
        recalls.append(recall_at_k(retrieved, labeled.relevant, k))
        rrs.append(reciprocal_rank(retrieved, labeled.relevant))
        ndcgs.append(ndcg_at_k(retrieved, labeled.relevance_map(), k))

    count = len(labels)
    return {
        "k": k,
        "queries": count,
        "recall_at_k": sum(recalls) / count,
        "mrr": sum(rrs) / count,
        "ndcg_at_k": sum(ndcgs) / count,
    }


def grid_search(search_fn_factory: Callable, labels, param_grid: dict,
                k: int) -> list[dict]:
    """遍历参数组合。

    search_fn_factory(**params) 返回该参数下的 search_fn；
    结果按 NDCG@k 降序，便于直接挑选最优参数。
    """
    keys = list(param_grid)
    results = []
    for combo in product(*(param_grid[key] for key in keys)):
        params = dict(zip(keys, combo))
        search_fn = search_fn_factory(**params)
        results.append({"params": params, "scores": evaluate(search_fn, labels, k)})
    return sorted(
        results, key=lambda r: r["scores"]["ndcg_at_k"], reverse=True
    )
