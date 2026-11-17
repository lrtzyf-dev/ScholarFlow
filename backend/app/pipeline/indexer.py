"""入库索引：PDF -> 解析 -> 切块 -> 落库 -> FTS -> 向量。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

import numpy as np
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from ..config import PARSED_DIR, PDF_DIR
from ..models import Chunk, Paper, Reference, TaskRecord
from ..parsing import parse_pdf
from ..parsing.metadata import extract_abstract, extract_authors
from .chunking import chunk_parsed
from .embeddings import APIEmbedder, embedding_connection, get_embedder
from .vectordb import NumpyVectorStore, current_embedding_fingerprint

# 每批编码的 chunk 数：控制 API 请求体大小与单批内存分配
EMBED_BATCH_SIZE = 64


class IndexRecoveryRequired(RuntimeError):
    """The recovery directory must survive a failed compensating file write."""

    def __init__(self, directory):
        self.directory = directory
        super().__init__("索引文件未能完整恢复，恢复副本已保留，请检查存储后处理")


class IndexVersionManaged(ValueError):
    """Legacy reparsing may not overwrite explicitly versioned generations."""


def assert_legacy_index_write_allowed(db, paper_id):
    from ..library_index_models import LibraryIndexState, LibraryIndexOperation
    if db.get(LibraryIndexState, paper_id) is not None or db.scalar(select(LibraryIndexOperation.id).where(
            LibraryIndexOperation.paper_id == paper_id, LibraryIndexOperation.status != "cancelled")):
        raise IndexVersionManaged("此文献已启用固定版本索引管理，请在文献库的固定版本中选择解析版本并重建索引；原引用与当前索引已保留")


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


def _merge_ocr_pages(base: dict, ocr_result: dict) -> dict:
    """把逐页 OCR 结果按页码合并回原解析结果，保留有文本层的页面。"""
    ocr_by_page = {p["page"]: p for p in ocr_result.get("pages", [])}
    merged_pages = [
        ocr_by_page[p["page"]] if p["page"] in ocr_by_page else p
        for p in base["pages"]
    ]
    out = dict(base)
    out["pages"] = merged_pages
    if ocr_result.get("ocr_engine"):
        out["ocr_engine"] = ocr_result["ocr_engine"]
    # 重新计算 OCR 后仍为空的页面（有效正文 < 20 字符）
    out["blank_page_indices"] = [
        i for i, p in enumerate(merged_pages)
        if len("".join(p.get("text", "").split())) < 20
    ]
    return out


def _finish_needs_ocr(
    db: Session, paper: Paper, task: TaskRecord, parsed_path: Path,
    parsed: dict, error: str,
) -> Paper:
    """统一的失败关闭收尾：标记待 OCR，记录原因，不建立任何检索索引。"""
    assert_legacy_index_write_allowed(db, paper.id)
    paper.status = "needs_ocr"
    paper.error = error
    paper.page_count = parsed["page_count"]
    paper.parsed_path = str(parsed_path)
    task.status = "done"
    task.progress = 1.0
    db.commit()
    return paper


def _prepare_embedding_vectors(chunks, *, authorization_check=None, on_batch=None, prepared_batches=None):
    """No caller write transaction may span a metered provider request."""
    from .. import config
    with config._settings_lock:
        if authorization_check is not None and authorization_check() is not True:
            raise ValueError("索引授权已改变，请核对后继续")
        fingerprint = current_embedding_fingerprint()
        embedder = get_embedder()
        if isinstance(embedder, APIEmbedder):
            embedder = APIEmbedder(connection=embedding_connection(),
                                  timeout=embedder._timeout, synthetic=embedder._synthetic,
                                  authorization_check=authorization_check)
    vectors = None
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        if authorization_check is not None and authorization_check() is not True:
            raise ValueError("索引授权已改变，请核对后继续")
        cached = (prepared_batches or {}).get(start)
        encoded = np.asarray(cached if cached is not None else embedder.encode([item["content"] for item in batch]), dtype=np.float32)
        if (encoded.ndim != 2 or encoded.shape[0] != len(batch) or encoded.shape[1] == 0
                or not np.isfinite(encoded).all()):
            raise ValueError("Embedding 返回的向量无效，原索引已保留")
        if vectors is None:
            vectors = np.empty((len(chunks), encoded.shape[1]), dtype=np.float32)
        elif encoded.shape[1] != vectors.shape[1]:
            raise ValueError("Embedding 分批维度不一致，原索引已保留")
        vectors[start:start + len(batch)] = encoded
        if cached is None and on_batch is not None:
            on_batch(start, encoded, fingerprint)
        with config._settings_lock:
            if current_embedding_fingerprint() != fingerprint:
                raise ValueError("向量模型配置已变化，请使用当前配置重新入库")
    return vectors, fingerprint


@contextmanager
def _publish_index_files(files, directory):
    """Stage all bytes before replacing live files; undo on publish/DB failure.

    The caller owns SQLite's writer reservation throughout publication. This
    restores normal exceptions, not a machine crash spanning DB and file commits.
    """
    backups = []
    for index, (_, destination) in enumerate(files):
        backup = directory / f"previous-{index}"
        existed = destination.exists()
        if existed:
            shutil.copy2(destination, backup)
        backups.append((destination, backup if existed else None))
    try:
        for staged, destination in files:
            if staged is None:
                destination.unlink(missing_ok=True)
            else:
                os.replace(staged, destination)
        yield
    except BaseException as original:
        failures = []
        for destination, backup in reversed(backups):
            try:
                if backup is None:
                    destination.unlink(missing_ok=True)
                else:
                    os.replace(backup, destination)
            except Exception as exc:
                failures.append({"destination": str(destination),
                                 "backup": backup.name if backup else None,
                                 "error_kind": type(exc).__name__})
        if failures:
            receipt = {"schema_version": 1, "status": "recovery_required",
                       "original_error_kind": type(original).__name__, "files": failures}
            try:
                from ..atomic_files import atomic_write_json
                atomic_write_json(directory / "recovery-required.json", receipt)
            except Exception:
                # A full or inaccessible disk may prevent even this small
                # receipt. The directory and old bytes must still survive.
                pass
            raise IndexRecoveryRequired(directory) from original
        raise


def ingest_pdf(db: Session, paper: Paper) -> Paper:
    paper_id = paper.id
    assert_legacy_index_write_allowed(db, paper_id)
    previous_status, previous_error = paper.status, paper.error
    paper.status = "parsing"
    db.commit()
    task = TaskRecord(paper_id=paper.id, type="parse", status="running")
    db.add(task)
    db.commit()
    task_id = task.id

    temporary, retain_recovery = None, False
    try:
        temporary = Path(mkdtemp(prefix=f".ingest-{paper_id}-", dir=PARSED_DIR))
        return _ingest_pdf_contents(db, paper, task, temporary)
    except Exception as exc:  # noqa: BLE001
        # Retain backups even if recording the failed state also cannot write.
        retain_recovery = isinstance(exc, IndexRecoveryRequired)
        # Discard uncommitted chunks/references/FTS before recording the failure.
        # In particular, a failed flush must never commit a half-built index.
        db.rollback()
        current_paper, current_task = db.get(Paper, paper_id), db.get(TaskRecord, task_id)
        message = "文献入库未完成；请检查解析、模型配置或存储后重试。"
        if retain_recovery:
            message = f"索引恢复未完成，需要恢复处理；恢复副本保留在 {exc.directory}"
        if current_paper is not None:
            if isinstance(exc, IndexVersionManaged):
                if current_paper.status == "parsing":
                    current_paper.status, current_paper.error = previous_status, previous_error
                message = str(exc)
            else:
                current_paper.status, current_paper.error = "failed", message
        if current_task is not None:
            current_task.status, current_task.error = "failed", message
        db.commit()
        raise
    finally:
        if temporary is not None and not retain_recovery:
            shutil.rmtree(temporary, ignore_errors=True)


def _ingest_pdf_contents(db, paper, task, temporary):
    from .. import config

    pdf_path = Path(paper.pdf_path)
    paper_id = paper.id
    parsed_path = PARSED_DIR / f"{paper.id}.json"
    staged_parsed = temporary / "parsed.json"
    parsed = parse_pdf(pdf_path, staged_parsed)

    # 空白页逐页 OCR 补全；无引擎或识别为空都显式失败关闭，绝不静默当作已解析
    blank_indices = parsed.get("blank_page_indices", [])
    if parsed.get("needs_ocr") and blank_indices:
        from ..parsing.ocr import (
            OcrUnavailable,
            has_usable_text,
            ocr_pdf,
        )
        try:
            # 仅对空白页 OCR，保留已有文本层页面，再合并回解析结果
            ocr_result = ocr_pdf(pdf_path, page_indices=blank_indices)
            parsed = _merge_ocr_pages(parsed, ocr_result)
            # 落盘结果同步替换为合并后的 OCR 结果（含 ocr_engine 标注）
            staged_parsed.write_text(
                json.dumps(parsed, ensure_ascii=False), encoding="utf-8"
            )
        except OcrUnavailable as exc:
            # 无引擎：整本无有效正文才标记待 OCR；已有部分文本页则继续解析
            if not has_usable_text(parsed):
                db.rollback()
                db.execute(text("BEGIN IMMEDIATE"))
                with _publish_index_files([(staged_parsed, parsed_path)], temporary):
                    return _finish_needs_ocr(db, paper, task, parsed_path, parsed, str(exc))
        # 引擎可用但 OCR 后整本仍无有效正文：识别失败，失败关闭
        if not has_usable_text(parsed):
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            with _publish_index_files([(staged_parsed, parsed_path)], temporary):
                return _finish_needs_ocr(
                    db, paper, task, parsed_path, parsed,
                    "OCR 未识别到有效正文，可能是纯图片或扫描质量过低，"
                    "请重新上传更清晰的文件",
                )

    chunks, references = chunk_parsed(parsed)
    # Release even the read transaction before APIEmbedder persists its ledger.
    db.rollback()
    vectors, fingerprint = _prepare_embedding_vectors(chunks)
    db.execute(text("BEGIN IMMEDIATE"))
    # No remote request occurs below. Configuration cannot change between
    # validating this vector identity and publishing its cache metadata.
    with config._settings_lock:
        if current_embedding_fingerprint() != fingerprint:
            raise ValueError("向量模型配置已变化，请重新入库")
        return _commit_pdf_index(db, paper, task, paper_id, pdf_path, parsed_path,
                                 staged_parsed, parsed, chunks, references, vectors, temporary)


def _commit_pdf_index(db, paper, task, paper_id, pdf_path, parsed_path, staged_parsed,
                      parsed, chunks, references, vectors, temporary):
    assert_legacy_index_write_allowed(db, paper_id)
    current = NumpyVectorStore()
    staged = NumpyVectorStore(temporary / "vectors.npy")
    keep = [index for index, pid in enumerate(current.paper_ids) if pid != paper_id]
    staged.ids = [current.ids[index] for index in keep]
    staged.paper_ids = [current.paper_ids[index] for index in keep]
    staged.matrix = current.matrix[keep] if keep else None
    staged.embedding_fingerprint = current.embedding_fingerprint if keep else None
    staged._reindex()
    if chunks:
        staged.begin_build(len(chunks), vectors.shape[1])
    db.execute(delete(Chunk).where(Chunk.paper_id == paper_id))
    db.execute(delete(Reference).where(Reference.paper_id == paper_id))
    db.execute(text("DELETE FROM chunks_fts WHERE paper_id = :pid"), {"pid": paper_id})

    # One DB transaction; batches only bound ORM construction and memory.
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch_chunks = chunks[start:start + EMBED_BATCH_SIZE]
        rows: list[Chunk] = []
        for ch in batch_chunks:
            row = Chunk(
                paper_id=paper_id,
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

        # FTS 索引（CJK 按字展开）
        for row in rows:
            db.execute(
                text("INSERT INTO chunks_fts(chunk_id, paper_id, content) VALUES (:cid, :pid, :content)"),
                {"cid": row.id, "pid": paper_id, "content": cjk_spaced(row.content)},
            )

        staged.fill([r.id for r in rows], [paper_id] * len(rows), vectors[start:start + len(rows)])
        for row in rows:
            db.expunge(row)

    staged.flush()

    for ref in references:
        db.add(Reference(paper_id=paper_id, **ref))

    # Explicitly adopted bibliographic metadata takes precedence over a PDF
    # title/author heuristic, including on later reparsing of the same original.
    from ..bibliography_models import PaperMetadataRevision
    curated_metadata = db.scalar(select(PaperMetadataRevision.id).where(
        PaperMetadataRevision.paper_id == paper_id).limit(1)) is not None
    guessed = _guess_title(parsed)
    if guessed and not curated_metadata:
        # 上传时标题暂用文件名；解析后若识别出文档标题则覆盖
        paper.title = guessed
    elif not paper.title:
        paper.title = pdf_path.stem
    # 启发式元数据：仅在识别成功时填充，缺失保留空值，不编造
    abstract = extract_abstract(parsed)
    if abstract and not curated_metadata:
        paper.abstract = abstract
    authors = extract_authors(parsed)
    if authors and not curated_metadata:
        paper.authors = authors
    paper.page_count = parsed["page_count"]
    paper.parsed_path = str(parsed_path)
    paper.status = "parsed"
    paper.error = ""
    task.status = "done"
    task.progress = 1.0
    db.flush()
    destination = current.path if current.path.suffix == ".npy" else Path(str(current.path) + ".npy")
    files = [(staged.path if staged.matrix is not None else None, destination),
             (staged.path.with_suffix(".json") if staged.matrix is not None else None, current.path.with_suffix(".json")),
             (staged_parsed, parsed_path)]
    if current._legacy_matrix != destination:
        files.append((None, current._legacy_matrix))
    with _publish_index_files(files, temporary):
        db.commit()
    return paper


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


def find_version_group(db: Session, version_group: str) -> Paper | None:
    """按归一化版本组（如基础 arXiv ID）查找同论文的其他版本；空组不参与。"""
    if not version_group:
        return None
    return db.query(Paper).filter(Paper.version_group == version_group).first()
