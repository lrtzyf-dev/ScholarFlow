"""入库索引：PDF -> 解析 -> 切块 -> 落库 -> FTS -> 向量。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from ..config import PARSED_DIR, PDF_DIR
from ..models import Chunk, Paper, Reference, TaskRecord
from ..parsing import parse_pdf
from .chunking import chunk_parsed
from .embeddings import get_embedder
from .vectordb import NumpyVectorStore


def cjk_spaced(text: str) -> str:
    """中文字符之间插空格，让 FTS5 unicode61 按字分词。"""
    out = []
    for i, ch in enumerate(text):
        if "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
            if i + 1 < len(text) and "\u4e00" <= text[i + 1] <= "\u9fff":
                out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _guess_title(parsed: dict) -> str:
    # 取首页前几个块中的首行；优先首行中最短的“标题样”行（<=120 字）
    candidates: list[str] = []
    for page in parsed["pages"][:1]:
        for block in page["blocks"][:6]:
            for line in block["text"].split("\n"):
                t = line.strip()
                if 8 <= len(t) <= 200:
                    candidates.append(t)
    if not candidates:
        return ""
    # 标题样：较短，且不含陈述句句号（中文“。”或行尾英文“.”），
    # 否则该行其实是正文句，不能误当作文档标题；全部不像标题则返回空串，保留文件名
    title_like = [
        t for t in candidates
        if len(t) <= 120 and "。" not in t and not t.endswith(".")
    ]
    return title_like[0] if title_like else ""


def ingest_pdf(db: Session, paper: Paper) -> Paper:
    paper.status = "parsing"
    db.commit()
    task = TaskRecord(paper_id=paper.id, type="parse", status="running")
    db.add(task)
    db.commit()

    try:
        pdf_path = Path(paper.pdf_path)
        parsed_path = PARSED_DIR / f"{paper.id}.json"
        parsed = parse_pdf(pdf_path, parsed_path)

        chunks, references = chunk_parsed(parsed)

        # 清除旧数据（重解析）
        db.execute(delete(Chunk).where(Chunk.paper_id == paper.id))
        db.execute(delete(Reference).where(Reference.paper_id == paper.id))
        db.execute(text("DELETE FROM chunks_fts WHERE paper_id = :pid"), {"pid": paper.id})
        NumpyVectorStore().remove_paper(paper.id)

        chunk_rows: list[Chunk] = []
        for ch in chunks:
            row = Chunk(
                paper_id=paper.id,
                chunk_index=ch["chunk_index"],
                parent_index=ch["parent_index"],
                page=ch["page"],
                bbox=ch["bbox"],
                section=ch["section"],
                kind=ch["kind"],
                content=ch["content"],
                char_count=len(ch["content"]),
            )
            chunk_rows.append(row)
        db.add_all(chunk_rows)
        db.flush()

        for ref in references:
            db.add(Reference(paper_id=paper.id, **ref))

        # FTS 索引（CJK 按字展开）
        for row in chunk_rows:
            db.execute(
                text("INSERT INTO chunks_fts(chunk_id, paper_id, content) VALUES (:cid, :pid, :content)"),
                {"cid": row.id, "pid": paper.id, "content": cjk_spaced(row.content)},
            )

        # 向量索引
        embedder = get_embedder()
        vecs = embedder.encode([r.content for r in chunk_rows])
        NumpyVectorStore().add(
            [r.id for r in chunk_rows],
            [paper.id] * len(chunk_rows),
            np.asarray(vecs, dtype=np.float32),
        )

        guessed = _guess_title(parsed)
        if guessed:
            # 上传时标题暂用文件名；解析后若识别出文档标题则覆盖
            paper.title = guessed
        elif not paper.title:
            paper.title = pdf_path.stem
        paper.page_count = parsed["page_count"]
        paper.parsed_path = str(parsed_path)
        paper.status = "parsed" if not parsed["needs_ocr"] else "parsed"
        paper.error = "needs_ocr" if parsed["needs_ocr"] else ""
        task.status = "done"
        task.progress = 1.0
        db.commit()
        return paper
    except Exception as exc:  # noqa: BLE001
        paper.status = "failed"
        paper.error = str(exc)
        task.status = "failed"
        task.error = str(exc)
        db.commit()
        raise


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find_duplicate(db: Session, content_hash: str) -> Paper | None:
    """按内容哈希查找已存在的论文；空哈希不参与查重。"""
    if not content_hash:
        return None
    return db.query(Paper).filter(Paper.content_hash == content_hash).first()
