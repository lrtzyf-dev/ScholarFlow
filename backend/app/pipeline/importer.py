"""一键入库：从外部检索结果（arXiv / Semantic Scholar 开放 PDF）下载 PDF 并入库。

单进程模式下 schedule_parse 同步执行，调用返回时文献已解析可检索。
去重分两层：先精确内容哈希，再按归一化版本组识别同一论文的不同版本（v1/v7）。
"""
from __future__ import annotations

from pathlib import Path

import httpx

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from ..config import PDF_DIR
from ..models import Paper
from ..netguard import assert_public_url, request_guard_hook
from ..pipeline.indexer import file_sha256, find_duplicate, find_version_group
from ..pipeline.versioning import (
    extract_arxiv_id,
    extract_arxiv_version,
    is_newer_version,
    normalize_arxiv_id,
)
from ..tasks import schedule_parse

_UA = "Mozilla/5.0 (compatible; ScholarFlow/1.0; research)"


def import_paper_from_url(db: Session, pdf_url: str, title: str = "") -> dict:
    """下载开放 PDF → 建 Paper → 解析入库。返回 {paper_id,title,status}。"""
    if not pdf_url:
        raise ValueError("缺少 PDF 下载地址")
    # 发起任何请求 / 建库前先做 SSRF 校验（重定向逐跳由 event hook 兜底）
    assert_public_url(pdf_url)

    # 从来源地址提取 arXiv 标识：完整 ID、归一化版本组、版本号
    full_arxiv = extract_arxiv_id(pdf_url)
    version_group = normalize_arxiv_id(pdf_url)
    incoming_version = extract_arxiv_version(pdf_url)

    paper = Paper(
        title=(title or "未命名论文")[:300],
        arxiv_id=full_arxiv,
        version_group=version_group,
        status="uploaded",
    )
    db.add(paper)
    try:
        db.commit()
        return _complete_import(db, paper, pdf_url, version_group, incoming_version)
    except Exception as exc:
        # A record, downloaded file, parse work or index may already exist.
        # Preserve the identity for inspection rather than creating another
        # record when the model changes only its tool-call ID.
        exc.tool_effect_possible = True
        identity = inspect(paper).identity
        exc.import_paper_id = identity[0] if identity else paper.__dict__.get("id")
        raise


def _complete_import(db, paper, pdf_url, version_group, incoming_version):
    pdf_path = PDF_DIR / f"{paper.id}.pdf"
    try:
        # event hook 对每次实际请求（含重定向逐跳）做 SSRF 校验
        with httpx.Client(timeout=60, follow_redirects=True,
                          event_hooks={"request": [request_guard_hook]},
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

    # 第一层：精确内容哈希去重（同一文件）
    existing = find_duplicate(db, paper.content_hash)
    if existing is not None and existing.id != paper.id:
        db.delete(paper)
        db.commit()
        pdf_path.unlink(missing_ok=True)
        return {"paper_id": existing.id, "title": existing.title,
                "status": existing.status, "error": existing.error or "",
                "duplicate": True}

    # 第二层：语义版本去重（同一论文不同版本，如 arXiv v1/v7）
    twin = find_version_group(db, version_group)
    if twin is not None and twin.id != paper.id:
        # 旧记录可能尚未写入版本组，补齐关联
        if not twin.version_group:
            twin.version_group = version_group
            db.commit()
        existing_version = extract_arxiv_version(twin.arxiv_id)
        newer = is_newer_version(incoming_version, existing_version)
        # 保留新版记录与 PDF，但不自动替换，等待用户决定采用哪一版
        return {
            "paper_id": paper.id,
            "existing_id": twin.id,
            "title": paper.title,
            "status": paper.status,
            "error": paper.error or "",
            "duplicate": False,
            "version_duplicate": True,
            "incoming_version": incoming_version,
            "existing_version": existing_version,
            "newer": newer,
            "recommend": "incoming" if newer else "existing",
        }

    schedule_parse(paper.id)  # 单进程同步解析
    db.refresh(paper)
    return {"paper_id": paper.id, "title": paper.title,
            "status": paper.status, "error": paper.error or "",
            "duplicate": False}
