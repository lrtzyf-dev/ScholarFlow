"""本地上传路径的语义版本去重服务。

两条检测路径，与 URL 入库共用底层纯函数：
1. 版本组：从文件名提取 arXiv ID 并归一化（剥离 v1/v7），同基础 ID 即同论文；
2. 文本相似度兜底：文件名无 arXiv ID 时，用正文 Jaccard 相似度判定，
   避免仅靠文件名或内容哈希无法识别的情况。

冲突时不自动替换、不删除记录，只返回结构化提示，由用户决定保留哪一版。
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Paper
from ..pipeline.indexer import find_version_group
from ..pipeline.versioning import (
    extract_arxiv_version,
    is_newer_version,
    is_same_work,
)


def load_paper_text(paper: Paper) -> str:
    """从落盘解析结果读取全文；解析文件缺失或损坏时返回空串。"""
    if not paper.parsed_path:
        return ""
    path = Path(paper.parsed_path)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return "\n".join(page.get("text", "") for page in data.get("pages", []))


def _build_conflict(incoming: Paper, existing: Paper) -> dict:
    """构造版本冲突响应字段；版本号缺失时用 None 表示。"""
    incoming_version = extract_arxiv_version(incoming.arxiv_id)
    existing_version = extract_arxiv_version(existing.arxiv_id)
    newer = is_newer_version(incoming_version, existing_version)
    return {
        "version_duplicate": True,
        "existing_id": existing.id,
        "incoming_version": incoming_version,
        "existing_version": existing_version,
        "newer": newer,
        "recommend": "incoming" if newer else "existing",
    }


def detect_version_conflict(
    db: Session, incoming: Paper, incoming_text: str
) -> dict | None:
    """检测同论文不同版本；命中返回冲突字段 dict，否则返回 None。

    优先版本组（可靠、不依赖文本），再用正文相似度兜底。
    """
    # 1) 归一化版本组（基于 arXiv ID）
    if incoming.version_group:
        twin = find_version_group(db, incoming.version_group)
        if twin is not None and twin.id != incoming.id:
            if not twin.version_group:
                twin.version_group = incoming.version_group
                db.commit()
            return _build_conflict(incoming, twin)

    # 2) 文本相似度兜底（文件名无 arXiv ID，或版本组未建立时）
    if not incoming_text:
        return None
    for existing in (
        db.query(Paper)
        .filter(Paper.status == "parsed", Paper.id != incoming.id)
        .all()
    ):
        existing_text = load_paper_text(existing)
        if existing_text and is_same_work(incoming_text, existing_text):
            # 文本命中后补齐版本组关联，便于后续再次识别
            if incoming.version_group and not existing.version_group:
                existing.version_group = incoming.version_group
                db.commit()
            return _build_conflict(incoming, existing)
    return None
