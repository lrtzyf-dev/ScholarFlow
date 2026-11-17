"""可溯源文献收集：只从本地文献库取真实论文，按 idea 相关度排序并编号。

不创建任何文献条目之外的信息，不编造标题、作者或链接。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sqlalchemy.orm import Session

from ..models import Paper


@dataclass
class SourceRef:
    ref_index: int
    paper_id: str
    title: str
    authors: str
    arxiv_id: str | None
    url: str
    published_at: str | None
    relevance: float
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ref_index": self.ref_index,
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": self.authors,
            "arxiv_id": self.arxiv_id,
            "url": self.url,
            "published_at": self.published_at,
            "relevance": round(self.relevance, 4),
            "evidence": self.evidence,
        }


def _normalized(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms > 0, norms, 1.0)


def collect_sources(db: Session, idea: str, embedder,
                    max_sources: int = 8, paper_snapshot=None) -> list[SourceRef]:
    from types import SimpleNamespace
    papers = (db.query(Paper).filter(Paper.status == "parsed").all() if paper_snapshot is None
        else [SimpleNamespace(**paper) for paper in paper_snapshot])
    if not papers:
        return []

    texts = [f"{p.title} {p.abstract}".strip() for p in papers]
    paper_vecs = _normalized(embedder.encode(texts))
    idea_vec = _normalized(embedder.encode([idea]))[0]
    similarities = paper_vecs @ idea_vec

    ranked = sorted(
        zip(papers, similarities), key=lambda x: x[1], reverse=True
    )[:max_sources]

    refs: list[SourceRef] = []
    for index, (paper, similarity) in enumerate(ranked, 1):
        url = (
            f"https://arxiv.org/abs/{paper.arxiv_id}"
            if paper.arxiv_id else ""
        )
        refs.append(
            SourceRef(
                ref_index=index,
                paper_id=paper.id,
                title=paper.title,
                authors=paper.authors,
                arxiv_id=paper.arxiv_id,
                url=url,
                published_at=paper.published_at,
                relevance=float(similarity),
            )
        )
    return refs
