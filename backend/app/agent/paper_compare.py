"""
多篇跨文献对比：确定性证据收集，结论可溯源。

不依赖 LLM 生成结论：服务只按维度从每篇论文检索原文证据并结构化返回，
每个单元格都附带固定来源定位（revision/locator，传统PDF保留page/bbox），
由前端展示或由 Agent 基于证据撰写，
避免跨文献对比时出现无法核验的"看起来合理"的结论。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Paper
from ..rag.retriever import retrieve

DEFAULT_DIMENSIONS = [
    {"key": "method", "label": "研究方法",
     "query": "method approach framework model 方法 模型 框架"},
    {"key": "dataset", "label": "数据集",
     "query": "dataset benchmark data corpus 数据集 语料 基准"},
    {"key": "metrics", "label": "评价指标",
     "query": "metrics evaluation measure score 指标 评估 结果"},
    {"key": "conclusion", "label": "主要结论",
     "query": "conclusion finding contribution result 结论 贡献 发现"},
]


def resolve_papers(db: Session, paper_ids: list[str]) -> list[Paper]:
    """按给定顺序返回已解析论文，跳过缺失或未解析的 id。"""
    papers: list[Paper] = []
    for pid in paper_ids:
        paper = db.get(Paper, pid)
        if paper and paper.status == "parsed" and paper not in papers:
            papers.append(paper)
    return papers


def build_comparison(
    db: Session,
    paper_ids: list[str],
    dimensions: list[dict] | None = None,
    evidence_per_cell: int = 2,
    *,
    conversation_id: str | None = None,
) -> dict:
    """构建对比结构：论文元数据 + 每个维度下各论文的可溯源证据。"""
    dimensions = dimensions or DEFAULT_DIMENSIONS
    from ..research_store import checked_papers
    papers = checked_papers(db, paper_ids)
    if len(papers) < 2:
        raise ValueError("跨文献对比需要至少 2 篇已解析论文")
    if conversation_id:
        from ..source_runtime import conversation_scope
        # Resolve scope before retrieving or copying any library source. The
        # caller must already own this conversation; never guess another scope.
        conversation_scope(db, conversation_id)

    paper_meta = [
        {
            "id": p.id,
            "title": p.title or "未命名论文",
            "authors": p.authors,
            "year": (p.published_at or "")[:4],
            "page_count": p.page_count,
            "content_sha256": p.content_hash,
        }
        for p in papers
    ]

    # Embedding requests persist their call ledger in separate short write
    # transactions. Finish *all* retrieval before creating any source aliases
    # or locators in this caller-owned transaction; otherwise the next cell's
    # request would block on the writer lock held by the previous cell.
    retrieved = {}
    for dimension_index, dim in enumerate(dimensions):
        for paper in papers:
            evidences = retrieve(
                db,
                dim["query"],
                topk=evidence_per_cell,
                paper_id=paper.id,
            )
            retrieved[(dimension_index, paper.id)] = [e.to_dict() for e in evidences]

    rows = []
    for dimension_index, dim in enumerate(dimensions):
        cells: dict[str, dict] = {}
        for paper in papers:
            serialized = retrieved[(dimension_index, paper.id)]
            if conversation_id:
                from ..source_library import augment_library_evidence
                serialized = augment_library_evidence(db, serialized, conversation_id)
            for evidence in serialized:
                fixed_locator = bool(evidence.get("revision_id") and evidence.get("locator_id"))
                pdf_page = isinstance(evidence.get("page"), int) and evidence["page"] > 0
                evidence["source_sha256"] = evidence.get("source_sha256") or paper.content_hash
                evidence["source_media_type"] = evidence.get("source_media_type") or (
                    "application/pdf" if pdf_page else None)
                evidence["location_status"] = "fixed_source" if fixed_locator else "pdf_page" if pdf_page else "unavailable"
                # Retrieval is a passage candidate, not a semantic verdict that
                # it supports this comparison dimension or a generated claim.
                evidence["verification_status"] = "unverified"
            cells[paper.id] = {
                "evidence": serialized,
                "quote": " ".join(e["quote"] for e in serialized),
                "status": "unverified" if serialized else "not_found",
            }
        rows.append({"key": dim["key"], "label": dim["label"], "cells": cells})

    return {
        "papers": paper_meta,
        "dimensions": [
            {"key": d["key"], "label": d["label"]} for d in dimensions
        ],
        "rows": rows,
        "conversation_id": conversation_id,
        "comparison_method": "retrieved_passages",
    }
