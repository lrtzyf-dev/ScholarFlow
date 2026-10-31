"""
局域引用图谱 v0：
- 节点 = 库内论文
- 边 = 论文 A 的参考文献条目通过标题 token 重合匹配到库内论文 B
跨库真实引用（arXiv 元数据）在 W4/W5 补齐。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Paper, Reference

router = APIRouter(prefix="/api/graph", tags=["graph"])

_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    toks = {w for w in _WORD.findall(text) if len(w) > 2}
    cjk = _CJK.findall(text)
    toks.update(cjk)
    toks.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return toks


@router.get("")
def get_graph(db: Session = Depends(get_db)):
    papers = db.query(Paper).filter(Paper.status == "parsed").all()
    paper_tokens = {p.id: _tokens(p.title) for p in papers}

    nodes = [
        {"id": p.id, "title": p.title or "未命名论文", "page_count": p.page_count}
        for p in papers
    ]
    edges = []
    for p in papers:
        refs = db.query(Reference).filter(Reference.paper_id == p.id).all()
        for ref in refs:
            ref_toks = _tokens(ref.raw_text)
            if not ref_toks:
                continue
            for other in papers:
                if other.id == p.id:
                    continue
                target = paper_tokens[other.id]
                if not target:
                    continue
                overlap = len(ref_toks & target) / max(3, min(len(target), len(ref_toks)))
                # 严格阈值，避免误连
                if overlap >= 0.55 or (len(target) <= 5 and len(ref_toks & target) >= 3):
                    edges.append({"source": p.id, "target": other.id, "via": ref.raw_text[:120]})
                    break

    return {"nodes": nodes, "edges": edges}
