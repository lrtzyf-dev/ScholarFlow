"""标注数据集：查询与相关项的 ground truth。

内置一份合成标注集用于离线演示与测试，明确标注为合成数据，不冒充人工标注；
真实评测应通过 load_labeled_queries 加载人工标注的 JSON。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LabeledQuery:
    query: str
    relevant: list[str]
    grades: dict[str, int] = field(default_factory=dict)

    def relevance_map(self) -> dict[str, int]:
        """相关性分级映射；未显式分级的相关项默认 1。"""
        return {item: self.grades.get(item, 1) for item in self.relevant}


def load_labeled_queries(path: str | Path) -> list[LabeledQuery]:
    """从 JSON 加载标注：[{"query": ..., "relevant": [...], "grades": {...}}]。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        LabeledQuery(
            query=row["query"],
            relevant=list(row["relevant"]),
            grades=dict(row.get("grades", {})),
        )
        for row in payload
    ]


# 合成标注集（离线演示用，非人工标注结论）
SYNTHETIC_LABELS = [
    LabeledQuery(
        query="attention mechanism",
        relevant=["c1", "c2"],
        grades={"c1": 3, "c2": 1},
    ),
    LabeledQuery(
        query="retrieval augmented generation",
        relevant=["c3", "c4", "c5"],
        grades={"c3": 2, "c4": 1, "c5": 1},
    ),
    LabeledQuery(
        query="citation network analysis",
        relevant=["c6"],
        grades={"c6": 2},
    ),
]
