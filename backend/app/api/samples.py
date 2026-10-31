"""内置示例论文：一键加载，便于演示与上手；不污染全新安装。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import PDF_DIR
from ..db import get_db
from ..models import Paper
from ..pipeline.indexer import file_sha256
from ..schemas import PaperOut
from ..tasks import schedule_parse

router = APIRouter(prefix="/api/samples", tags=["samples"])


def samples_dir() -> Path:
    """打包后示例位于 _internal/app/samples；开发时位于 app/samples。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / "samples"
    return Path(__file__).resolve().parent.parent / "samples"


def _import_one(db: Session, src: Path) -> Paper | None:
    content_hash = file_sha256(src)
    exists = db.query(Paper).filter(Paper.content_hash == content_hash).first()
    if exists:
        return exists

    title = src.stem.replace("-", " ")
    paper = Paper(title=title, status="uploaded", content_hash=content_hash)
    db.add(paper)
    db.commit()

    pdf_path = PDF_DIR / f"{paper.id}.pdf"
    shutil.copyfile(src, pdf_path)
    paper.pdf_path = str(pdf_path)
    db.commit()

    schedule_parse(paper.id)
    db.refresh(paper)
    return paper


@router.get("")
def list_samples(db: Session = Depends(get_db)):
    hashes = {p.content_hash for p in db.query(Paper).all()}
    items = []
    for src in sorted(samples_dir().glob("*.pdf")):
        items.append({
            "name": src.name,
            "title": src.stem.replace("-", " "),
            "imported": file_sha256(src) in hashes,
        })
    return items


@router.post("/import", response_model=list[PaperOut])
def import_samples(db: Session = Depends(get_db)):
    result = []
    for src in sorted(samples_dir().glob("*.pdf")):
        paper = _import_one(db, src)
        if paper:
            result.append(paper)
    return result
