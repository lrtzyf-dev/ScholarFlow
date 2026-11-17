"""引用关系图谱 API：同篇合并 + 分层匹配 + PageRank 中心性。"""
import re
import sqlite3
from collections import defaultdict
from typing import Optional, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..algorithms.centrality import degree_centrality, pagerank
from ..db import get_db
from ..graph_projection import load_graph
from ..models import Paper, Reference
from ..parsing.identifiers import extract_arxiv_id, extract_doi

router = APIRouter(prefix="/api/graph", tags=["graph"])

# 高频通用词（中英文）：匹配前剔除，避免短标题靠通用词误连。
_STOPWORDS = {
    "a", "an", "the", "of", "for", "on", "in", "to", "and", "or", "with",
    "via", "using", "based", "is", "are", "be", "by", "from", "at", "as",
    "this", "that", "we", "our", "their", "its", "study", "approach",
    "method", "methods", "model", "models", "paper", "novel",
}

# Jaccard 连边阈值与短标题共同词门槛（与具体样本无关的通用值）。
_TITLE_JACCARD = 0.5
_SHORT_TOKENS = 5
_SHORT_COMMON = 3


def normalize_title(text: Optional[str]) -> str:
    """标题归一化：小写、标点转空格、压缩空白，中英文通用。"""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text: Optional[str]) -> set:
    """标题 token 集合：英文词 + 中文单字与相邻二元组，已去停用词。"""
    norm = normalize_title(text)
    tokens = set()
    for word in re.findall(r"[a-z0-9]+", norm):
        if word not in _STOPWORDS:
            tokens.add(word)
    cjk = re.findall(r"[\u4e00-\u9fff]", norm)
    tokens.update(cjk)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i + 1])
    return tokens


def _build_groups(papers: list[Paper]) -> list[dict]:
    """按 version_group 把同篇论文（含多版本）合并为一个节点组。"""
    grouped: dict[str, list[Paper]] = defaultdict(list)
    standalone: list[Paper] = []
    for p in papers:
        if p.version_group:
            grouped[p.version_group].append(p)
        else:
            standalone.append(p)

    groups: list[dict] = []

    def make_group(members: list[Paper]) -> dict:
        # 代表论文：标题非空且最长者（信息最全），否则取首个；组 id 用代表 paper id。
        rep = max(
            members,
            key=lambda m: len(m.title or ""),
            default=members[0],
        )
        arxiv_ids, dois = set(), set()
        for m in members:
            blob = f"{m.title or ''} {m.arxiv_id or ''}"
            aid = m.arxiv_id or extract_arxiv_id(blob)
            if aid:
                arxiv_ids.add(aid)
            doi = extract_doi(blob)
            if doi:
                dois.add(doi)
        return {
            "id": rep.id,
            "title": rep.title or members[0].title or "未命名",
            "members": [m.id for m in members],
            "version_count": len(members),
            "arxiv_ids": arxiv_ids,
            "dois": dois,
            "tokens": title_tokens(rep.title or ""),
            "page_count": sum(m.page_count or 0 for m in members),
            "published_at": rep.published_at,
        }

    for members in grouped.values():
        groups.append(make_group(members))
    for p in standalone:
        groups.append(make_group([p]))
    return groups


def _match_reference(ref: Reference, groups: list[dict]) -> Optional[tuple]:
    """分层匹配一条参考文献到最优组：编号精确匹配优先，归一化标题兜底。

    返回 (group_id, match_type)；无可靠匹配返回 None。标题层选全局最优，
    不依赖遍历顺序，也不强制一对一。
    """
    blob = f"{ref.title or ''} {ref.raw_text or ''}"
    ref_arxiv = extract_arxiv_id(blob)
    ref_doi = extract_doi(blob)

    # 第一层：编号精确匹配（arXiv 编号 / DOI）
    if ref_arxiv or ref_doi:
        for g in groups:
            if ref_arxiv and ref_arxiv in g["arxiv_ids"]:
                return g["id"], "arxiv_id"
            if ref_doi and ref_doi in g["dois"]:
                return g["id"], "doi"

    # 第二层：归一化标题，全局选 Jaccard 最高者
    rtokens = title_tokens(ref.title or ref.raw_text or "")
    if not rtokens:
        return None
    best, best_score = None, 0.0
    for g in groups:
        if not g["tokens"]:
            continue
        common = rtokens & g["tokens"]
        if not common:
            continue
        union = rtokens | g["tokens"]
        score = len(common) / len(union)
        if score > best_score:
            best, best_score = g, score
    if best is None:
        return None
    common_n = len(rtokens & best["tokens"])
    short_ok = len(rtokens) <= _SHORT_TOKENS and common_n >= _SHORT_COMMON
    if best_score >= _TITLE_JACCARD or short_ok:
        return best["id"], "title"
    return None


def _compute_full_graph(db: Session) -> dict:
    """Build against all parsed papers inside the projection's write snapshot.

    Never commit here: extracted relation revisions and the cached graph must
    become visible together, or all roll back if matching/serialization fails.
    """
    papers = db.query(Paper).filter(Paper.status == "parsed").order_by(Paper.id).all()
    groups = _build_groups(papers)
    group_index = {gid: g for g in groups for gid in g["members"]}
    group_ids = {g["id"] for g in groups}

    references = db.query(Reference).order_by(Reference.id).all()
    edge_set = set()
    edges = []
    from ..research_relations import sync_relation, relation_id
    from ..research_models import RelationEvidence
    paper_index = {p.id:p for p in papers}
    # Reference matches and human corrections share the same revision stream.
    links: dict[str, str] = {}
    for ref in references:
        source_group = group_index.get(ref.paper_id)
        if not source_group:
            continue
        matched = _match_reference(ref, groups)
        if not matched:
            previous = db.get(RelationEvidence, relation_id(ref, paper_index[ref.paper_id]))
            if previous is None or not previous.manual:
                continue
            # A reviewed correction remains usable when the old automatic
            # target is removed. Preserve original extraction for audit.
            matched = (previous.extracted["target"], previous.extracted["match_type"])
        target_id, match_type = matched
        relation = sync_relation(db, ref, paper_index[ref.paper_id], source_group["id"], target_id, match_type)
        effective = relation.effective
        if effective["status"] == "excluded":
            continue
        target = group_index.get(effective["target"])
        target_id = target["id"] if target else effective["target"]
        if target_id not in group_ids:
            continue
        if ref.id is not None and effective["relation_type"] == "citation":
            links[ref.id] = target_id
        if source_group["id"] == target_id:
            continue  # 同篇自引不连边
        key = (source_group["id"], target_id, relation.id)
        if key in edge_set:
            continue
        edge_set.add(key)
        via = ref.title or ref.raw_text or ""
        edges.append({"source": key[0], "target": target_id, "via": via[:120],
                      "match_type": effective["match_type"], "relation_id": relation.id,
                      "relation_version": relation.version, "relation_type": effective["relation_type"],
                      "status": effective["status"]})

    # Clear outdated derived links too (removed targets, excluded corrections,
    # reparsing and status changes). This output is excluded from input hashes.
    for ref in references:
        target = links.get(ref.id)
        if ref.linked_paper_id != target:
            ref.linked_paper_id = target

    node_ids = [g["id"] for g in groups]
    raw_edges = [(e["source"], e["target"]) for e in edges]
    pr = pagerank(node_ids, raw_edges)
    in_cent, out_cent = degree_centrality(node_ids, raw_edges)

    nodes = []
    for g in groups:
        nodes.append({
            "id": g["id"],
            "title": g["title"],
            "member_ids": g["members"],
            "version_count": g["version_count"],
            "page_count": g["page_count"],
            "published_at": g["published_at"],
            "pagerank": pr.get(g["id"], 0.0),
            "in_centrality": in_cent.get(g["id"], 0.0),
            "out_centrality": out_cent.get(g["id"], 0.0),
        })
    ranking = sorted(
        [{"id": g["id"], "title": g["title"], "pagerank": pr.get(g["id"], 0.0)}
         for g in groups],
        key=lambda x: x["pagerank"], reverse=True,
    )
    return {"nodes": nodes, "edges": edges, "ranking": ranking}


def _select_graph(graph: dict, paper_ids: Optional[set] = None,
                  focus: str | None = None, relation_type: str | None = None) -> dict:
    """Filter the full-library match, then recompute the selected graph's ranks.

    A version member selects its full group; it never changes the representative
    or re-matches a reference to a weaker candidate inside the user's subset.
    """
    if paper_ids is None and not focus and not relation_type:
        return graph
    nodes = [dict(n) for n in graph["nodes"]
             if paper_ids is None or paper_ids.intersection(n["member_ids"])]
    ids = {n["id"] for n in nodes}
    edges = [e for e in graph["edges"] if e["source"] in ids and e["target"] in ids
             and (not relation_type or e["relation_type"] == relation_type)]
    if focus:
        # Accept a nonrepresentative version as a focus as well.
        center = next((n["id"] for n in nodes if focus in n["member_ids"]), focus)
        neighbors = {center}
        for edge in edges:
            if center in (edge["source"], edge["target"]):
                neighbors.update((edge["source"], edge["target"]))
        nodes = [n for n in nodes if n["id"] in neighbors]
        ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in ids and e["target"] in ids]
    node_ids = [n["id"] for n in nodes]
    raw_edges = [(e["source"], e["target"]) for e in edges]
    ranks = pagerank(node_ids, raw_edges)
    in_cent, out_cent = degree_centrality(node_ids, raw_edges)
    for node in nodes:
        node.update(pagerank=ranks[node["id"]], in_centrality=in_cent[node["id"]],
                    out_centrality=out_cent[node["id"]])
    ranking = sorted(
        [{"id": n["id"], "title": n["title"], "pagerank": n["pagerank"]} for n in nodes],
        key=lambda n: n["pagerank"], reverse=True,
    )
    return {"nodes": nodes, "edges": edges, "ranking": ranking}


def _load_projection(db: Session) -> dict:
    try:
        return load_graph(db, _compute_full_graph)
    except OperationalError as exc:
        code = getattr(exc.orig, "sqlite_errorcode", 0)
        if code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise HTTPException(503, "图谱正在更新，请稍后重试", headers={"Retry-After": "2"}) from None
        raise


def _build_graph(db: Session, paper_ids: Optional[set] = None) -> dict:
    return _select_graph(_load_projection(db), paper_ids)


@router.get("")
def get_graph(
    paper_ids: Optional[str] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    focus: str | None = None,
    relation_type: str | None = None,
    db: Session = Depends(get_db),
):
    """返回引用图谱。可选 paper_ids（逗号分隔）在检索结果子图上计算中心性。"""
    subset = None
    if paper_ids:
        subset = {x.strip() for x in paper_ids.split(",") if x.strip()}
    graph = _select_graph(_load_projection(db), subset, focus, relation_type)
    ordered = sorted(graph["nodes"], key=lambda n:n["id"])
    nodes = ordered[offset:offset + limit]
    ids = {n["id"] for n in nodes}
    return {**graph, "nodes": nodes,
        "edges": [e for e in graph["edges"] if e["source"] in ids or e["target"] in ids],
        "total_nodes": len(ordered), "next_offset": offset + limit if offset + limit < len(ordered) else None,
        "ranking": [r for r in graph["ranking"] if r["id"] in ids]}


@router.get("/diagram")
def get_diagram(fmt: str = "mermaid", db: Session = Depends(get_db)):
    graph = _build_graph(db)
    lines = []
    if fmt == "mermaid":
        lines.append("graph LR")
        for e in graph["edges"]:
            src = _safe(e["source"])
            tgt = _safe(e["target"])
            connector = "-->" if e["relation_type"] == "citation" else "-.-"
            lines.append(f"    {src} {connector} {tgt}")
    else:
        lines.append("digraph citations {")
        for e in graph["edges"]:
            style = '' if e["relation_type"] == "citation" else ' [style=dashed, dir=none]'
            lines.append(f'  "{e["source"]}" -> "{e["target"]}"{style};')
        lines.append("}")
    return {"format": fmt, "script": "\n".join(lines)}


def _safe(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", node_id)
