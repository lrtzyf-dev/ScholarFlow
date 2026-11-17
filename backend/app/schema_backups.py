"""Verified SQLite snapshots taken before changing an existing schema.

These are database migration recovery points, not full research-data backups.
They deliberately do not copy an active SQLite file or its WAL with shutil.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import inspect

from .atomic_files import atomic_write_json


class MigrationProtectionError(RuntimeError):
    pass


def schema_changes(engine, metadata) -> dict:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    if not existing:
        return {}
    missing_tables = sorted(set(metadata.tables) - existing)
    missing_columns = {}
    for name, table in metadata.tables.items():
        if name in existing:
            columns = {column["name"] for column in inspector.get_columns(name)}
            missing = sorted(set(table.columns.keys()) - columns)
            if missing:
                missing_columns[name] = missing
    return {"tables": missing_tables, "columns": missing_columns} if missing_tables or missing_columns else {}


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _readonly(path: Path):
    return sqlite3.connect(f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro", uri=True, timeout=5)


def verify_database_snapshot(path: Path, *, expected_hash: str | None = None) -> None:
    path = Path(path)
    if not path.is_file() or (expected_hash is not None and _digest(path) != expected_hash):
        raise MigrationProtectionError("数据库恢复点缺失或校验不匹配，未进行迁移/恢复")
    try:
        with closing(_readonly(path)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise MigrationProtectionError("数据库恢复点完整性检查失败")
    except sqlite3.Error as exc:
        raise MigrationProtectionError("数据库恢复点不可读取") from exc


def prepare_schema_snapshot(engine, metadata) -> Path | None:
    """Fail closed before DDL. Called during startup, before worker admission."""
    changes = schema_changes(engine, metadata)
    if not changes or engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    source = Path(database).resolve()
    folder = source.parent / "backups" / "schema"
    temporary = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
        snapshot = folder / f"{name}.sqlite3"
        descriptor, temporary = tempfile.mkstemp(prefix=".schema-", suffix=".tmp", dir=folder)
        os.close(descriptor)
        deadline = time.monotonic() + 30

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise MigrationProtectionError("数据库恢复点创建超时，未进行升级；请结束其他数据库写入后重试")

        with closing(_readonly(source)) as origin, closing(sqlite3.connect(temporary)) as target:
            origin.backup(target, pages=256, progress=progress, sleep=0.02)
        verify_database_snapshot(Path(temporary))
        with open(temporary, "r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, snapshot)
        temporary = None
        manifest = {
            "schema_version": 1, "type": "database_schema_recovery",
            "snapshot": snapshot.name, "sha256": _digest(snapshot),
            "size": snapshot.stat().st_size, "changes": changes,
            "status": "prepared", "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": "database_only", "contains_protected_credentials": True,
        }
        atomic_write_json(snapshot.with_suffix(".json"), manifest)
        return snapshot
    except (OSError, sqlite3.Error) as exc:
        raise MigrationProtectionError("无法创建并验证升级恢复点，数据库升级未开始；请检查磁盘空间和目录权限") from exc
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def finish_schema_snapshot(snapshot: Path | None, *, succeeded: bool) -> None:
    if snapshot is None:
        return
    path = snapshot.with_suffix(".json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["status"] = "migration_succeeded" if succeeded else "migration_failed"
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(path, manifest)


def stage_database_restore(snapshot: Path, destination: Path) -> Path:
    """Restore to a NEW staging path; never replace a live application database."""
    snapshot, destination = Path(snapshot).resolve(), Path(destination).resolve()
    if destination.exists() or snapshot == destination:
        raise MigrationProtectionError("恢复目标必须是不存在的新文件，不能覆盖正在使用的数据")
    manifest = json.loads(snapshot.with_suffix(".json").read_text(encoding="utf-8"))
    if manifest.get("snapshot") != snapshot.name:
        raise MigrationProtectionError("恢复点清单不匹配")
    verify_database_snapshot(snapshot, expected_hash=manifest.get("sha256"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents a concurrent restore overwriting another one.
    with destination.open("xb") as stream:
        try:
            with snapshot.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            destination.unlink(missing_ok=True)
            raise
    try:
        verify_database_snapshot(destination, expected_hash=manifest["sha256"])
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination
