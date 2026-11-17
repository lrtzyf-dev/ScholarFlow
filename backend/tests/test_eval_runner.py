"""评测运行器与标注集测试。"""
from eval.__main__ import make_demo_search_fn
from eval.dataset import SYNTHETIC_LABELS, LabeledQuery
from eval.runner import evaluate, grid_search


def test_evaluate_perfect_search():
    def perfect(query, limit):
        labeled = next(item for item in SYNTHETIC_LABELS if item.query == query)
        return labeled.relevant[:limit]

    out = evaluate(perfect, SYNTHETIC_LABELS, 5)
    assert out["queries"] == 3
    assert out["recall_at_k"] == 1.0
    assert out["mrr"] == 1.0
    assert out["ndcg_at_k"] == 1.0


def test_evaluate_empty_labels():
    out = evaluate(lambda query, limit: [], [], 5)
    assert out["queries"] == 0
    assert out["recall_at_k"] == 0.0


def test_grid_search_orders_by_ndcg():
    grid = grid_search(
        make_demo_search_fn, SYNTHETIC_LABELS, {"noise": [0, 3]}, 5
    )
    assert grid[0]["params"]["noise"] == 0
    assert grid[0]["scores"]["ndcg_at_k"] >= grid[1]["scores"]["ndcg_at_k"]


def test_relevance_map_default_grade():
    labeled = LabeledQuery(query="q", relevant=["a", "b"])
    assert labeled.relevance_map() == {"a": 1, "b": 1}
