"""
论文版本 diff（v1 vs v7）：
- 句子 embedding 贪心对齐（cosine ≥ 0.82 配对，语义修改）
- difflib 字面兜底（新增/删除）
- 输出 added / removed / changed
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..pipeline.embeddings import get_embedder

SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
SIMILAR_THRESHOLD = 0.82
CHANGED_THRESHOLD = 0.98


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_RE.findall(text) if len(s.strip()) >= 8]


def _cosine(a, b) -> float:
    import numpy as np

    den = float((a @ a) ** 0.5 * (b @ b) ** 0.5)
    return float(a @ b / den) if den else 0.0


def _literal_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def diff_texts(old: str, new: str) -> dict:
    old_sents = split_sentences(old)
    new_sents = split_sentences(new)

    embedder = get_embedder()
    old_vecs = embedder.encode(old_sents) if old_sents else None
    new_vecs = embedder.encode(new_sents) if new_sents else None

    used_old: set[int] = set()
    changed: list[dict] = []
    added: list[str] = []

    for j, new_s in enumerate(new_sents):
        best_i, best_sim = -1, -1.0
        if old_vecs is not None:
            for i, old_s in enumerate(old_sents):
                if i in used_old:
                    continue
                sim = _cosine(old_vecs[i], new_vecs[j])
                if sim > best_sim:
                    best_i, best_sim = i, sim
        if best_i >= 0 and best_sim >= SIMILAR_THRESHOLD:
            used_old.add(best_i)
            old_s = old_sents[best_i]
            lit = _literal_ratio(old_s, new_s)
            if best_sim < CHANGED_THRESHOLD or lit < 0.98:
                changed.append({"old": old_s, "new": new_s, "similarity": round(best_sim, 3), "literal": round(lit, 3)})
        else:
            added.append(new_s)

    removed = [s for i, s in enumerate(old_sents) if i not in used_old]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "stats": {"old_sentences": len(old_sents), "new_sentences": len(new_sents), "added": len(added), "removed": len(removed), "changed": len(changed)},
    }
