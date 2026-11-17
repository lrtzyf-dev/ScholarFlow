"""论文上传、列表、原文与页面数据。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, Query
from sqlalchemy import select, func, case, or_
from typing import Literal
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..algorithms.citation_quality import analyze_paper_references
from ..algorithms.future_work import build_future_work, year_of
from ..config import PDF_DIR
from ..db import get_db
from ..models import Chunk, Paper, Reference
from ..parsing import InvalidPdfError
from ..parsing.citation_marks import citation_map
from ..pipeline.dedup_service import detect_version_conflict, load_paper_text
from ..pipeline.indexer import file_sha256, find_duplicate
from ..pipeline.versioning import extract_arxiv_id, normalize_arxiv_id
from ..schemas import PageOut, PaperOut
from ..tasks import schedule_parse


class CompareRequest(BaseModel):
    paper_ids: list[str]
    dimensions: list[str] | None = None
    conversation_id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")

router = APIRouter(prefix="/api/papers", tags=["papers"])


@router.post("/upload", response_model=PaperOut)
def upload_paper(file: UploadFile, db: Session = Depends(get_db)):
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    paper = Paper(title=filename, status="uploaded")
    # 从文件名提取 arXiv 标识与归一化版本组（如 1706.03762v7.pdf）
    paper.arxiv_id = extract_arxiv_id(filename)
    paper.version_group = normalize_arxiv_id(filename)
    db.add(paper)
    db.commit()

    pdf_path = PDF_DIR / f"{paper.id}.pdf"
    with pdf_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    paper.pdf_path = str(pdf_path)
    paper.content_hash = file_sha256(pdf_path)
    db.commit()

    # 第一层：内容哈希精确去重（同一文件）
    existing = find_duplicate(db, paper.content_hash)
    if existing is not None and existing.id != paper.id:
        db.delete(paper)
        db.commit()
        if pdf_path.exists():
            pdf_path.unlink()
        out = PaperOut.model_validate(existing)
        out.duplicate = True
        return out

    # 第二层：版本组（基于 arXiv ID 归一化），解析前即可识别，无需等待解析
    if paper.version_group:
        conflict = detect_version_conflict(db, paper, "")
        if conflict:
            out = PaperOut.model_validate(paper)
            for key, value in conflict.items():
                setattr(out, key, value)
            return out

    # 解析入库；损坏 PDF 属于客户端错误，返回 400 而非 500
    try:
        schedule_parse(paper.id)
    except InvalidPdfError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.refresh(paper)

    # 第三层：正文相似度兜底（文件名无 arXiv ID 时也能识别同论文不同版本）
    incoming_text = load_paper_text(paper)
    conflict = detect_version_conflict(db, paper, incoming_text)
    if conflict:
        out = PaperOut.model_validate(paper)
        for key, value in conflict.items():
            setattr(out, key, value)
        return out

    return paper


@router.post("/import-from-url")
def import_from_url(payload: dict, db: Session = Depends(get_db)):
    from ..pipeline.importer import import_paper_from_url

    pdf_url = (payload.get("pdf_url") or "").strip()
    title = (payload.get("title") or "").strip()
    try:
        return import_paper_from_url(db, pdf_url, title)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"一键入库失败：{type(exc).__name__}: {exc}")


@router.get("", response_model=list[PaperOut])
def list_papers(db: Session = Depends(get_db)):
    from ..source_library import library_metadata_many
    papers=db.query(Paper).order_by(Paper.created_at.desc(),Paper.id.desc()).all()
    metadata=library_metadata_many(db,[p.id for p in papers])
    return [PaperOut.model_validate(p).model_copy(update=metadata.get(p.id,{})) for p in papers]


@router.get("/page")
def paper_page(q:str=Query(default='',max_length=200),status:str=Query(default='all',max_length=32),
               sort:Literal['recent','pages','title']='recent',offset:int=Query(default=0,ge=0,le=1000000),
               limit:int=Query(default=40,ge=1,le=100),db:Session=Depends(get_db)):
    """Bounded library projection; global statistics do not load graph objects."""
    from ..source_library import library_metadata_many
    conditions=[]
    if status!='all': conditions.append(Paper.status==status)
    if q.strip():
        phrase=q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        conditions.append(or_(*(field.ilike('%'+phrase+'%',escape='\\') for field in (Paper.title,Paper.authors,Paper.abstract))))
    ordering={'recent':[Paper.created_at.desc(),Paper.id.desc()],
        'pages':[Paper.page_count.desc(),Paper.id.desc()],'title':[func.lower(Paper.title),Paper.id]}[sort]
    total=db.scalar(select(func.count()).select_from(Paper).where(*conditions)) or 0
    rows=db.scalars(select(Paper).where(*conditions).order_by(*ordering).offset(offset).limit(limit)).all()
    metadata=library_metadata_many(db,[p.id for p in rows])
    stats=db.execute(select(func.count(),func.coalesce(func.sum(Paper.page_count),0),
        func.coalesce(func.sum(case((Paper.status=='parsed',1),else_=0)),0)).select_from(Paper)).one()
    return {'items':[PaperOut.model_validate(p).model_copy(update=metadata.get(p.id,{})) for p in rows],
        'total':total,'offset':offset,'limit':limit,'next_offset':offset+len(rows) if offset+len(rows)<total else None,
        'statistics':{'papers':stats[0],'pages':stats[1],'parsed':stats[2],
            'internal_references':db.scalar(select(func.count()).select_from(Reference).where(Reference.linked_paper_id.is_not(None))) or 0}}


@router.get("/future-work/analysis")
def future_work_api(db: Session = Depends(get_db)):
    """跨库提取未来工作并做时间线判断，区分开放与疑似已处理方向。"""
    papers = db.query(Paper).filter(Paper.status == "parsed").all()
    paper_data = []
    for paper in papers:
        chunks = (
            db.query(Chunk)
            .filter(Chunk.paper_id == paper.id)
            .order_by(Chunk.chunk_index)
            .all()
        )
        paper_data.append({
            "id": paper.id,
            "title": paper.title or "未命名论文",
            "year": year_of(paper.published_at),
            "chunks": [
                {"page": c.page, "bbox": c.bbox, "content": c.content} for c in chunks
            ],
        })
    return build_future_work(paper_data)


@router.get("/{paper_id}", response_model=PaperOut)
def get_paper(paper_id: str, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    from ..source_library import library_metadata
    return PaperOut.model_validate(paper).model_copy(update=library_metadata(db, paper.id))


@router.post("/{paper_id}/reparse", response_model=PaperOut)
def reparse_paper(paper_id: str, db: Session = Depends(get_db)):
    """重新解析现有 PDF（含重试 OCR），返回最新状态，供待 OCR/失败论文恢复。"""
    paper = db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    from ..pipeline.indexer import assert_legacy_index_write_allowed, IndexVersionManaged
    try:
        assert_legacy_index_write_allowed(db, paper.id)
    except IndexVersionManaged as exc:
        raise HTTPException(409, str(exc)) from None
    if not paper.pdf_path or not Path(paper.pdf_path).exists():
        raise HTTPException(409, "原文 PDF 缺失，无法重新解析")
    try:
        schedule_parse(paper.id)
    except InvalidPdfError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.refresh(paper)
    return paper


@router.get("/{paper_id}/file")
def paper_file(paper_id: str, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if not paper or not paper.pdf_path or not Path(paper.pdf_path).exists():
        raise HTTPException(404, "原文不存在")
    return FileResponse(paper.pdf_path, media_type="application/pdf", filename=f"{paper.title or paper_id}.pdf")


@router.get("/{paper_id}/pages/{page}", response_model=PageOut)
def get_page(paper_id: str, page: int, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    if not paper.parsed_path or not Path(paper.parsed_path).exists():
        raise HTTPException(409, "论文尚未解析完成")
    parsed = json.loads(Path(paper.parsed_path).read_text(encoding="utf-8"))
    if page < 1 or page > parsed["page_count"]:
        raise HTTPException(400, "页码超出范围")
    data = parsed["pages"][page - 1]
    return {"page": page, "width": data["width"], "height": data["height"], "text": data["text"], "blocks": data["blocks"]}


@router.get("/{paper_id}/citations")
def get_citations(paper_id: str, db: Session = Depends(get_db)):
    """返回参考文献（含位置）与正文引用标记位置映射，供阅读器双向跳转。"""
    paper = db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    refs = (
        db.query(Reference)
        .filter(Reference.paper_id == paper_id)
        .order_by(Reference.ref_index)
        .all()
    )
    max_ref = max((r.ref_index for r in refs), default=0)
    chunks = (
        db.query(Chunk)
        .filter(Chunk.paper_id == paper_id)
        .order_by(Chunk.chunk_index)
        .all()
    )
    chunk_dicts = [
        {"page": c.page, "bbox": c.bbox, "content": c.content} for c in chunks
    ]
    inline = citation_map(chunk_dicts, max_ref)
    return {
        "references": [
            {
                "ref_index": r.ref_index,
                "raw_text": r.raw_text,
                "page": r.page,
                "bbox": r.bbox,
            }
            for r in refs
        ],
        # JSON 对象键为字符串，前端按字符串编号访问
        "inline_citations": {str(k): v for k, v in inline.items()},
    }


@router.get("/{paper_id}/citation-quality")
def citation_quality_api(paper_id: str, label: str | None = None,
                         db: Session = Depends(get_db)):
    """分析参考文献含金量，可按 label 筛选实质/应付引用。"""
    paper = db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")

    refs = (
        db.query(Reference)
        .filter(Reference.paper_id == paper_id)
        .order_by(Reference.ref_index)
        .all()
    )
    chunks = (
        db.query(Chunk.content)
        .filter(Chunk.paper_id == paper_id)
        .order_by(Chunk.chunk_index)
        .all()
    )
    full_text = "\n".join(row[0] for row in chunks)
    results = analyze_paper_references(full_text, len(refs))

    raw_by_index = {r.ref_index: r.raw_text for r in refs}
    for item in results:
        item["raw_text"] = raw_by_index.get(item["ref_index"], "")

    if label:
        results = [item for item in results if item["label"] == label]

    summary: dict[str, int] = {}
    for item in analyze_paper_references(full_text, len(refs)):
        summary[item["label"]] = summary.get(item["label"], 0) + 1

    return {"paper_id": paper_id, "results": results, "summary": summary}


@router.post("/compare")
def compare_papers_api(req: CompareRequest, db: Session = Depends(get_db)):
    """Explain migration to old clients without starting uncheckpointed I/O."""
    if req.conversation_id:
        from ..api.sources import _source_errors
        from ..source_runtime import conversation_scope
        with _source_errors(db):
            conversation_scope(db, req.conversation_id)
    raise HTTPException(409, {"code": "migration_required", "not_executed": True,
        "new_route": "/api/research/matrix-runs",
        "message": "跨文献对比已迁移为固定来源的可恢复矩阵任务，请先创建矩阵并保留原选择；本次未检索或调用模型。"})
