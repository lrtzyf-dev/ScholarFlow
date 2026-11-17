"""单篇论文重新索引维护脚本（离线可用）。

用途：解析器修复后，刷新已入库论文的旧版（乱序/截断）解析结果——
重新解析现有 PDF -> 重新切块 -> 替换该论文的 chunks / FTS / 向量 / 参考文献。
不删除论文、不改动会话与历史问答记录。

用法：
    python scripts/reindex_paper.py                 # 重索引当前数据目录下全部论文
    python scripts/reindex_paper.py <paper_id>      # 仅重索引指定论文
可通过环境变量 data_dir 指向打包版数据目录（%APPDATA%\\ScholarFlow）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许直接以脚本方式运行（把 backend 加入 import 路径）
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import numpy as np
from sqlalchemy import delete, text

from app.config import PARSED_DIR, settings
from app.db import SessionLocal
from app.models import Chunk, Paper, Reference
from app.parsing.pdfplumber_parser import parse_pdf
from app.pipeline.chunking import chunk_parsed
from app.pipeline.embeddings import get_embedder
from app.pipeline.indexer import cjk_spaced
from app.pipeline.vectordb import NumpyVectorStore

EMBED_BATCH_SIZE = 64


def reindex(db, paper: Paper) -> dict:
    pdf_path = Path(paper.pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 不存在：{pdf_path}")

    parsed_path = PARSED_DIR / f"{paper.id}.json"
    parsed = parse_pdf(pdf_path, parsed_path)
    if parsed.get("needs_ocr"):
        raise RuntimeError(f"论文 {paper.id} 判定为扫描件，本脚本不处理 OCR，请走应用内 OCR 流程")

    chunks, references = chunk_parsed(parsed)

    # 清除该论文旧数据
    db.execute(delete(Chunk).where(Chunk.paper_id == paper.id))
    db.execute(delete(Reference).where(Reference.paper_id == paper.id))
    db.execute(text("DELETE FROM chunks_fts WHERE paper_id = :pid"), {"pid": paper.id})
    store = NumpyVectorStore()
    store.remove_paper(paper.id)

    embedder = get_embedder()
    # begin_build 只接收"新增"行数，内部会自动容纳既有行
    store.begin_build(len(chunks), settings.vector_dim)
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        rows = []
        for ch in batch:
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
            rows.append(row)
        db.add_all(rows)
        db.flush()
        for row in rows:
            db.execute(
                text("INSERT INTO chunks_fts(chunk_id, paper_id, content) VALUES (:cid, :pid, :content)"),
                {"cid": row.id, "pid": paper.id, "content": cjk_spaced(row.content)},
            )
        vecs = np.asarray(embedder.encode([r.content for r in rows]), dtype=np.float32)
        store.fill([r.id for r in rows], [paper.id] * len(rows), vecs)
        db.commit()
    store.flush()

    for ref in references:
        db.add(Reference(paper_id=paper.id, **ref))

    paper.page_count = parsed["page_count"]
    paper.parsed_path = str(parsed_path)
    paper.status = "parsed"
    paper.error = ""
    db.commit()
    return {"paper_id": paper.id, "chunks": len(chunks), "references": len(references),
            "pages": parsed["page_count"]}


def main(argv: list[str]) -> None:
    print("data_dir =", settings.data_dir, " embedding_mode =", settings.embedding_mode)
    db = SessionLocal()
    try:
        query = db.query(Paper)
        if argv:
            query = query.filter(Paper.id == argv[0])
        papers = query.all()
        if not papers:
            print("未找到匹配论文")
            return
        for paper in papers:
            result = reindex(db, paper)
            print("已重索引:", json.dumps(result, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1:])
