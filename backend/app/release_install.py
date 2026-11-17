"""Offline, signed candidate installation with exclusive locks and a recovery journal.

No downloaded instructions are executed. Only the fixed private health protocol
is invoked. This engine currently admits exact-schema/checkpoint-compatible
updates; schema migrations need an independently reviewed migration transaction.
Neither a signed candidate nor these checks certify public release readiness.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from uuid import uuid4

from .atomic_files import atomic_write_json
from .install_barrier import barrier_path
from .release_files import contained, digest, file_record, no_link, relative_path, validate_records, verify_records
from .release_info import ReleaseError, read_json, validate_definition
from .runtime_lock_file import open_lock_file, validate_open_lock, LockIdentityError
from .release_manifest import verify as verify_inputs, verify_signature
from .release_schema import schema_snapshot, validate_schema, pending_runs
from .release_database import (database_files as _database_files,
    observation_database as _observation_database, database_reservation as _database_reservation)

MANIFEST = "install-manifest.json"
FORMAT = "scholarflow-assembled-candidate"
TERMINAL = {"committed", "rolled_back"}
ACTIVE = {"running", "queued", "pending", "starting", "stopping", "resuming", "cancelling", "dispatching"}


def load_external_trust(path, *, untrusted_roots):
    """A CLI trust file cannot bootstrap trust from any candidate/install slot."""
    path = no_link(no_link(path).resolve())
    for folder in untrusted_roots:
        folder = no_link(no_link(folder).resolve())
        if path == folder or path.is_relative_to(folder):
            raise ReleaseError("信任库必须来自候选、安装和事务目录之外")
    value = read_json(path, 64 * 1024)
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ReleaseError("外部发布者信任库无效")
    return value


def _files(root):
    """Enumerate all files, including hidden entries; reject every link/reparse point."""
    root = no_link(root)
    rows = []
    for folder, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            no_link(Path(folder) / name)
        for name in files:
            path = no_link(Path(folder) / name)
            rows.append(file_record(root, path))
    return validate_records(sorted(rows, key=lambda item: item["path"]))


def describe_candidate(folder, prepared, *, entrypoint="ScholarFlow.exe"):
    """Write an unsigned envelope for an already assembled payload; never build it."""
    folder, prepared = no_link(folder), no_link(prepared)
    if (folder / MANIFEST).exists() or (folder / "release-signature.json").exists():
        raise ReleaseError("候选清单或签名已存在，不能覆盖")
    verified = verify_inputs(prepared)
    definition = validate_definition(read_json(prepared / "release.json"))
    schema = validate_schema(read_json(prepared / "schema.json"))
    relative_path(entrypoint)
    payload = folder / "payload"
    files = _files(payload)
    if not files or entrypoint not in {r["path"] for r in files} or not entrypoint.lower().endswith(".exe"):
        raise ReleaseError("候选程序入口必须是清单内的 EXE")
    value = {"format": FORMAT, "schema_version": 1, "definition": definition,
        "input_build_id": verified["build_id"], "target_schema": schema,
        "entrypoint": entrypoint, "files": files,
        "qualification": "assembled_candidate_not_public_release"}
    value["candidate_id"] = digest(value)
    atomic_write_json(folder / MANIFEST, value)
    return {"candidate_id": value["candidate_id"], "signature": "unsigned", "public_release_ready": False}


def verify_candidate(folder, trusted_keys):
    folder = no_link(folder)
    value = read_json(contained(folder, MANIFEST))
    fields = {"format", "schema_version", "definition", "input_build_id", "target_schema", "entrypoint", "files", "qualification", "candidate_id"}
    if (not isinstance(value, dict) or set(value) != fields or value.get("format") != FORMAT
            or value.get("schema_version") != 1 or value.get("qualification") != "assembled_candidate_not_public_release"
            or value.get("candidate_id") != digest({k: v for k, v in value.items() if k != "candidate_id"})
            or not isinstance(value.get("input_build_id"), str) or not re.fullmatch("[a-f0-9]{64}", value["input_build_id"])):
        raise ReleaseError("安装候选清单无效")
    validate_definition(value["definition"])
    validate_schema(value["target_schema"])
    validate_records(value["files"])
    relative_path(value["entrypoint"])
    if value["entrypoint"] not in {r["path"] for r in value["files"]} or not value["entrypoint"].lower().endswith(".exe"):
        raise ReleaseError("安装候选缺少固定 EXE 入口")
    signature = verify_signature(folder, value, trusted_keys)
    if not signature["trusted"]:
        raise ReleaseError("安装候选必须由外部信任库中已信任的发布者签名")
    verify_records(folder / "payload", value["files"])
    expected = [{**r, "path": "payload/" + r["path"]} for r in value["files"]]
    expected += [file_record(folder, folder / name) for name in (MANIFEST, "release-signature.json")]
    if _files(folder) != sorted(expected, key=lambda r: r["path"]):
        raise ReleaseError("安装候选包含清单外文件")
    return value


@contextmanager
def _exclusive(path):
    path = no_link(path)
    # Neither opening nor locking may write a byte to an existing asset. In
    # particular, an empty hard-linked lock must not receive an initialization
    # byte before its opened-handle identity has been checked. Windows supports
    # locking a byte range beyond EOF, including a completely empty file.
    try:
        stream = open_lock_file(path)
    except LockIdentityError as exc:
        raise ReleaseError(str(exc)) from None
    try:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ReleaseError("安装或数据目录仍被占用，请先结束运行中的研序") from None
        try:
            validate_open_lock(stream, path)
        except LockIdentityError as exc:
            raise ReleaseError(str(exc)) from None
        yield
    finally:
        stream.close()  # OS releases the lock even after process termination.


def _disjoint(*paths):
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                raise ReleaseError("程序、候选、数据与事务目录必须互不包含")


def _roots(target, data):
    target, data = no_link(no_link(target).resolve()), no_link(no_link(data).resolve())
    if target == target.parent or not target.parent.is_dir() or not data.is_dir():
        raise ReleaseError("安装父目录及明确指定的数据目录必须存在")
    state = no_link(target.parent / ("." + target.name + "-updates"))
    _disjoint(target, data, state)
    state.mkdir(exist_ok=True)
    return target, data, state


def _save(folder, journal, state=None):
    if state is not None:
        journal["state"] = state
    atomic_write_json(folder / "journal.json", journal)


def _checkpoint(name):
    """Fault-injection boundary; no public or candidate-supplied callback."""


def _tree_fingerprint(path):
    return digest(_files(path))


def _data_fingerprint(data):
    # Hash only; no private filenames, contents or credentials enter the journal.
    from .data_backup import _inventory, _skip
    records = []
    for name in _inventory(data, "scholarflow.db"):
        if not _skip(name, "scholarflow.db"):
            records.append(file_record(data, data / name))
    return digest(sorted(records, key=lambda r: r["path"]))


def _check_reserved_database(database, records):
    if records is not None and _database_files(database) != records:
        raise ReleaseError("数据库或WAL在安装准备或健康检查期间变化，拒绝提交")


def _health(program, manifest, stage, receipt):
    nonce = secrets.token_hex(32)
    authority = no_link(stage.parent / "health-authority.json")
    if authority.exists() or receipt.exists():
        raise ReleaseError("健康检查回执或目录授权已存在，不重复执行")
    atomic_write_json(authority, {"format": "scholarflow-health-authority", "version": 1,
        "nonce": nonce, "stage": stage.name, "receipt": receipt.name})
    env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PYTHONPATH", "PYTHONHOME", "DATA_DIR"))}
    env.update(DATA_DIR=str(stage), PYTHONUTF8="1")
    command = [str(contained(program / "payload", manifest["entrypoint"])), "--upgrade-health-check",
        "--stage", str(stage), "--receipt", str(receipt), "--nonce", nonce]
    try:
        result = subprocess.run(command, cwd=program / "payload", env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError("候选程序健康检查未能完成") from None
    if result.returncode:
        raise ReleaseError("候选程序健康检查失败")
    actual = read_json(no_link(receipt), 64 * 1024)
    expected = {"format": "scholarflow-install-health", "version": 1, "nonce": nonce,
        "definition": manifest["definition"], "schema_sha256": manifest["target_schema"]["sha256"],
        "input_build_id": manifest["input_build_id"],
        "scope": "disposable_database_only", "model_requests": 0}
    if actual != expected:
        raise ReleaseError("候选程序身份、结构或健康回执不匹配")


def _pending(state):
    path = state / "pending.json"
    if not path.exists():
        return None
    value = read_json(no_link(path), 4096)
    if not isinstance(value, dict) or set(value) != {"transaction_id"} or not re.fullmatch("[a-f0-9]{32}", str(value["transaction_id"])):
        raise ReleaseError("升级事务指针无效，请保留目录并人工检查")
    return no_link(state / value["transaction_id"])


def _barrier_value(folder, journal):
    return {"format": "scholarflow-install-pending", "version": 1,
        "transaction_id": folder.name, "target": journal["target"], "data": journal["data"]}


def _clear_barrier(data, folder, journal):
    path = barrier_path(data)
    if path.exists():
        if read_json(path, 16 * 1024) != _barrier_value(folder, journal):
            raise ReleaseError("升级启动标记属于其它事务，拒绝解除")
        path.unlink()


def _verify_recovery_program(program, data, journal, trusted_keys):
    manifest = verify_candidate(program, trusted_keys)
    database = no_link(data / "scholarflow.db")
    if journal.get("had_database", bool(journal.get("backup_sha256"))) and not database.is_file():
        raise ReleaseError("原数据库缺失，请先核对备份，不能直接解除升级围栏")
    if database.exists():
        with _observation_database(database) as snapshot:
            if schema_snapshot(snapshot)["sha256"] != manifest["target_schema"]["sha256"]:
                raise ReleaseError("恢复时的数据结构已变化，拒绝恢复不兼容程序")
    return manifest


def _recover_locked(target, data, state, trusted_keys):
    folder = _pending(state)
    if folder is None:
        return {"state": "nothing_pending", "data_modified": False}
    journal = read_json(no_link(folder / "journal.json"), 64 * 1024)
    if (not isinstance(journal, dict) or journal.get("format") != "scholarflow-install-transaction"
            or journal.get("version") != 1 or journal.get("target") != str(target) or journal.get("data") != str(data)
            or journal.get("transaction_id") != folder.name
            or type(journal.get("had_previous")) is not bool):
        raise ReleaseError("升级日志与明确指定的安装/数据目录不符")
    if barrier_path(data).exists() and read_json(barrier_path(data), 16 * 1024) != _barrier_value(folder, journal):
        raise ReleaseError("升级启动标记属于其它事务，拒绝恢复")
    if journal["state"] == "committed":
        current = verify_candidate(target, trusted_keys)
        if current["candidate_id"] != journal["candidate_id"]:
            raise ReleaseError("已提交安装与回执不符，请保留现场")
        _clear_barrier(data, folder, journal)
        return {"state": "committed", "transaction_id": folder.name, "data_modified": False}
    if journal["state"] == "rolled_back":
        if journal["had_previous"] and (not target.exists() or _tree_fingerprint(target) != journal["previous_fingerprint"]):
            raise ReleaseError("已回退程序与日志不符，请保留现场")
        if journal["had_previous"]:
            _verify_recovery_program(target, data, journal, trusted_keys)
        if not journal["had_previous"] and target.exists():
            raise ReleaseError("已回退新装目录出现其它程序，请保留现场")
        _clear_barrier(data, folder, journal)
        return {"state": "rolled_back", "transaction_id": folder.name, "data_modified": False}
    if journal["state"] not in {"preparing", "ready", "moving_previous", "activating", "checking_health"}:
        raise ReleaseError("升级日志状态无效，请保留现场")
    previous, rejected = folder / "previous", folder / "rejected"
    if previous.exists():
        if _tree_fingerprint(previous) != journal["previous_fingerprint"]:
            raise ReleaseError("回退原程序已变化，保留现场且拒绝覆盖")
        _verify_recovery_program(previous, data, journal, trusted_keys)
        if target.exists():
            # Never move an unrelated target created after the interrupted update.
            if _tree_fingerprint(target) != journal.get("candidate_fingerprint") or rejected.exists():
                raise ReleaseError("当前程序与事务候选不符，拒绝覆盖")
            target.rename(rejected)
        previous.rename(target)
    elif journal["had_previous"]:
        if not target.exists() or _tree_fingerprint(target) != journal["previous_fingerprint"]:
            raise ReleaseError("回退程序缺失或变化，请保留升级目录")
        _verify_recovery_program(target, data, journal, trusted_keys)
    elif target.exists():
        if _tree_fingerprint(target) != journal.get("candidate_fingerprint") or rejected.exists():
            raise ReleaseError("新装程序与事务候选不符，拒绝覆盖")
        target.rename(rejected)
    _save(folder, journal, "rolled_back")
    _clear_barrier(data, folder, journal)
    return {"state": "rolled_back", "transaction_id": folder.name, "data_modified": False,
        "backup_retained": bool(journal.get("backup_sha256"))}


def recover(target, data, *, trusted_keys):
    target, data, state = _roots(target, data)
    with _exclusive(state / "install.lock"), _exclusive(data / ".runtime.lock"), ExitStack() as guards:
        database = no_link(data / "scholarflow.db")
        if database.exists() and _pending(state) is not None:
            guards.enter_context(_database_reservation(database))
        return _recover_locked(target, data, state, trusted_keys)


def install(candidate, target, data, *, trusted_keys, direction="upgrade", expected_candidate_id=None):
    if direction not in {"upgrade", "rollback"}:
        raise ReleaseError("升级方向无效")
    if expected_candidate_id is not None and (not isinstance(expected_candidate_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_candidate_id) is None):
        raise ReleaseError("预期候选身份无效")
    candidate = no_link(no_link(candidate).resolve())
    target, data, state = _roots(target, data)
    _disjoint(candidate, target, data, state)
    with _exclusive(state / "install.lock"), _exclusive(data / ".runtime.lock"), ExitStack() as guards:
        if barrier_path(data).exists():
            raise ReleaseError("存在未恢复的升级启动标记，请先执行恢复")
        last = _pending(state)
        if last:
            previous_journal = read_json(no_link(last / "journal.json"))
            if previous_journal.get("target") != str(target) or previous_journal.get("data") != str(data):
                raise ReleaseError("此安装位置的事务绑定其它数据目录，不能覆盖原恢复入口")
            if previous_journal.get("state") not in TERMINAL:
                raise ReleaseError("存在未完成升级，请先执行恢复")
        manifest = verify_candidate(candidate, trusted_keys)
        if expected_candidate_id is not None and manifest["candidate_id"] != expected_candidate_id:
            raise ReleaseError("候选身份已变化，请重新校验后执行")
        current = verify_candidate(target, trusted_keys) if target.exists() else None
        if direction == "rollback" and current is None:
            raise ReleaseError("没有可回退的当前程序")
        if current:
            if current["definition"]["data_contract"] != manifest["definition"]["data_contract"]:
                raise ReleaseError("数据或检查点契约不兼容，拒绝替换")
            old_rank = tuple(map(int, current["definition"]["version"].split("."))) + (current["definition"]["build_number"],)
            new_rank = tuple(map(int, manifest["definition"]["version"].split("."))) + (manifest["definition"]["build_number"],)
            if (direction == "upgrade" and new_rank <= old_rank) or (direction == "rollback" and new_rank >= old_rank):
                raise ReleaseError("候选版本与升级/回退方向不符")
        db = no_link(data / "scholarflow.db")
        database_records = None
        if db.exists():
            database_records = guards.enter_context(_database_reservation(db))
            with _observation_database(db) as snapshot:
                if schema_snapshot(snapshot)["sha256"] != manifest["target_schema"]["sha256"]:
                    raise ReleaseError("结构不同，须先完成已审核的迁移事务；禁止直接降级结构")
                if any(count for rows in pending_runs(snapshot).values() for status, count in rows.items() if status in ACTIVE):
                    raise ReleaseError("仍有活动或排队任务，请先完成或明确取消")
        elif any(p.name != ".runtime.lock" for p in data.iterdir()):
            raise ReleaseError("数据目录非空但数据库缺失，拒绝作为全新安装")
        needed = 2 * sum(row["size"] for row in _files(candidate)) + 16 * 1024 * 1024
        if db.exists():
            from .data_backup import _inventory
            needed += 3 * sum(identity[0] for identity in _inventory(data, "scholarflow.db").values())
        if shutil.disk_usage(state).free < needed:
            raise ReleaseError("磁盘空间不足，未替换现有程序")
        folder = state / uuid4().hex
        folder.mkdir()
        journal = {"format": "scholarflow-install-transaction", "version": 1, "transaction_id": folder.name,
            "target": str(target), "data": str(data), "state": "preparing", "direction": direction,
            "candidate_id": manifest["candidate_id"], "had_previous": current is not None,
            "had_database": db.exists(),
            "previous_fingerprint": _tree_fingerprint(target) if current else None,
            "candidate_fingerprint": None, "backup_sha256": None, "data_modified": False}
        _save(folder, journal)
        atomic_write_json(state / "pending.json", {"transaction_id": folder.name})
        try:
            baseline = _data_fingerprint(data)
            shutil.copytree(candidate, folder / "stage")
            staged = verify_candidate(folder / "stage", trusted_keys)
            if staged["candidate_id"] != manifest["candidate_id"]:
                raise ReleaseError("候选在准备期间变化，拒绝替换")
            # Do not commit a journal while payload bytes are only in buffered IO.
            for row in _files(folder / "stage"):
                with contained(folder / "stage", row["path"]).open("r+b") as stream:
                    os.fsync(stream.fileno())
            journal["candidate_fingerprint"] = _tree_fingerprint(folder / "stage")
            health_stage = folder / "health-data"
            if db.exists():
                from .data_backup import create_backup, verify_archive
                backup = create_backup(data, destination=folder / "data.sfbak")
                journal["backup_sha256"] = backup["sha256"]
                verify_archive(folder / "data.sfbak", extract_to=health_stage)
            else:
                health_stage.mkdir()
            _save(folder, journal, "ready")
            _checkpoint("ready")
            if _data_fingerprint(data) != baseline:
                raise ReleaseError("数据在升级准备期间变化，拒绝替换")
            _check_reserved_database(db, database_records)
            atomic_write_json(barrier_path(data), _barrier_value(folder, journal))
            _save(folder, journal, "moving_previous")
            if current:
                if _tree_fingerprint(target) != journal["previous_fingerprint"]:
                    raise ReleaseError("原程序在升级准备期间变化，拒绝替换")
                target.rename(folder / "previous")
            _checkpoint("previous_moved")
            _save(folder, journal, "activating")
            (folder / "stage").rename(target)
            _checkpoint("candidate_activated")
            _save(folder, journal, "checking_health")
            activated = verify_candidate(target, trusted_keys)
            if (activated["candidate_id"] != manifest["candidate_id"]
                    or _tree_fingerprint(target) != journal["candidate_fingerprint"]):
                raise ReleaseError("程序在激活期间变化，拒绝执行健康检查")
            _health(target, manifest, health_stage, folder / "health-receipt.json")
            _checkpoint("before_commit")
            verify_candidate(target, trusted_keys)
            if _tree_fingerprint(target) != journal["candidate_fingerprint"] or _data_fingerprint(data) != baseline:
                raise ReleaseError("程序或数据在健康检查期间变化，拒绝提交")
            _check_reserved_database(db, database_records)
            _save(folder, journal, "committed")
            _checkpoint("committed")
            _clear_barrier(data, folder, journal)
            return {"state": "committed", "transaction_id": folder.name, "candidate_id": manifest["candidate_id"],
                "data_modified": False, "backup_retained": bool(journal["backup_sha256"]),
                "health_scope": "disposable_database_only", "public_release_ready": False}
        except Exception:
            # Unknown filesystem changes fail closed; recovery never deletes them.
            _recover_locked(target, data, state, trusted_keys)
            raise
