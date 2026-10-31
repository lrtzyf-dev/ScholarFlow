"""
混合检索：FTS5(BM25) + 向量，RRF 融合。
输出证据包 Evidence：chunk_id / page / bbox / quote / score。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import Chunk, Paper
from ..pipeline.embeddings import get_embedder
from ..pipeline.indexer import cjk_spaced
from ..pipeline.vectordb import NumpyVectorStore

RRF_K = 60
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class Evidence:
    id: str
    chunk_id: str
    paper_id: str
    paper_title: str
    page: int
    bbox: list[float] | None
    quote: str
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "paper_title": self.paper_title,
            "page": self.page,
            "bbox": self.bbox,
            "quote": self.quote,
            "score": round(self.score, 5),
        }


def _fts_query(query: str) -> str:
    q = query.lower()
    terms = _WORD.findall(q)
    terms.extend(_CJK.findall(q))
    terms = [t for t in terms if t.strip()]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


def fts_search(db: Session, query: str, topk: int, paper_id: str | None) -> list[str]:
    match = _fts_query(query)
    if not match:
        return []
    sql = "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH :q ORDER BY rank LIMIT :n"
    params: dict = {"q": match, "n": topk}
    if paper_id:
        sql = "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH :q AND paper_id = :pid ORDER BY rank LIMIT :n"
        params["pid"] = paper_id
    rows = db.execute(text(sql), params).fetchall()
    return [r[0] for r in rows]


def dense_search(query: str, topk: int, paper_id: str | None) -> list[tuple[str, float]]:
    vec = get_embedder().encode([query])[0]
    return NumpyVectorStore().search(vec, topk=topk, paper_id=paper_id)


def rrf_fuse(rank_lists: list[list[str]], topk: int) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranked in rank_lists:
        for rank, cid in enumerate(ranked):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:topk]


_STOPWORDS = {"the", "a", "an", "of", "to", "in", "on", "and", "or", "is", "are", "was", "how", "what", "does", "do", "many", "use", "with", "for", "that"}


def _quote_snippet(content: str, query: str, max_len: int = 220) -> str:
    text = content.lower()
    q_terms = [t for t in _WORD.findall(query.lower()) if t not in _STOPWORDS and len(t) > 2]
    q_terms += _CJK.findall(query)
    # 统计每个词在内容中的出现次数，稀有词（如 BLEU 只出现一次）权重更高
    freq = {t: text.count(t) for t in q_terms}
    occurrences: list[int] = []
    for term in q_terms:
        start_f = 0
        while True:
            pos = text.find(term, start_f)
            if pos < 0:
                break
            occurrences.append(pos)
            start_f = pos + 1
    if not occurrences:
        return content[:max_len]

    def _window_score(center: int) -> float:
        s = max(0, center - max_len // 2)
        e = min(len(content), s + max_len)
        window = text[s:e]
        return sum(1.0 / (1 + freq.get(t, 0)) for t in set(q_terms) if t in window)

    best = max(occurrences, key=lambda p: (_window_score(p), -freq.get(text[p:p + 1], 0)))
    start = max(0, best - max_len // 2)
    end = min(len(content), start + max_len)
    return ("…" if start > 0 else "") + content[start:end] + ("…" if end < len(content) else "")


def _mmr_select(query_vec, cand_vecs, scores, topk: int,
                lambda_mult: float = 0.6, dup_threshold: float = 0.92):
    """最大边际相关选择：兼顾与查询的相关度、与已选块的差异度。

    query_vec/cand_vecs 均为 L2 归一化向量，故点积即余弦相似度。
    近重复（与任一已选块余弦 > dup_threshold）直接排除，保证 top-k 不重复。
    """
    import numpy as np

    n = len(cand_vecs)
    qsim = cand_vecs @ query_vec  # 与查询的相关度
    pool = list(range(n))
    selected: list[int] = []

    while pool and len(selected) < topk:
        if not selected:
            # 首块取融合分最高（融合排序最可靠）
            best = max(pool, key=lambda i: scores[i])
        else:
            sel_mat = cand_vecs[selected]
            # 每个候选与已选块的最大相似度
            sim_to_sel = (sel_mat @ cand_vecs[pool].T).max(axis=0)
            cand_q = qsim[pool]
            mmr = lambda_mult * cand_q - (1 - lambda_mult) * sim_to_sel
            # 近重复硬排除：相似度超阈值的候选不参与
            valid = [i for i, s in zip(pool, sim_to_sel) if s < dup_threshold]
            if not valid:
                break
            valid_set = set(valid)
            best = max(pool, key=lambda i: mmr[pool.index(i)] if i in valid_set else -1e9)
            if best not in valid_set:
                break
        selected.append(best)
        pool.remove(best)
    return selected


def retrieve(db: Session, query: str, topk: int = 6, paper_id: str | None = None) -> list[Evidence]:
    candidate_k = topk * 4
    bm25_ids = fts_search(db, query, candidate_k, paper_id)
    dense_hits = dense_search(query, candidate_k, paper_id)
    # 先融合出较大候选池，再做 MMR 去重/多样性选择
    fused = rrf_fuse([bm25_ids, [cid for cid, _ in dense_hits]], topk=candidate_k)
    if not fused:
        return []

    fused_ids = [cid for cid, _ in fused]
    score_map = dict(fused)
    rows = db.query(Chunk).filter(Chunk.id.in_(fused_ids)).all()
    # 按融合顺序排列候选
    rows = sorted(rows, key=lambda r: fused_ids.index(r.id))
    if not rows:
        return []

    # 计算查询与候选块向量（复用当前 embedder；离线为哈希向量、在线为语义向量）
    embedder = get_embedder()
    import numpy as np

    qv = embedder.encode([query])[0]
    qv = qv / (np.linalg.norm(qv) or 1.0)
    cv = embedder.encode([r.content for r in rows])
    norms = np.linalg.norm(cv, axis=1, keepdims=True)
    cv = cv / np.where(norms > 0, norms, 1.0)
    cand_scores = [score_map.get(r.id, 0.0) for r in rows]
    chosen = _mmr_select(qv, cv, cand_scores, topk=topk)

    paper_cache: dict[str, str] = {}
    out: list[Evidence] = []
    for ri in chosen:
        row = rows[ri]
        if row.paper_id not in paper_cache:
            p = db.get(Paper, row.paper_id)
            paper_cache[row.paper_id] = p.title if p else ""
        out.append(
            Evidence(
                id=f"e{len(out) + 1}",
                chunk_id=row.id,
                paper_id=row.paper_id,
                paper_title=paper_cache[row.paper_id],
                page=row.page,
                bbox=row.bbox,
                quote=_quote_snippet(row.content, query),
                score=score_map.get(row.id, 0.0),
            )
        )
    # 保持 MMR 顺序（已按相关度+多样性排序），重新连续编号
    for i, ev in enumerate(out, 1):
        ev.id = f"e{i}"
    return out
