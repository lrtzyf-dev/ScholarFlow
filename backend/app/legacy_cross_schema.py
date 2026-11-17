"""Latest Git old20 -> fixed current schema, on a private extracted copy only.

No application config/model/engine import, old Python or input-provided SQL.
Every DDL statement and source/target fingerprint is a source-reviewed literal.
"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3

from .legacy_schema_contracts import (OLD20_COLUMNS, OLD20_FTS_OBJECTS, OLD20_NO_FTS_OBJECTS,
    OLD20_FTS_SHA256, OLD20_NO_FTS_SHA256, PRE_EXPORT_OBJECTS, PRE_EXPORT_SHA256,
    ARTIFACT_EXPORT_OBJECTS, TARGET_OBJECTS, TARGET_SHA256)
from .release_files import contained, digest, file_record, no_link
from .release_info import ReleaseError
from .release_schema import schema_snapshot
from .legacy_row_validation import validate_row


FTS_ID = "git-old20-fts-to-current-dormant-20261116-v2"
NO_FTS_ID = "git-old20-no-fts-to-current-dormant-20261116-v2"
PRE_EXPORT_ID = "pre-export-current-to-fixed-exports-dormant-20261116-v1"
SOURCES = {FTS_ID: OLD20_FTS_SHA256, NO_FTS_ID: OLD20_NO_FTS_SHA256}
DEFAULTS = {"foreground_runs": {"agent_checkpoint": None, "runtime_config": None, "display_snapshot": None},
            "qa_records": {"runtime_config": None, "provider_context": "{}"}, "tasks": {"runtime_config": None}}


def _body(objects):
    return {"format": "scholarflow-sqlite-schema", "version": 1,
        "objects": [{"type": t, "name": n, "table": table, "sql": sql} for t,n,table,sql in objects]}


def verify_contracts():
    if (digest(_body(OLD20_FTS_OBJECTS)) != OLD20_FTS_SHA256
            or digest(_body(OLD20_NO_FTS_OBJECTS)) != OLD20_NO_FTS_SHA256
            or digest(_body(PRE_EXPORT_OBJECTS)) != PRE_EXPORT_SHA256
            or digest(_body(TARGET_OBJECTS)) != TARGET_SHA256):
        raise ReleaseError("内置旧结构迁移契约指纹不符，未执行任何输入SQL")


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def create_target(database):
    """Only built-in literal DDL, never the candidate/archive's SQL text."""
    verify_contracts()
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        for kind in ("table", "index"):
            for typ,name,_,sql in TARGET_OBJECTS:
                if typ == kind and sql and not name.startswith("chunks_fts_"):
                    db.execute(sql)
        db.commit()
    if schema_snapshot(database)["sha256"] != TARGET_SHA256:
        raise ReleaseError("内置目标建表结果不符；保留事务文件，不发布")


def upgrade(database, migration_id):
    """Explicit named row copy; original extracted file replaced only on success."""
    database = no_link(database)
    verify_contracts()
    if migration_id not in SOURCES or schema_snapshot(database)["sha256"] != SOURCES[migration_id]:
        raise ReleaseError("没有此精确旧20表结构的静态迁移项")
    destination = no_link(database.with_name(".legacy-current-schema.sqlite3"))
    if destination.exists():
        raise ReleaseError("迁移事务内已有目标结构文件，未覆盖恢复证据")
    create_target(destination)
    try:
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(destination)) as target:
            source.execute("PRAGMA query_only=ON");source.execute("PRAGMA trusted_schema=OFF")
            source.execute("BEGIN")
            if source.execute("PRAGMA foreign_key_check").fetchone():
                raise ReleaseError("旧数据关联约束无效，不能复制成已验证新结构")
            target.execute("PRAGMA trusted_schema=OFF");target.execute("BEGIN IMMEDIATE")
            for table, columns in sorted(OLD20_COLUMNS.items()):
                # Physical SQLite column positions differ across versions.
                # The only new-column values are the six reviewed defaults.
                names = ','.join(map(quote, columns))
                types = {row[1]: row[2].upper() for row in
                         target.execute("PRAGMA table_info(" + quote(table) + ")")}
                added = DEFAULTS.get(table, {})
                all_names = ','.join(map(quote, (*columns, *added)))
                statement = "INSERT INTO " + quote(table) + " (" + all_names + ") VALUES (" + ','.join('?' for _ in (*columns,*added)) + ")"
                ordering = ','.join("typeof(" + quote(c) + ")," + quote(c) + " COLLATE BINARY" for c in columns)
                for row in source.execute("SELECT " + names + " FROM " + quote(table) + " ORDER BY " + ordering):
                    validate_row(table, columns, row, types)
                    target.execute(statement, (*row, *added.values()))
            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise ReleaseError("复制后数据关联不符，未发布迁移目录")
            target.commit()
        if schema_snapshot(destination)["sha256"] != TARGET_SHA256:
            raise ReleaseError("复制后的目标结构指纹不符")
        os.replace(destination, database)
    except (sqlite3.Error, OverflowError):
        raise ReleaseError("旧20表的行或类型不能无损映射到审核目标；保留事务和原始归档") from None


def upgrade_previous_current(database):
    """Add only reviewed export DDL to the exact former current private copy."""
    database = no_link(database)
    verify_contracts()
    if schema_snapshot(database)["sha256"] != PRE_EXPORT_SHA256:
        raise ReleaseError("没有此精确导出前结构的静态迁移项")
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        if digest(_body(rows)) != PRE_EXPORT_SHA256 or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ReleaseError("导出前结构或关联约束在迁移前改变，未提交")
        for kind in ("table", "index"):
            for typ, _, _, sql in ARTIFACT_EXPORT_OBJECTS:
                if typ == kind:
                    db.execute(sql)
        rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        if digest(_body(rows)) != TARGET_SHA256:
            raise ReleaseError("固定导出加表结果与审核目标不符，未提交")
        if db.execute("SELECT 1 FROM artifact_exports LIMIT 1").fetchone():
            raise ReleaseError("结构迁移不得生成已完成导出")
        db.commit()


def verify_empty_exports(database):
    """An additive old-data migration never invents a completed binary output."""
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("SELECT 1 FROM artifact_exports LIMIT 1").fetchone():
            raise ReleaseError("旧结构迁移意外产生固定导出，未发布")


def rebuild_fts(database):
    """Rebuild from retained chunks; never certify old external vector files."""
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA trusted_schema=OFF");db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM chunks_fts")
        count = 0
        for ident,paper,content in db.execute("SELECT id,paper_id,content FROM chunks ORDER BY id"):
            if not isinstance(content, str):
                raise ReleaseError("旧文献索引正文不是文本，不能重建全文检索")
            chars = []
            for i,c in enumerate(content):
                chars.append(c)
                if '\u4e00' <= c <= '\u9fff' and i+1 < len(content) and '\u4e00' <= content[i+1] <= '\u9fff':
                    chars.append(' ')
            db.execute("INSERT INTO chunks_fts(chunk_id,paper_id,content) VALUES (?,?,?)",(ident,paper,''.join(chars)))
            count += 1
        if db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone() != (count,):
            raise ReleaseError("全文索引重建行数不一致")
        db.commit()
    return {"mode": "reconstructed_lexical_only", "chunks": count,
            "new_embedding_calls": 0, "modern_index_receipt_created": False}


def verify_new_defaults(database):
    with closing(sqlite3.connect(database)) as db:
        for table, values in DEFAULTS.items():
            for name,value in values.items():
                predicate = quote(name) + (" IS NOT NULL" if value is None else " IS NULL OR " + quote(name) + " != ?")
                if db.execute("SELECT 1 FROM " + quote(table) + " WHERE " + predicate + " LIMIT 1",
                              () if value is None else (value,)).fetchone():
                    raise ReleaseError("旧20表迁移的新列包含非审核默认，未发布")


def verify_new_tables(database):
    """No implicit state/credential/evidence activation from a structural copy."""
    permitted = {"sources", "source_snapshots", "source_revisions", "source_library_bindings",
        "source_library_imports", "source_extractions", "library_chunk_archives", "chunks_fts"}
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA query_only=ON");db.execute("PRAGMA trusted_schema=OFF")
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            if table in OLD20_COLUMNS or table in permitted or table.startswith(("sqlite_", "chunks_fts_")):
                continue
            if db.execute("SELECT 1 FROM " + quote(table) + " LIMIT 1").fetchone():
                raise ReleaseError("旧结构迁移意外产生非审核的新任务、凭证或证据状态，未发布")


def quarantine_vectors(stage, manifest, transaction_id):
    """Retain old default/compat caches without letting NumPy auto-load them.

    We do not open npy/json contents, infer an embedding configuration, follow
    config/env paths or execute old code. Explicit future reindex creates a new
    current cache; these four source-reviewed historical default names stay
    inert. Custom filenames are never selected as a current default by import.
    """
    names = {"vectors.npy", "vectors.npz", "vectors.npz.npy", "vectors.json"}
    selected = sorted((row for row in manifest["files"] if row["path"].casefold() in names),
                      key=lambda row: row["path"])
    folder = "legacy-dormant-indexes-" + transaction_id
    if any(row["path"].casefold() == folder.casefold() or
           row["path"].casefold().startswith(folder.casefold()+"/") for row in manifest["files"]):
        raise ReleaseError("旧归档占用休眠索引目录，未覆盖任何历史资产")
    result = {"protocol": "legacy_default_vectors_retained_inert_v1", "assets": [],
              "new_embedding_fingerprint_created": False, "current_default_vector_cache_active": False}
    if not selected:
        return result
    destination = contained(stage, folder)
    if destination.exists():
        raise ReleaseError("休眠索引目录已存在，未覆盖恢复证据")
    destination.mkdir()
    for row in selected:
        source = contained(stage, row["path"])
        if file_record(stage, source) != row:
            raise ReleaseError("旧向量资产在封存前变化，未发布")
        relative = folder + "/" + row["path"]
        target = contained(stage, relative)
        if target.exists():
            raise ReleaseError("休眠索引文件已存在，未覆盖历史资产")
        source.rename(target)
        actual = file_record(stage, target)
        if (actual["sha256"], actual["size"]) != (row["sha256"], row["size"]):
            raise ReleaseError("旧向量资产封存校验失败，未发布")
        result["assets"].append({"original_path":row["path"], "dormant_path":relative,
                                 "size":row["size"], "sha256":row["sha256"]})
    return result
