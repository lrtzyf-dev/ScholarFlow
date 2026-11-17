"""One bounded maintenance job at a time, outside the live data directory."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import data_backup as core, maintenance
from .config import DATA_DIR, settings

_guard = threading.Lock()
_worker = None
_current_id = None
_records_lock = threading.RLock()
_records = {}


def _root():
    return core.recovery_root(DATA_DIR)


def _record_path(job_id):
    return _root() / "jobs" / (core._id(job_id) + ".json")


def _save(record):
    with _records_lock:
        _records[str(_record_path(record["id"]))] = deepcopy(record)
        core.atomic_write_json(_record_path(record["id"]), record)


def job(job_id):
    with _records_lock:
        try:
            path = _record_path(job_id)
            record = deepcopy(_records.get(str(path))) or json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise core.BackupError("备份任务不存在") from None
        if record.get("status") in ("queued", "running") and _current_id != job_id:
            record.update(status="interrupted", error="应用曾退出；没有自动重试，请核对结果后重新操作")
    return record


def status():
    folder = _root()
    jobs = sorted((folder / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    records = []
    for path in jobs:
        try:
            records.append(job(path.stem))
        except core.BackupError:
            continue
    def metadata(name):
        try:
            result = json.loads((folder / name).read_text(encoding="utf-8"))
            return {k: result[k] for k in ("id", "status", "restored_at", "adjustments", "credential_portability") if k in result}
        except (OSError, ValueError):
            return None
    return {"jobs": records, "pending_restore": metadata("pending.json"),
            "last_restore": metadata("last-restore.json"), "maintenance_reason": maintenance.current_reason(),
            "scope": "application_data", "external_project_files_included": False,
            "credential_portability": core.credentials.EXCLUDED_RECONFIGURE}


def assert_idle():
    from . import run_store, task_runner, terminal_sessions, git_operations, worktrees, workspace_transfers, project_commands
    if settings.redis_url:
        raise core.BackupError("当前配置了外部任务队列；请先停止外部 worker 并切回桌面本地模式，再做一致性备份或恢复")
    with project_commands._guard:
        commands = bool(project_commands._running)
    if (run_store.active_runs() or task_runner.running_task_ids() or terminal_sessions.active()
            or git_operations.active() or worktrees.active() or workspace_transfers.active() or commands):
        raise core.BackupError("仍有运行中的会话、终端或项目操作。请待其结束后重试；任务没有被取消")


def start(kind, *, upload=None, restore_id=None):
    global _worker, _current_id
    if kind not in ("create", "prepare", "rollback", "schedule", "cancel"):
        raise core.BackupError("未知备份操作")
    if not _guard.acquire(blocking=False):
        raise core.BackupError("另一项备份或恢复操作正在进行，请等其完成")
    record = {"id": uuid4().hex, "kind": kind, "status": "queued",
              "created_at": datetime.now(timezone.utc).isoformat(), "progress": None, "result": None, "error": ""}
    try:
        _save(record)
        _current_id = record["id"]
        def execute():
            global _current_id
            try:
                record["status"] = "running"
                _save(record)
                if kind in ("prepare", "rollback"):
                    # Preview only writes to a newly allocated staging folder;
                    # the application remains usable during archive validation.
                    result = (core.prepare_restore(DATA_DIR, upload, expected_database=settings.db_name) if kind == "prepare"
                              else core.prepare_rollback(DATA_DIR, expected_database=settings.db_name))
                elif kind == "cancel":
                    result = core.cancel_restore(DATA_DIR)
                    maintenance.set_restore_pending(False)
                else:
                    with maintenance.exclusive("正在核对应用数据，请稍后再试", timeout=10):
                        assert_idle()
                        if kind == "schedule":
                            result = core.schedule_restore(DATA_DIR, restore_id)
                            maintenance.set_restore_pending(True)
                        else:
                            if (_root() / "pending.json").exists():
                                raise core.BackupError("恢复等待重启；请先完成或取消恢复")
                            def progress(done, total):
                                if done == total or done % 20 == 0:
                                    record["progress"] = {"completed_files": done, "total_files": total}
                                    _save(record)
                            exported = core.create_backup(DATA_DIR, database=settings.db_name, progress=progress)
                            result = {"backup_id": exported["id"], "size": exported["size"], "sha256": exported["sha256"],
                                      "file_count": len(exported["manifest"]["files"]),
                                      "credential_portability": exported["manifest"]["credential_portability"],
                                      "download_url": f'/api/backups/exports/{exported["id"]}'}
                record.update(status="succeeded", result=result)
            except (core.BackupError, maintenance.MaintenanceBusy) as exc:
                record.update(status="failed", error=str(exc))
            except Exception:
                record.update(status="failed", error="备份或恢复操作未完成，请核对磁盘空间和目录权限；原运行数据未替换")
            finally:
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    try:
                        _save(record)
                    except OSError:
                        record["error"] = "操作结果已返回，但任务记录未能保存到磁盘。请保留下载文件；恢复状态以等待重启提示为准。"
                        with _records_lock:
                            _records[str(_record_path(record["id"]))] = deepcopy(record)
                finally:
                    try:
                        if upload:
                            Path(upload).unlink(missing_ok=True)
                    except OSError:
                        pass  # Inert upload remains under recovery/uploads.
                    finally:
                        _current_id = None
                        _guard.release()
        _worker = threading.Thread(target=execute, name="application-data-maintenance", daemon=True)
        _worker.start()
        return dict(record)
    except BaseException:
        _current_id = None
        _guard.release()
        raise


def export_path(backup_id):
    path = _root() / "exports" / (core._id(backup_id) + ".sfbak")
    core._plain_path(path)
    if not path.is_file():
        raise core.BackupError("备份文件不存在或尚未生成")
    return path


def active():
    return _guard.locked()


def wait_shutdown(timeout=10):
    worker = _worker
    if worker is not None:
        worker.join(timeout=timeout)
