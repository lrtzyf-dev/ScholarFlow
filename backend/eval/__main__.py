"""离线演示入口：`python -m eval`。

用合成标注集与确定性假检索器演示单组评测与参数网格搜索，不访问数据库或
网络；真实评测应把实际检索函数包装成 search_fn 后调用 runner。
"""
from __future__ import annotations

from .dataset import SYNTHETIC_LABELS
from .runner import evaluate, grid_search

_DISTRACTORS = ["x1", "x2", "x3", "x4", "x5"]


def make_demo_search_fn(noise: int):
    """noise 越大，相关项被推得越靠后，模拟不同检索参数的效果。"""

    def search_fn(query: str, limit: int) -> list[str]:
        labeled = next(
            (item for item in SYNTHETIC_LABELS if item.query == query), None
        )
        if labeled is None:
            return []
        ranked = list(_DISTRACTORS[:noise]) + list(labeled.relevant)
        return ranked[:limit]

    return search_fn


def main() -> None:
    print("== 单组评测（noise=2, k=5）==")
    print(evaluate(make_demo_search_fn(2), SYNTHETIC_LABELS, 5))

    print("\n== 网格搜索：noise 0–4，k=5（按 NDCG 降序）==")
    grid = grid_search(
        make_demo_search_fn, SYNTHETIC_LABELS,
        {"noise": [0, 1, 2, 3, 4]}, 5,
    )
    for row in grid:
        scores = {
            key: round(value, 4)
            for key, value in row["scores"].items() if key != "k"
        }
        print(row["params"], scores)


if __name__ == "__main__":
    main()
