"""Deterministic fixed raw copies for a reviewed old20 migration.

No app config, user credential, ORM engine, old parser or external file access.
New PDF text is a new local extraction, never certification of old QA/chunks.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3

from .release_files import contained, digest, no_link
from .release_info import canonical_json, ReleaseError


PROTOCOL = "legacy_fixed_raw_and_new_pdf_extraction_v1"
MAX_RAW = 100 * 1024 * 1024
MAX_TEXT = 16 * 1024 * 1024
MAX_DOCUMENT = 64 * 1024 * 1024
PARSER = "pdfplumber-source/1"


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _id(seed, kind, paper):
    return digest([PROTOCOL, seed, kind, paper])[:32]


def _extract(raw):
    """Same explicit PDF coordinate/text contract; no application imports."""
    import pdfplumber
    blocks, pages, texts, total = [], [], [], 0
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        if len(pdf.pages) > 1000:
            raise ValueError("bounded PDF pages")
        for i,page in enumerate(pdf.pages):
            content = page.extract_text() or ""
            total += len(content.encode("utf8")) + 1
            if total > MAX_TEXT:
                raise ValueError("bounded PDF text")
            geometry = {"page_index": i, "page_number": i+1, "page_index_base": 0,
                "bbox": list(page.bbox), "page_width": float(page.width), "page_height": float(page.height),
                "rotation": page.rotation, "geometry_verified": True, "coordinate_space": "pdf_points_top_left"}
            pages.append(geometry);blocks.append({"kind": "pdf_page", **geometry, "text": content});texts.append(content)
    text = "\n\f\n".join(texts)
    result = {"format": "pdf", "parser": PARSER, "text": text, "blocks": blocks, "sheets": [], "pages": pages,
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "trust": "untrusted_source_data", "authorizes_actions": False, "extraction_accuracy_verified": False,
        "model_understanding_verified": False, "extraction_scope": "pdf_text_layer",
        "omitted_content": ["image_understanding", "scan_ocr"]}
    if len(canonical_json(result)) > MAX_DOCUMENT:
        raise ValueError("bounded PDF document")
    return result


def _time(manifest):
    # Stable archive capture time is hash-bound by preparation. It is not a
    # user row, journal self-report or fabricated timestamp of old analysis.
    try:
        parsed = datetime.fromisoformat(manifest["created_at"])
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    except (KeyError,TypeError,ValueError):
        raise ReleaseError("来源归档缺少明确采集时间，未创建固定来源") from None


def _managed_path(value, *, output, stage, known, root):
    if not isinstance(value, str) or not value:
        return None
    try:
        final = no_link(Path(value).resolve())
        relative = final.relative_to(output.resolve()).as_posix()
        if not relative.startswith(root + "/") or relative not in known:
            return None
        path = contained(stage, relative)
        return (relative,path) if path.is_file() else None
    except (OSError,ValueError,ReleaseError):
        return None


def build(database, *, stage, output, manifest, transaction_id, source_schema):
    stage,output = no_link(stage),no_link(output)
    known = {item["path"]:item for item in manifest["files"]}
    now = _time(manifest)
    summaries, verified_hashes = [], {}
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA trusted_schema=OFF");db.execute("BEGIN IMMEDIATE")
        papers = db.execute("SELECT id,title,pdf_path,parsed_path,content_hash,status,error FROM papers ORDER BY id").fetchall()
        for paper,title,pdf_path,parsed_path,content_hash,status,error in papers:
            summary = {"paper_id": paper, "fixed_original": "unavailable", "structured_extraction": "not_created",
                       "historical_chunk_alignment": "not_verified", "old_status": status}
            current = _managed_path(pdf_path,output=output,stage=stage,known=known,root="pdf")
            parsed = _managed_path(parsed_path,output=output,stage=stage,known=known,root="parsed")
            summary["historical_parsed_asset"] = ("retained_archive_asset_not_active" if parsed is not None
                else "original_row_only_not_read" if parsed_path else "none")
            # A hashed archive authenticates an old derived file's capture,
            # not its association with this raw PDF. The legacy page endpoint
            # follows this field without checking status or raw identity.
            # Keep the file and original DB in history, and expose only the
            # newly computed SourceExtraction as current structured reading.
            db.execute("UPDATE papers SET parsed_path='' WHERE id=?",(paper,))
            reason = None
            if current is None:
                reason = "missing_managed_original"
            elif not isinstance(content_hash,str) or not re.fullmatch(r"[a-f0-9]{64}",content_hash):
                reason = "historical_hash_unknown"
            elif not isinstance(paper,str) or not re.fullmatch(r"[\w:-]{1,96}",paper):
                reason = "historical_identity_unusable_for_fixed_source"
            else:
                relative,path = current
                if not 1 <= known[relative]["size"] <= MAX_RAW:
                    reason = "original_exceeds_fixed_source_limit"
                else:
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_RAW+1)
                    if len(raw) != known[relative]["size"] or hashlib.sha256(raw).hexdigest() != known[relative]["sha256"]:
                        raise ReleaseError("暂存原件在固定来源采集时变化，保留事务，不发布")
                    if hashlib.sha256(raw).hexdigest() != content_hash:
                        reason = "historical_hash_mismatch"
            if reason:
                summary["reason"] = reason
                db.execute("UPDATE papers SET pdf_path='',status='failed',error=? WHERE id=?",
                    ("旧数据导入：原件未能与历史哈希对应，历史答案与索引已保留；请重新选择核对原件（"+reason+"）",paper))
                summaries.append(summary)
                continue
            sid,snap,rid = (_id(transaction_id,kind,paper) for kind in ("source","snapshot","revision"))
            title = title if isinstance(title,str) and title.strip() else Path(relative).name
            origin = "legacy-import:" + transaction_id + ":" + paper
            authorization = "legacy-import:" + transaction_id
            snapshot_fp = hashlib.sha256(json.dumps([content_hash,None,"application/pdf"],ensure_ascii=False).encode()).hexdigest()
            revision_fp = hashlib.sha256(_json([sid,snap,1,title,snapshot_fp,authorization,origin,"none",None]).encode()).hexdigest()
            db.execute("INSERT INTO sources(id,scope_type,scope_id,kind,title,version,current_revision_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (sid,"library",paper,"library",title,1,rid,now,now))
            db.execute("INSERT INTO source_snapshots(id,scope_type,scope_id,fingerprint,raw_sha256,text_sha256,media_type,raw_bytes,text,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (snap,"library",paper,snapshot_fp,content_hash,None,"application/pdf",raw,None,now))
            db.execute("INSERT INTO source_revisions(id,source_id,snapshot_id,version,title_at_capture,snapshot_fingerprint,fingerprint,authorization_id,origin_ref,text_origin,parser,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid,sid,snap,1,title,snapshot_fp,revision_fp,authorization,origin,"none",None,now))
            db.execute("INSERT INTO source_library_bindings(paper_id,revision_id,created_at) VALUES (?,?,?)",(paper,rid,now))
            db.execute("INSERT INTO source_library_imports(revision_id,raw_sha256,paper_id,request_id,chunk_locators,created_at) VALUES (?,?,?,?,?,?)",
                (rid,content_hash,paper,"legacy-fixed-"+transaction_id,"{}",now))
            verified_hashes[paper] = content_hash
            summary.update(fixed_original="verified_hash_copy",revision_id=rid,raw_sha256=content_hash,
                           captured_time_basis="verified_archive_created_at")
            try:
                document = _extract(raw)
            except Exception:
                # No parser diagnostics or original text enter the report.
                summary["structured_extraction"] = "local_parse_unavailable_raw_retained"
            else:
                fp = hashlib.sha256(json.dumps([content_hash,document["text_sha256"],PARSER,document],
                    ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
                db.execute("INSERT INTO source_extractions(revision_id,raw_sha256,text_sha256,parser,document,fingerprint,created_at) VALUES (?,?,?,?,?,?,?)",
                    (rid,content_hash,document["text_sha256"],PARSER,_json(document),fp,now))
                summary.update(structured_extraction="new_local_pdf_text_layer",extraction_fingerprint=fp,
                    page_count=len(document["pages"]),extraction_accuracy_verified=False)
            if status == "parsing":
                db.execute("UPDATE papers SET status='failed',error=? WHERE id=?",
                    ("旧数据导入：旧解析任务已中断；已保存原件及新的独立阅读提取，请明确重建索引",paper))
            summaries.append(summary)
        columns = ("id","paper_id","chunk_index","parent_index","page","bbox","section","kind","content","char_count")
        for row in db.execute("SELECT "+','.join('"'+c+'"' for c in columns)+" FROM chunks ORDER BY id"):
            document = dict(zip(columns,row))
            if document["bbox"] is not None:
                try:document["bbox"] = json.loads(document["bbox"])
                except (TypeError,ValueError):raise ReleaseError("旧chunk坐标不是有效历史JSON，未发布") from None
            document.update(archive_scope="legacy_migrated_chunk",authorizes_actions=False,
                legacy_source_schema_sha256=source_schema,legacy_raw_sha256=verified_hashes.get(document["paper_id"]),
                original_alignment_verified=False)
            db.execute("INSERT INTO library_chunk_archives(chunk_id,paper_id,document,fingerprint,vector_data,vector_fingerprint,created_at) VALUES (?,?,?,?,?,?,?)",
                (document["id"],document["paper_id"],_json(document),digest(document),None,None,now))
        db.commit()
    return {"protocol":PROTOCOL,"papers":summaries,"legacy_evidence_upgraded":False,
            "old_chunk_alignment_verified":False,"network_calls":0,"new_embedding_calls":0}
