"""Versioned application-data archives and restart-only, recoverable restore.

This module deliberately imports neither config nor the database engine, so
pending restores run before those components load state. Backups contain all
regular files under DATA_DIR except runtime and backup artifacts; external
project folders are references, not silently copied or overwritten.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import shutil
import sqlite3
import stat
import time
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .atomic_files import atomic_write_json as _atomic_write_json
from . import backup_credentials as credentials
from .runtime_lock_file import open_lock_file, validate_open_lock, LockIdentityError


class BackupError(ValueError):
    pass


FORMAT = "scholarflow-application-data"
VERSION = 1
MAX_FILES = 200_000
MAX_TOTAL_BYTES = 50 * 1024**3
MAX_MANIFEST_BYTES = 32 * 1024**2
BLOCK = 1024 * 1024
SKIP_ROOTS = {".runtime.lock", ".runtime.json", "backups"}
_PRIVATE_ROLLBACK = object()


def _windows_retry(operation):
    deadline = time.monotonic() + 2
    while True:
        try:
            return operation()
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(.03)


def atomic_write_json(path, value):
    return _windows_retry(lambda: _atomic_write_json(path, value))


def _replace(source, target):
    return _windows_retry(lambda: os.replace(source, target))


def recovery_root(data_dir):
    root = Path(data_dir).resolve()
    return root.parent / ("." + root.name + "-recovery")


def _safe_relative(value):
    if (not isinstance(value, str) or not value or len(value) > 2048 or ntpath.splitdrive(value)[0]
            or "\\" in value or value.startswith("/") or any(ord(c) < 32 for c in value)):
        raise BackupError("备份包含无效路径")
    parts = value.split("/")
    for part in parts:
        base = part.split(".", 1)[0].upper()
        if (part in ("", ".", "..") or part.endswith((".", " ")) or any(c in part for c in ':*?"<>|')
                or base in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
            raise BackupError("备份路径不能穿越目录或使用系统保留名称")
    return value


def _plain_path(path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise BackupError("数据目录包含链接或目录联接，请先核对其原件后再备份")


def _id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise BackupError("备份或恢复编号无效")
    return value


def _digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def _skip(rel, database):
    # Archive paths are portable to case-insensitive Windows volumes. A
    # differently cased runtime lock must never be staged for replacement.
    rel, database = rel.casefold(), database.casefold()
    top = rel.split("/")[0]
    return top in SKIP_ROOTS or top.startswith(".runtime-") or rel in {
        database + "-wal", database + "-shm", database + "-journal"}


def _inventory(root, database):
    result = {}
    total = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        parent = Path(folder)
        kept = []
        for name in sorted(dirs):
            path = parent / name
            rel = path.relative_to(root).as_posix()
            if _skip(rel, database):
                continue
            _plain_path(path)
            _safe_relative(rel)
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = parent / name
            rel = path.relative_to(root).as_posix()
            if _skip(rel, database):
                continue
            _safe_relative(rel)
            _plain_path(path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise BackupError("备份只接受普通文件")
            total += info.st_size
            if len(result) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise BackupError("数据超过当前备份容量限制，请分离外部项目后重试")
            result[rel] = (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)
    return result


def _connect_readonly(path):
    return sqlite3.connect("file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro", uri=True, timeout=5)


def _check_database(path):
    try:
        with closing(_connect_readonly(path)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("备份数据库完整性校验未通过")
    except sqlite3.Error:
        raise BackupError("备份数据库无法读取") from None


def _database_snapshot(source, target):
    deadline = time.monotonic() + 60
    def progress(*args):
        if time.monotonic() > deadline:
            raise BackupError("数据库备份超时，原数据未改动")
    with closing(_connect_readonly(source)) as live, closing(sqlite3.connect(target)) as copy:
        live.backup(copy, pages=256, progress=progress, sleep=0.02)
    _check_database(target)


def create_backup(data_dir, *, database="scholarflow.db", destination=None, progress=None, origin_data_dir=None):
    """Create a downloadable archive; credentials can never be opted in."""
    return _create_backup(data_dir, database=database, destination=destination,
                          progress=progress, origin_data_dir=origin_data_dir)


def _create_backup(data_dir, *, database="scholarflow.db", destination=None, progress=None,
                   origin_data_dir=None, _private_rollback=None):
    root = Path(data_dir).resolve(strict=True)
    private = _private_rollback is _PRIVATE_ROLLBACK
    if _private_rollback is not None and not private:
        raise BackupError("内部回退凭证权限无效")
    if private:
        # Only the durable, local previous/ tree may retain credentials. This
        # capability is never part of create_backup or any HTTP request model.
        expected = recovery_root(origin_data_dir) / "staged" if origin_data_dir else None
        if (expected is None or root.name != "previous" or root.parent.parent != expected
                or destination is None or Path(destination).resolve().parent != expected):
            raise BackupError("凭证保留仅允许本机内部恢复回退")
        _id(root.parent.name)
    _safe_relative(database)
    if "/" in database:
        raise BackupError("数据库必须位于应用数据目录顶层")
    _plain_path(root)
    folder = recovery_root(root) / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    backup_id = uuid4().hex
    target = Path(destination) if destination else folder / f"{backup_id}.sfbak"
    if target.exists() or target.resolve().is_relative_to(root):
        raise BackupError("备份目标已存在或位于正在备份的数据目录内")
    temporary = target.with_name("." + target.name + ".tmp")
    db_copy = folder / ("." + backup_id + ".sqlite3")
    originals = _inventory(root, database)
    if database not in originals:
        raise BackupError("应用数据库不存在，未生成不完整备份")
    files = {name: identity for name, identity in originals.items()
             if private or not credentials.excluded_managed_path(name)}
    try:
        _database_snapshot(root / database, db_copy)
        if not private:
            credentials.sanitized_database(db_copy)
            _check_database(db_copy)
        entries = []
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
            for index, (relative, identity) in enumerate(files.items()):
                source = db_copy if relative == database else root / relative
                _plain_path(source)
                h, size = hashlib.sha256(), 0
                with source.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if relative != database and (opened.st_size, opened.st_mtime_ns, opened.st_dev, opened.st_ino) != identity:
                        raise BackupError("备份期间文件已变化，请稍后重试")
                    if not private and credentials.is_managed_configuration(relative):
                        raw = stream.read(credentials.MAX_CONFIGURATION_BYTES + 1)
                        blocks = (credentials.sanitized_configuration(relative, raw),)
                    else:
                        blocks = iter(lambda: stream.read(BLOCK), b"")
                    with archive.open("data/" + relative, "w", force_zip64=True) as output:
                        for block in blocks:
                            size += len(block)
                            if size > MAX_TOTAL_BYTES:
                                raise BackupError("备份文件超限")
                            h.update(block)
                            output.write(block)
                    if relative != database and os.fstat(stream.fileno()).st_mtime_ns != opened.st_mtime_ns:
                        raise BackupError("备份期间文件已变化，请稍后重试")
                entries.append({"path": relative, "size": size, "sha256": h.hexdigest()})
                if progress:
                    progress(index + 1, len(files))
            after = _inventory(root, database)
            # DB/WAL are snapshotted through SQLite, and the admission barrier
            # provides file/DB quiescence. External edits still fail closed.
            if {k: v for k, v in after.items() if k != database} != {k: v for k, v in originals.items() if k != database}:
                raise BackupError("资料在备份期间变化，未发布混合版本备份")
            manifest = {"format": FORMAT, "version": VERSION, "id": backup_id,
                "created_at": datetime.now(timezone.utc).isoformat(), "database": database,
                "source_data_dir": str(Path(origin_data_dir or root).resolve()), "files": entries,
                "scope": "application_data", "external_project_files_included": False,
                "credential_portability": credentials.PRIVATE_ORIGINAL_USER if private else credentials.EXCLUDED_RECONFIGURE,
                "credential_exclusion_scope": None if private else "application_managed_stores",
                "excluded": sorted(SKIP_ROOTS) + [database + "-wal", database + "-shm", database + "-journal"]
                            + sorted(set(originals) - set(files))}
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        verify_archive(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        _replace(temporary, target)
        return {"id": backup_id, "path": str(target), "size": target.stat().st_size,
                "sha256": _digest(target), "manifest": manifest}
    except credentials.CredentialExportError as exc:
        raise BackupError(str(exc)) from None
    finally:
        temporary.unlink(missing_ok=True)
        db_copy.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            db_copy.with_name(db_copy.name + suffix).unlink(missing_ok=True)


def verify_archive(path, *, extract_to=None):
    root = Path(extract_to).resolve() if extract_to else None
    if root:
        root.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.infolist()
            if len(info) > MAX_FILES + 1 or len({item.filename.casefold() for item in info}) != len(info):
                raise BackupError("备份条目重复或超过数量限制")
            mapping = {item.filename: item for item in info}
            manifest_info = mapping.get("manifest.json")
            if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise BackupError("备份清单缺失或过大")
            manifest = json.loads(archive.read(manifest_info))
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
                raise BackupError("不支持的备份格式或版本")
            database = _safe_relative(manifest.get("database"))
            if "/" in database or _skip(database, database):
                raise BackupError("备份数据库路径无效")
            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
                raise BackupError("备份文件清单无效")
            expected, total = {"manifest.json"}, 0
            for entry in entries:
                if not isinstance(entry, dict):
                    raise BackupError("备份文件记录无效")
                relative = _safe_relative(entry.get("path"))
                if _skip(relative, database):
                    raise BackupError("备份包含禁止恢复的运行文件")
                name = "data/" + relative
                if name in expected:
                    raise BackupError("备份清单包含重复文件")
                expected.add(name)
                member = mapping.get(name)
                size, digest = entry.get("size"), entry.get("sha256")
                if (type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest) or member is None
                        or member.is_dir() or member.file_size != size or stat.S_ISLNK(member.external_attr >> 16)):
                    raise BackupError("备份条目缺失、类型或大小不匹配")
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise BackupError("备份解压大小超过限制")
                if root:
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    output = target.open("xb")
                else:
                    output = None
                h, read = hashlib.sha256(), 0
                try:
                    with archive.open(member) as source:
                        for block in iter(lambda: source.read(BLOCK), b""):
                            read += len(block)
                            if read > size:
                                raise BackupError("备份条目实际大小超过声明")
                            h.update(block)
                            if output:
                                output.write(block)
                finally:
                    if output:
                        output.close()
                if read != size or h.hexdigest() != digest:
                    raise BackupError("备份内容哈希不匹配，未恢复")
            if set(mapping) != expected or "data/" + database not in expected:
                raise BackupError("备份包含清单外文件或缺少数据库")
            if root:
                _check_database(root / database)
                credentials.validate_restore_database(root / database)
                if manifest.get("credential_portability") == credentials.EXCLUDED_RECONFIGURE:
                    credentials.validate_excluded_backup(root, database, [entry["path"] for entry in entries])
            return manifest
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, EOFError, KeyError) as exc:
        if root and root.exists():
            # root was created exclusively above, never caller-owned content.
            shutil.rmtree(root)
        if isinstance(exc, BackupError):
            raise
        if isinstance(exc, credentials.CredentialExportError):
            raise BackupError(str(exc)) from None
        raise BackupError("备份无法读取或校验失败，原数据未改动") from None


def _suspend_restored_automation(stage, manifest, target_root):
    """Import data as dormant state; restore must not trigger queued API calls."""
    database = stage / manifest["database"]
    # Recheck at the write boundary even when the archive was already checked.
    credentials.validate_restore_database(database)
    adjustments = []
    with closing(sqlite3.connect(database)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, sql in {
            "subscriptions": "UPDATE subscriptions SET active=0",
            "run_inputs": "UPDATE run_inputs SET status='cancelled', reason='从备份恢复后未自动执行；旧派发结果可能未知，请核对原执行回执后重新发送' WHERE status IN ('queued','dispatching','dispatched')",
            "conversation_options": "UPDATE conversation_options SET approval_mode='read_only', revision=revision+1",
        }.items():
            if table in tables:
                db.execute(sql)
                adjustments.append(table)
        # These are managed copies. External project paths and immutable
        # provenance are historical identities and must never be rewritten.
        old_root = manifest.get("source_data_dir")
        known = {item["path"] for item in manifest["files"]}
        if isinstance(old_root, str) and old_root and "papers" in tables:
            cols = {r[1] for r in db.execute('PRAGMA table_info(papers)')}
            for column in ("pdf_path", "parsed_path"):
                if column not in cols:
                    continue
                for paper_id, original in db.execute(f'SELECT id, {column} FROM papers'):
                    if not original:
                        continue
                    try:
                        relative = ntpath.relpath(original, old_root).replace("\\", "/")
                        _safe_relative(relative)
                    except (ValueError, BackupError):
                        continue
                    if relative in known:
                        db.execute(f'UPDATE papers SET {column}=? WHERE id=?',
                                   (str(target_root / relative), paper_id))
            adjustments.append("managed_paper_paths_relocated")
        db.commit()
        _verify_suspended_database(db, tables)
    config = stage / "config.json"
    if config.exists():
        data = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise BackupError("恢复配置格式无效")
        data["approval_mode"] = "read_only"
        atomic_write_json(config, data)
        adjustments.append("approval_mode")
    hooks = stage / "hooks.json"
    if hooks.exists():
        # load_hooks does not interpret enabled flags. Empty the effective
        # configuration and keep an inert copy for deliberate later review.
        original = json.loads(hooks.read_text(encoding="utf-8"))
        atomic_write_json(stage / ("hooks.restore-review-" + uuid4().hex + ".json"), original)
        atomic_write_json(hooks, {})
        adjustments.append("hooks_disabled")
    mcp = stage / "mcp_servers.json"
    if mcp.exists():
        original = json.loads(mcp.read_text(encoding="utf-8"))
        if not isinstance(original, dict) or not isinstance(original.get("servers", []), list):
            raise BackupError("恢复 MCP 配置格式无效")
        for server in original.get("servers", []):
            if isinstance(server, dict):
                server["enabled"] = False
        atomic_write_json(mcp, original)
        adjustments.append("external_mcp_disabled")
    if manifest.get("credential_portability") == credentials.EXCLUDED_RECONFIGURE:
        credentials.validate_excluded_backup(stage, manifest["database"],
                                              [item["path"] for item in manifest["files"]])
    return adjustments


def _verify_suspended_database(db, tables):
    credentials.validate_restore_database(db.execute("PRAGMA database_list").fetchone()[2])
    if "subscriptions" in tables and db.execute("SELECT 1 FROM subscriptions WHERE active != 0 OR active IS NULL LIMIT 1").fetchone():
        raise BackupError("恢复后的订阅未全部暂停，拒绝继续")
    if "run_inputs" in tables and db.execute("SELECT 1 FROM run_inputs WHERE status IN ('queued','dispatching','dispatched') LIMIT 1").fetchone():
        raise BackupError("恢复后的待发送任务未全部暂停，拒绝继续")
    if "conversation_options" in tables and db.execute(
            "SELECT 1 FROM conversation_options WHERE approval_mode IS NULL OR approval_mode != 'read_only' "
            "OR revision IS NULL LIMIT 1").fetchone():
        raise BackupError("恢复后的会话审批模式未全部设为只读，拒绝继续")


def prepare_restore(data_dir, archive_path, *, expected_database=None):
    root = Path(data_dir).resolve()
    restore_id = uuid4().hex
    folder = recovery_root(root) / "staged" / restore_id
    folder.mkdir(parents=True, exist_ok=False)
    stage = folder / "data"
    try:
        manifest = verify_archive(archive_path, extract_to=stage)
        if expected_database is not None and manifest["database"] != expected_database:
            raise BackupError("备份数据库名称与当前应用配置不一致，请使用匹配的配置恢复；现有数据未替换")
        adjustments = _suspend_restored_automation(stage, manifest, root)
        inventory = _inventory(stage, manifest["database"])
        files = [{"path": name, "sha256": _digest(stage / name), "size": value[0]} for name, value in inventory.items()]
        review = {"id": restore_id, "created_at": manifest.get("created_at"), "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files), "database": manifest["database"],
            "source_data_dir": manifest.get("source_data_dir"), "target_data_dir": str(root),
            "external_project_files_included": False, "adjustments": adjustments, "files": files,
            "credential_portability": manifest.get("credential_portability"),
            "status": "prepared", "requires_restart": True}
        atomic_write_json(folder / "review.json", review)
        return {k: v for k, v in review.items() if k != "files"}
    except BaseException:
        shutil.rmtree(folder)
        raise


def _load_review(root, restore_id):
    folder = recovery_root(root) / "staged" / _id(restore_id)
    try:
        review = json.loads((folder / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError("恢复暂存记录不存在") from None
    if review.get("id") != restore_id or review.get("target_data_dir") != str(Path(root).resolve()):
        raise BackupError("恢复记录与目标数据目录不匹配")
    return folder, review


def _verify_tree(stage, review, *, allow_runtime=False):
    _plain_path(stage)
    if not allow_runtime and any(_skip(p.name, review["database"]) for p in stage.iterdir()):
        raise BackupError("恢复暂存目录包含禁止恢复的运行文件")
    actual = _inventory(stage, review["database"])
    expected = {item["path"] for item in review["files"]}
    if set(actual) != expected:
        raise BackupError("恢复暂存文件清单已变化")
    for entry in review["files"]:
        path = stage / _safe_relative(entry["path"])
        if path.stat().st_size != entry["size"] or _digest(path) != entry["sha256"]:
            raise BackupError("恢复暂存文件已变化，未修改现有数据")
    _check_database(stage / review["database"])
    try:
        credentials.validate_restore_database(stage / review["database"])
    except credentials.CredentialExportError as exc:
        raise BackupError(str(exc)) from None


def _verify_staged(folder, review):
    _verify_tree(folder / "data", review)


def schedule_restore(data_dir, restore_id):
    root = Path(data_dir).resolve()
    folder, review = _load_review(root, restore_id)
    _verify_staged(folder, review)
    pending = recovery_root(root) / "pending.json"
    if pending.exists():
        current = json.loads(pending.read_text(encoding="utf-8"))
        if current.get("id") != restore_id:
            raise BackupError("已有另一份恢复等待重启，请先取消")
        return {"id": restore_id, "status": "scheduled", "requires_restart": True}
    atomic_write_json(pending, {"id": restore_id, "target_data_dir": str(root), "status": "scheduled"})
    return {"id": restore_id, "status": "scheduled", "requires_restart": True}


def cancel_restore(data_dir):
    pending = recovery_root(data_dir) / "pending.json"
    if not pending.exists():
        return {"status": "none"}
    current = json.loads(pending.read_text(encoding="utf-8"))
    if current.get("status") != "scheduled":
        raise BackupError("恢复已开始，不能删除恢复日志")
    pending.unlink()
    return {"status": "cancelled"}


@contextmanager
def _runtime_exclusive(root):
    root.mkdir(parents=True, exist_ok=True)
    try:
        handle = open_lock_file(root / ".runtime.lock")
    except LockIdentityError as exc:
        raise BackupError(str(exc)) from None
    try:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BackupError("应用仍在运行，待其完全退出后才能恢复") from None
        try:
            validate_open_lock(handle, root / ".runtime.lock")
        except LockIdentityError as exc:
            raise BackupError(str(exc)) from None
        yield
    finally:
        handle.close()


def apply_pending_restore(data_dir, *, expected_database=None):
    """Called before config/engine startup; durable move intents survive crash."""
    root = Path(data_dir).resolve()
    pending_path = recovery_root(root) / "pending.json"
    if not pending_path.exists():
        return None
    with _runtime_exclusive(root):
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if pending.get("target_data_dir") != str(root):
            raise BackupError("待恢复目录不匹配")
        folder, review = _load_review(root, pending["id"])
        if expected_database is not None and review["database"] != expected_database:
            raise BackupError("待恢复数据库与启动配置不一致，请使用确认恢复时的数据库配置")
        stage, rollback = folder / "data", folder / "previous"
        if pending["status"] == "scheduled":
            _verify_staged(folder, review)
            # Only the contents of this exact data directory can be replaced.
            # External projects referenced in the DB are not touched.
            old = sorted(p.name for p in root.iterdir() if p.name.casefold() != ".runtime.lock")
            for name in old:
                _plain_path(root / name)
                _safe_relative(name)
            new = sorted(p.name for p in stage.iterdir())
            if any(_skip(name, review["database"]) for name in new):
                raise BackupError("恢复内容与运行保留文件冲突")
            # A crash after creating this directory but before recording the
            # move intent may leave an empty directory. Never reuse contents.
            rollback.mkdir(exist_ok=True)
            if any(rollback.iterdir()):
                raise BackupError("恢复前保留目录意外包含文件")
            pending.update(status="applying", old=old, new=new, moved_old=[], moved_new=[])
            atomic_write_json(pending_path, pending)
        if pending["status"] != "applying":
            raise BackupError("无法识别的恢复日志状态")
        for name in pending["old"]:
            _safe_relative(name)
            destination, source = rollback / name, root / name
            if name not in pending["moved_old"]:
                if not destination.exists():
                    if not source.exists():
                        raise BackupError("恢复前原件缺失，请保留恢复目录并检查")
                    _replace(source, destination)
                pending["moved_old"].append(name)
                atomic_write_json(pending_path, pending)
        for name in pending["new"]:
            _safe_relative(name)
            source, destination = stage / name, root / name
            if name not in pending["moved_new"]:
                if source.exists():
                    if destination.exists():
                        raise BackupError("恢复目标出现意外内容，请检查后重试")
                    _replace(source, destination)
                elif not destination.exists():
                    raise BackupError("恢复暂存原件缺失")
                pending["moved_new"].append(name)
                atomic_write_json(pending_path, pending)
        _verify_tree(root, review, allow_runtime=True)
        result = {"id": pending["id"], "status": "restored", "restored_at": datetime.now(timezone.utc).isoformat(),
                  "previous_data_retained": True, "adjustments": review["adjustments"],
                  "credential_portability": review.get("credential_portability")}
        atomic_write_json(recovery_root(root) / "last-restore.json", result)
        pending_path.unlink()
        return result


def prepare_rollback(data_dir, *, expected_database=None):
    root = Path(data_dir).resolve()
    record = recovery_root(root) / "last-restore.json"
    try:
        last = json.loads(record.read_text(encoding="utf-8"))
        folder, review = _load_review(root, last["id"])
    except (OSError, ValueError, KeyError):
        raise BackupError("没有可回退的恢复前数据") from None
    previous = folder / "previous"
    _plain_path(previous)
    if not previous.is_dir():
        raise BackupError("恢复前数据不存在")
    exported = _create_backup(previous, database=review["database"], origin_data_dir=root,
                              destination=recovery_root(root) / "staged" / (uuid4().hex + ".sfbak"),
                              _private_rollback=_PRIVATE_ROLLBACK)
    try:
        result = prepare_restore(root, exported["path"], expected_database=expected_database)
        result["rollback_of"] = last["id"]
        return result
    finally:
        Path(exported["path"]).unlink(missing_ok=True)
