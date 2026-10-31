"""论文上传、列表、原文与页面数据。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import PDF_DIR
from ..db import get_db
from ..models import Paper
from ..pipeline.indexer import file_sha256, find_duplicate
from ..schemas import PageOut, PaperOut
from ..tasks import schedule_parse

router = APIRouter(prefix="/api/papers", tags=["papers"])


@router.post("/upload", response_model=PaperOut)
def upload_paper(file: UploadFile, db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    paper = Paper(title=file.filename or "未命名论文", status="uploaded")
    db.add(paper)
    db.commit()

    pdf_path = PDF_DIR / f"{paper.id}.pdf"
    with pdf_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    paper.pdf_path = str(pdf_path)
    paper.content_hash = file_sha256(pdf_path)
    db.commit()

    # 内容去重：同一文件（内容哈希相同）已在库中，则丢弃临时记录，返回已有论文
    existing = find_duplicate(db, paper.content_hash)
    if existing is not None and existing.id != paper.id:
        db.delete(paper)
        db.commit()
        if pdf_path.exists():
            pdf_path.unlink()
        out = PaperOut.model_validate(existing)
        out.duplicate = True
        return out

    schedule_parse(paper.id)
    db.refresh(paper)
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
    return db.query(Paper).order_by(Paper.created_at.desc()).all()


@router.get("/{paper_id}", response_model=PaperOut)
def get_paper(paper_id: str, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
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
