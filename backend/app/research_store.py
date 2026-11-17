"""Research persistence with fixed sources, append-only history and CAS writes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select, update
from . import source_store as sources
from .source_runtime import conversation_scope
from .source_models import Source, SourceRevision, SourceLibraryImport
from .source_library import open_library_source
from .models import Paper
from .research_models import ResearchMatrix, MatrixRevision, MatrixCreationRequest
from .timeutil import utcnow


def checked_papers(db, ids):
    if not 2 <= len(ids) <= 200 or len(set(ids)) != len(ids):
        raise sources.SourceError("请选择2至200份不同资料")
    papers, problems = [], []
    for pid in ids:
        paper = db.get(Paper, pid)
        if paper is None:
            problems.append(f"{pid}: 不存在")
        elif paper.status != "parsed":
            problems.append(f"{paper.title or pid}: 尚未解析({paper.status})")
        else:
            papers.append(paper)
    if problems:
        raise sources.SourceConflict("所选资料未完整就绪，未静默移除任何资料：" + "；".join(problems))
    return papers


def pin_paper(db, paper, cid):
    record, _ = open_library_source(db, paper.id, cid)
    if record["raw_sha256"] != paper.content_hash:
        raise sources.SourceConflict("固定来源与当前文献不一致，请创建新矩阵版本")
    return {"id": paper.id, "title": paper.title or "未命名资料", "authors": paper.authors,
        "year": (paper.published_at or "")[:4], "page_count": paper.page_count,
        "content_sha256": record["raw_sha256"], "source_id": record["source_id"],
        "revision_id": record["revision_id"], "source_version": record["version"],
        "source_media_type": record["media_type"]}


def empty_cell():
    return {"evidence": [], "quote": "", "status": "not_processed", "extraction_status": "not_processed",
        "verification_status": "unverified", "manual": None, "extraction_history": []}


def create_matrix(db, cid, title, paper_ids, fields, request_id=None, actor="local_user"):
    payload = hashlib.sha256(json.dumps([title,paper_ids,fields],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if request_id:
        receipt=db.scalar(select(MatrixCreationRequest).where(MatrixCreationRequest.conversation_id==cid,
            MatrixCreationRequest.request_id==request_id))
        if receipt:
            if receipt.payload_sha256 != payload:
                raise sources.SourceConflict("创建请求编号已绑定不同内容，请保留草稿并重新提交")
            return matrix(db,receipt.matrix_id,cid,writing=True)
    document=matrix_document(db,cid,paper_ids,fields)
    item=ResearchMatrix(conversation_id=cid,title=title,document=document)
    db.add(item);db.flush()
    db.add(MatrixRevision(matrix_id=item.id,version=1,action="create",actor=actor,document={"title":item.title,"data":document}))
    if request_id:
        db.add(MatrixCreationRequest(conversation_id=cid,request_id=request_id,payload_sha256=payload,matrix_id=item.id))
    return item


def matrix_document(db, cid, paper_ids, fields, previous=None):
    papers = checked_papers(db, paper_ids)
    frozen = [pin_paper(db, p, cid) for p in papers]
    previous = previous or {}
    old_fields = {f["key"]: f for f in previous.get("dimensions", [])}
    old_rows = {r["key"]: r for r in previous.get("rows", [])}
    old_papers = {p["id"]: p for p in previous.get("papers", [])}
    rows = []
    for field in fields:
        cells = {}
        for paper in frozen:
            prior = old_rows.get(field["key"], {}).get("cells", {}).get(paper["id"])
            compatible = old_fields.get(field["key"]) == field and old_papers.get(paper["id"], {}).get("content_sha256") == paper["content_sha256"]
            cells[paper["id"]] = deepcopy(prior) if prior and compatible else empty_cell()
        rows.append({"key": field["key"], "label": field["label"], "cells": cells})
    return {"papers": frozen, "dimensions": fields, "rows": rows, "conversation_id": cid,
        "schema_version": previous.get("schema_version", 0) + 1, "comparison_method": "retrieved_passages"}


def matrix(db, matrix_id, cid, writing=False):
    conversation_scope(db, cid, writing=writing)
    item = db.get(ResearchMatrix, matrix_id, populate_existing=True)
    if item is None or item.conversation_id != cid:
        raise sources.SourceNotFound("此会话没有该证据矩阵")
    return item


def matrix_output(item):
    return {"id": item.id, "title": item.title, "version": item.version,
        "conversation_id": item.conversation_id, "document": item.document,
        "updated_at": item.updated_at.isoformat()}


def save_matrix(db, item, document, expected, action, title=None, actor="local_user"):
    if item.version != expected:
        raise sources.SourceConflict("矩阵已被其他窗口修改，请重新打开后合并")
    title = title if title is not None else item.title
    result = db.execute(update(ResearchMatrix).where(ResearchMatrix.id == item.id,
        ResearchMatrix.version == expected).values(document=deepcopy(document), title=title,
        version=expected + 1, updated_at=utcnow()), execution_options={"synchronize_session": False})
    if result.rowcount != 1:
        raise sources.SourceConflict("矩阵版本冲突，修改未覆盖其他窗口")
    db.add(MatrixRevision(matrix_id=item.id, version=expected + 1, action=action, actor=actor,
        document={"title": title, "data": deepcopy(document)}))
    db.flush()
    db.refresh(item)
    return item


def extract_matrix(db, item, expected, field_keys=None, paper_ids=None):
    if item.version != expected:
        raise sources.SourceConflict("矩阵已更新，请重新打开")
    cid, mid, original = item.conversation_id, item.id, deepcopy(item.document)
    ids = paper_ids or [p["id"] for p in original["papers"]]
    if not set(ids) <= {p["id"] for p in original["papers"]}:
        raise sources.SourceError("重提取包含不属于此矩阵的资料")
    fields = [f for f in original["dimensions"] if not field_keys or f["key"] in field_keys]
    if not fields or (field_keys and set(field_keys) != {f["key"] for f in fields}):
        raise sources.SourceError("请选择矩阵已有字段")
    for p in original["papers"]:
        if p["id"] in ids:
            current = db.get(Paper, p["id"])
            if not current or current.status != "parsed" or current.content_hash != p["content_sha256"]:
                raise sources.SourceConflict("所选来源已变化或缺失，固定版本未被替换")
    # All retrieval occurs before any source/locator writer: embedding ledger
    # uses separate durable transactions. Never hold a writer over network I/O.
    db.rollback()
    from .rag.retriever import retrieve
    results = {(f["key"], pid): [e.to_dict() for e in retrieve(db, f["query"], topk=2, paper_id=pid)]
               for f in fields for pid in ids}
    db.rollback()
    from sqlalchemy import text
    db.execute(text("BEGIN IMMEDIATE"))
    item = matrix(db, mid, cid, writing=True)
    if item.version != expected:
        raise sources.SourceConflict("提取期间矩阵有新修改，请重试；人工修改已保留")
    from .source_library import augment_library_evidence
    scope = conversation_scope(db, cid)
    for row in original["rows"]:
        for pid, cell in row["cells"].items():
            key = (row["key"], pid)
            if key not in results:
                continue
            p = next(p for p in original["papers"] if p["id"] == pid)
            current = db.get(Paper, pid, populate_existing=True)
            if not current or current.content_hash != p["content_sha256"] or current.status != "parsed":
                raise sources.SourceConflict("提取期间文献已变化，未覆盖固定来源")
            evidence = augment_library_evidence(db, results[key], cid)
            for e in evidence:
                if not e.get("locator_id") and p["source_media_type"] == "application/pdf" and e.get("page", 0) > 0:
                    loc = sources.add_pdf_locator(db, p["revision_id"], scope=scope, page_index=e["page"] - 1, bbox=e.get("bbox"))
                    e.update(locator_id=loc["locator_id"], revision_id=p["revision_id"], source_id=p["source_id"],
                        source_location=loc["location"], locator_kind=loc["kind"], quote=loc["quote"])
                if e.get("revision_id") != p["revision_id"]:
                    raise sources.SourceConflict("检索结果不属于矩阵固定版本，未保存")
                e.update(source_sha256=p["content_sha256"], source_version=p["source_version"],
                    source_media_type=p["source_media_type"], location_status="fixed_source", verification_status="unverified")
            # History is append-only at the matrix revision level; the prior
            # extracted value also stays next to each manually corrected cell.
            previous = {k: deepcopy(cell.get(k)) for k in ("evidence", "quote", "status", "extraction_status")}
            cell.setdefault("extraction_history", []).append(previous)
            cell.update(evidence=evidence, quote=" ".join(e["quote"] for e in evidence),
                extraction_status="retrieved" if evidence else "not_found")
            if not cell.get("manual"):
                cell.update(status="unverified" if evidence else "not_found", verification_status="unverified")
    return save_matrix(db, item, original, expected, "extract", actor="retrieval")


def export_matrix(db, item):
    history = list(db.scalars(select(MatrixRevision).where(MatrixRevision.matrix_id == item.id).order_by(MatrixRevision.version)))
    return {"format": "scholarflow-evidence-matrix", "format_version": 1, "title": item.title,
        "document": item.document, "exported_version": item.version,
        "history": [{"version": r.version, "action": r.action, "actor": r.actor, "document": r.document,
            "created_at": r.created_at.isoformat()} for r in history],
        "contains_original_files": False, "requires_original_source_scope": item.conversation_id}


def validate_import(db, payload, cid):
    # The package is data, not instructions or an authorization to cross scopes.
    if payload.get("format") != "scholarflow-evidence-matrix" or payload.get("format_version") != 1:
        raise sources.SourceError("不支持的矩阵文件格式")
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 12_000_000:
        raise sources.SourceError("矩阵文件超过12MB，请分开导入")
    doc = deepcopy(payload.get("document"))
    if not isinstance(doc, dict) or doc.get("conversation_id") != cid:
        raise sources.SourceAccessDenied("请在原来源所属会话导入，文件未包含原件，不能跨会话借用定位")
    scope = conversation_scope(db, cid)
    papers, fields, rows = doc.get("papers", []), doc.get("dimensions", []), doc.get("rows", [])
    if not 2 <= len(papers) <= 200 or not 1 <= len(fields) <= 40 or len(rows) != len(fields):
        raise sources.SourceError("矩阵清单或字段结构无效")
    ids = {p["id"] for p in papers}
    if len(ids) != len(papers) or len({f["key"] for f in fields}) != len(fields):
        raise sources.SourceError("矩阵包含重复的来源或字段")
    if type(doc.get("schema_version")) is not int or doc["schema_version"] < 1:
        raise sources.SourceError("字段版本无效")
    for f in fields:
        if (not isinstance(f.get("key"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", f["key"])
                or not isinstance(f.get("label"), str) or not 1 <= len(f["label"]) <= 120
                or not isinstance(f.get("query"), str) or not 1 <= len(f["query"]) <= 1000):
            raise sources.SourceError("导入字段结构无效")
    for paper in papers:
        record = sources.read_revision(db, paper["revision_id"], scope=scope)
        if (record["raw_sha256"] != paper["content_sha256"] or record["version"] != paper["source_version"]
                or record["source_id"] != paper.get("source_id") or record["media_type"] != paper.get("source_media_type")):
            raise sources.SourceConflict("导入的来源版本或校验值不一致")
    valid_status = {"supported", "conflict", "not_found", "not_processed", "unverified"}
    by_id = {p["id"]: p for p in papers}
    if [r["key"] for r in rows] != [f["key"] for f in fields]:
        raise sources.SourceError("矩阵字段与行不一致")
    for row in rows:
        if set(row.get("cells", {})) != ids:
            raise sources.SourceError("矩阵单元格未覆盖完整来源清单")
        for pid, cell in row["cells"].items():
            if cell.get("status") not in valid_status:
                raise sources.SourceError("未知单元格状态")
            if (cell.get("extraction_status") not in {"retrieved", "not_found", "not_processed"}
                    or cell.get("verification_status") not in {"unverified", "human_reviewed"}
                    or not isinstance(cell.get("evidence"), list) or not isinstance(cell.get("quote"), str)):
                raise sources.SourceError("单元格提取或核验状态无效")
            manual = cell.get("manual")
            if manual is not None and (not isinstance(manual, dict) or manual.get("status") not in valid_status
                    or not isinstance(manual.get("value"), str) or len(manual["value"]) > 10000
                    or not isinstance(manual.get("note"), str) or len(manual["note"]) > 10000):
                raise sources.SourceError("人工修订内容无效")
            if cell["status"] in {"supported", "conflict"} and not cell["evidence"]:
                raise sources.SourceError("导入单元格缺少支持或冲突对应证据")
            candidate = cell.get("extraction_candidate")
            if candidate is not None:
                from .research_runs import digest
                if (not isinstance(candidate, dict) or candidate.get("requires_review") is not True
                        or candidate.get("verification_status") != "unverified"
                        or candidate.get("extraction_status") not in {"retrieved", "not_found"}
                        or not isinstance(candidate.get("quote"), str) or not isinstance(candidate.get("evidence"), list)
                        or digest({k: v for k, v in candidate.items() if k != "sha256"}) != candidate.get("sha256")):
                    raise sources.SourceError("导入的新提取候选校验失败，未改变人工复核结论")
            for e in [*cell.get("evidence", []), *((candidate or {}).get("evidence", []))]:
                loc = sources.read_locator(db, e["locator_id"], scope=scope)
                if (loc["revision_id"] != by_id[pid]["revision_id"] or e.get("revision_id") != loc["revision_id"]
                        or e.get("quote") != loc["quote"] or e.get("source_location") != loc["location"]):
                    raise sources.SourceConflict("导入引用与本地固定原件不一致")
    # Preserve provenance without representing imported assertions as locally
    # verified user actions. Original history remains available for audit.
    doc["imported_history"] = {"trust": "imported_unverified", "entries": deepcopy(payload.get("history", []))}
    return doc
