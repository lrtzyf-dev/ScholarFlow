"""订阅刷新编排：抓取、编码、相关性评分、去重与持久化。

可注入 fetcher 与 embedder，便于离线测试；网络失败结构化记录，不伪造结果。
"""
from __future__ import annotations

import numpy as np

from ..models import Paper, Recommendation
from ..pipeline.embeddings import get_embedder
from .arxiv_fetcher import fetch_arxiv
from .scoring import build_reasons, score_candidate

MAX_CANDIDATES = 10


def _local_papers_and_vectors(db, embedder):
    papers = db.query(Paper).filter(Paper.status == "parsed").all()
    texts = [
        f"{p.title or ''} {p.abstract or ''}".strip() for p in papers
    ]
    if not texts:
        return [], None
    return papers, embedder.encode(texts)


def run_subscription(db, subscription, *, fetcher=fetch_arxiv, embedder=None):
    """执行一次订阅刷新，返回结构化结果摘要。"""
    embedder = embedder or get_embedder()

    # 持久化先于动作：先标记 running 再抓取，崩溃后可识别
    subscription.last_status = "running"
    db.commit()

    try:
        candidates = fetcher(subscription.topic, MAX_CANDIDATES)
    except Exception as exc:  # 网络/HTTP 错误结构化记录
        subscription.last_status = f"failed: {type(exc).__name__}"
        db.commit()
        return {"status": "failed", "error": str(exc), "added": 0}

    existing = {
        r.arxiv_id for r in subscription.recommendations if r.arxiv_id
    }
    local_papers, local_vecs = _local_papers_and_vectors(db, embedder)
    topic_vec = embedder.encode([subscription.topic])[0]

    added = 0
    if candidates:
        cand_texts = [
            f"{c['title']} {c['abstract']}".strip() for c in candidates
        ]
        cand_vecs = embedder.encode(cand_texts)

        for candidate, cvec in zip(candidates, cand_vecs):
            if candidate["arxiv_id"] and candidate["arxiv_id"] in existing:
                continue
            topic_sim = max(0.0, float(np.dot(cvec, topic_vec)))
            local_max, local_title = 0.0, None
            if local_vecs is not None:
                sims = local_vecs @ cvec
                idx = int(np.argmax(sims))
                local_max = max(0.0, float(sims[idx]))
                local_title = local_papers[idx].title

            has_github = bool(candidate["github_url"])
            score = score_candidate(topic_sim, local_max, has_github)
            reasons = build_reasons(topic_sim, local_max, local_title, has_github)
            db.add(Recommendation(
                subscription_id=subscription.id,
                arxiv_id=candidate["arxiv_id"],
                title=candidate["title"],
                authors=candidate["authors"],
                abstract=candidate["abstract"],
                published_at=candidate["published_at"],
                url=candidate["url"],
                github_url=candidate["github_url"],
                score=score,
                reasons=reasons,
                feedback=None,
            ))
            added += 1

    subscription.last_status = "ok"
    db.commit()
    return {
        "status": "ok",
        "added": added,
        "total": len(subscription.recommendations),
    }
