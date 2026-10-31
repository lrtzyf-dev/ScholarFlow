"""一键入库：从外部检索结果（arXiv / Semantic Scholar 开放 PDF）下载 PDF 并入库。

单进程模式下 schedule_parse 同步执行，调用返回时文献已解析可检索。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from sqlalchemy.orm import Session

from ..config import PDF_DIR
from ..models import Paper
from ..pipeline.indexer import file_sha256, find_duplicate
from ..tasks import schedule_parse

_UA = "Mozilla/5.0 (compatible; ScholarFlow/1.0; research)"


def import_paper_from_url(db: Session, pdf_url: str, title: str = "") -> dict:
    """下载开放 PDF → 建 Paper → 解析入库。返回 {paper_id,title,status}。"""
    if not pdf_url:
        raise ValueError("缺少 PDF 下载地址")
    paper = Paper(title=(title or "未命名论文")[:300], status="uploaded")
    db.add(paper)
    db.commit()

    pdf_path = PDF_DIR / f"{paper.id}.pdf"
    try:
        with httpx.Client(timeout=60, follow_redirects=True,
                          headers={"User-Agent": _UA}) as client:
            resp = client.get(pdf_url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if resp.content[:4] != b"%PDF" and "pdf" not in ctype:
                # 部分开放地址返回 HTML 落地页，无法直接解析
                raise ValueError("下载地址未返回有效 PDF（可能需要登录或已失效）")
            pdf_path.write_bytes(resp.content)
    except Exception as exc:
        paper.status = "failed"
        paper.error = f"下载失败：{type(exc).__name__}: {exc}"
        db.commit()
        raise

    paper.pdf_path = str(pdf_path)
    paper.content_hash = file_sha256(pdf_path)
    db.commit()

    # 内容去重：相同内容哈希已存在则不重复入库
    existing = find_duplicate(db, paper.content_hash)
    if existing is not None and existing.id != paper.id:
        db.delete(paper)
        db.commit()
        pdf_path.unlink(missing_ok=True)
        return {"paper_id": existing.id, "title": existing.title,
                "status": existing.status, "error": existing.error or "",
                "duplicate": True}

    schedule_parse(paper.id)  # 单进程同步解析
    db.refresh(paper)
    return {"paper_id": paper.id, "title": paper.title,
            "status": paper.status, "error": paper.error or "",
            "duplicate": False}
