"""Recoverable, separate-directory import from a rechecked legacy preparation.

Only a statically reviewed exact-schema transform is allowed. Neither the old
installation/data nor the proposed program target is replaced. Candidate EXEs
are verified, never executed. A journal is evidence, not migration authority:
recovery rebuilds expected output from the rechecked archive and built-in code.
"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import uuid4
import zipfile

from . import data_backup, legacy_migrations as migrations
from .atomic_files import atomic_write_json
from .legacy_install import BASE_BLOCKERS, verify_preparation
from .release_database import database_reservation
from .release_files import digest, no_link
from .release_info import ReleaseError, read_json
from .release_install import _exclusive, _files, verify_candidate
from .release_schema import schema_snapshot


FORMAT = "scholarflow-separate-legacy-import"
MARKER = ".legacy-import.json"


def _path(value):
    return no_link(no_link(value).resolve())


def _separate(a, b):
    return a != b and not a.is_relative_to(b) and not b.is_relative_to(a)


def _roots(preparation, legacy, data, target, output):
    preparation, legacy, data, target, output = map(_path, (preparation, legacy, data, target, output))
    state = _path(output.parent / ("." + output.name + "-imports"))
    originals = (preparation, legacy, data, target, _path(data_backup.recovery_root(data)),
                 _path(target.parent / ("." + target.name + "-updates")))
    if (output == output.parent or not output.parent.is_dir() or not _separate(output, state)
            or any(not _separate(item, other) for item in (output, state) for other in originals)):
        raise ReleaseError("分离导入的新数据及事务目录必须在旧程序、旧数据、准备归档和新程序目标之外")
    return preparation, legacy, data, target, output, state


def _review(preparation, legacy, data, target, output, *, trusted_keys, migration_id,
            expected_preparation_sha256, allow_output=False):
    preparation, legacy, data, target, output, state = _roots(preparation, legacy, data, target, output)
    if output.exists() and not allow_output:
        raise ReleaseError("分离导入目标必须尚不存在；已有结果请用显式恢复复核，不覆盖目录")
    if not isinstance(expected_preparation_sha256, str) or not re.fullmatch("[a-f0-9]{64}", expected_preparation_sha256):
        raise ReleaseError("必须提供此前审阅的准备记录指纹")
    verified = verify_preparation(preparation, legacy, data, target, trusted_keys=trusted_keys)
    if {"runtime_owner_active_or_unavailable", "active_or_queued_tasks", "data_recovery_pending"}.intersection(verified["blockers"]):
        raise ReleaseError("原数据仍被运行实例占用、含活动任务或待恢复事务；未开始分离导入")
    receipt = read_json(no_link(preparation / "preparation.json"), 256 * 1024)
    if receipt["preparation_sha256"] != expected_preparation_sha256:
        raise ReleaseError("准备记录与明确审阅的指纹不符")
    candidate = verify_candidate(preparation / "candidate", trusted_keys)
    item = migrations.resolve(migration_id, receipt["observation"]["current_schema_sha256"],
                              candidate["target_schema"]["sha256"], candidate["definition"]["data_contract"])
    review = {"format": FORMAT + "-review", "version": 1,
        "preparation": str(preparation), "legacy": str(legacy), "data": str(data),
        "program_target": str(target), "new_data": str(output),
        "preparation_sha256": expected_preparation_sha256, "backup_sha256": receipt["backup_sha256"],
        "candidate_id": candidate["candidate_id"], "migration": item,
        "legacy_blockers": verified["blockers"], "original_program_retained": True,
        "original_data_retained": True, "program_installation_performed": False,
        "old_checkpoint_resume_allowed": False, "credential_reconfiguration_required": True,
        "cross_schema_migration_performed": False, "public_release_ready": False}
    if item["structure_changed"]:
        review["structure_change_planned"] = True
    return {**review, "review_sha256": digest(review)}


def review_import(preparation, legacy, data, target, output, *, trusted_keys, migration_id,
                  expected_preparation_sha256):
    """Read-only review; does not create state/output or load legacy code/config."""
    return _review(preparation, legacy, data, target, output, trusted_keys=trusted_keys,
                   migration_id=migration_id, expected_preparation_sha256=expected_preparation_sha256)


def _checkpoint(name):
    """Private fault-injection boundary, never selected by a CLI or archive."""


def _save(folder, journal, state):
    journal["state"] = state
    atomic_write_json(folder / "journal.json", journal)


def _build(stage, preparation, output, review, transaction_id):
    stage = no_link(stage)
    try:
        with zipfile.ZipFile(no_link(preparation / "data.sfbak")) as archive:
            info = archive.infolist()
            total = sum(item.file_size for item in info)
            if len(info) > data_backup.MAX_FILES + 1 or total > data_backup.MAX_TOTAL_BYTES + data_backup.MAX_MANIFEST_BYTES:
                raise ReleaseError("分离导入归档超过可恢复大小限制")
    except (OSError, zipfile.BadZipFile):
        raise ReleaseError("分离导入归档已无法读取；保留旧目录与恢复记录") from None
    # Include a complete extracted copy, SQLite's VACUUM workspace and a
    # separate reconstructed recovery copy. Low-space failure retains old data.
    if shutil.disk_usage(stage.parent).free < 3 * total + 16 * 1024 * 1024:
        raise ReleaseError("磁盘空间不足，未发布分离导入目录")
    manifest = data_backup.verify_archive(preparation / "data.sfbak", extract_to=stage)
    if (manifest.get("credential_portability") != data_backup.credentials.EXCLUDED_RECONFIGURE
            or manifest.get("database") != "scholarflow.db"):
        raise ReleaseError("分离导入只接受已验证排除凭证的明确数据库归档")
    database = stage / "scholarflow.db"
    if schema_snapshot(database)["sha256"] != review["migration"]["source_schema_sha256"]:
        raise ReleaseError("归档来源结构与已审核项不符")
    with closing(sqlite3.connect(database)) as connection:
        before = migrations.immutable_rows(connection, review["migration"]["id"])
    migrations.prepare_structure(database, review["migration"]["id"])
    prior_hooks = set(stage.glob("hooks.restore-review-*.json"))
    adjustments = data_backup._suspend_restored_automation(stage, manifest, output)
    # Restore's review filename is intentionally random. Give only the new inert
    # copy a deterministic transaction-bound name so recovery can rebuild bytes.
    created_hooks = set(stage.glob("hooks.restore-review-*.json")) - prior_hooks
    if created_hooks:
        destination = stage / ("legacy-import-hooks-review-" + transaction_id + ".json")
        if len(created_hooks) != 1 or destination.exists():
            raise ReleaseError("旧hooks的只读审阅副本与分离导入目标冲突")
        next(iter(created_hooks)).rename(destination)
    migrations.transform(database, review["migration"]["id"])
    cross_evidence = migrations.finish_cross_schema(database, review["migration"]["id"],
        stage=stage, output=output, manifest=manifest, transaction_id=transaction_id)
    if cross_evidence is not None:
        report = stage / "legacy-schema-migration.json"
        if report.exists():
            raise ReleaseError("旧归档包含迁移报告同名文件，未覆盖历史资产")
        atomic_write_json(report, cross_evidence)
        if not (stage / "config.json").exists():
            atomic_write_json(stage / "config.json", {"approval_mode":"read_only",
                "llm_api_key":"", "embedding_api_key":"", "legacy_credential_generation":1})
    with closing(sqlite3.connect(database)) as connection:
        after = migrations.immutable_rows(connection, review["migration"]["id"])
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        data_backup._verify_suspended_database(connection, tables)
    migrations.verify_dormant(database)
    if before != after or schema_snapshot(database)["sha256"] != review["migration"]["target_schema_sha256"]:
        raise ReleaseError("分离导入改变了未授权的历史行、原文字段或数据库结构")
    if (stage / MARKER).exists():
        raise ReleaseError("归档已有分离导入身份，不能静默覆盖来源记录")
    atomic_write_json(stage / MARKER, {"format": FORMAT, "version": 1, "transaction_id": transaction_id,
        "review_sha256": review["review_sha256"], "preparation_sha256": review["preparation_sha256"],
        "candidate_id": review["candidate_id"], "backup_sha256": review["backup_sha256"],
        "migration": review["migration"], "old_checkpoints": "retained_archive_only_never_resumed"})
    files = _files(stage)
    data_backup.credentials.validate_excluded_backup(stage, "scholarflow.db", [row["path"] for row in files])
    for row in files:
        with (stage / row["path"]).open("r+b") as stream:
            os.fsync(stream.fileno())
    return {"files_sha256": digest(files), "tables": {table: value["rows"] for table, value in after.items()},
            "retained_rows_sha256": digest(after), "restore_adjustments": adjustments}


def _same_review(old, current):
    if old != current:
        raise ReleaseError("旧程序、旧数据、准备归档、候选或审核项已经变化；保留结果供复核，不继续发布新目录")


def _result(journal):
    return {"state": journal["state"], "transaction_id": journal["id"], "new_data": journal["review"]["new_data"],
        "program_installation_performed": False, "candidate_executed": False,
        "original_program_retained": True, "original_data_retained": True,
        "cross_schema_migration_performed": journal["review"]["migration"]["structure_changed"], "old_checkpoint_resume_allowed": False,
        "legacy_blockers": journal["review"]["legacy_blockers"], "public_release_ready": False}


def _publish(folder, journal, stage, output, evidence, recheck):
    _save(folder, journal, "ready")
    _checkpoint("ready")
    if digest(_files(stage)) != evidence["files_sha256"] or output.exists():
        raise ReleaseError("发布前分离副本已变化或目标已存在；未覆盖目录")
    journal["evidence"] = evidence
    _save(folder, journal, "publishing")
    _checkpoint("before_publish")
    recheck()
    if digest(_files(stage)) != evidence["files_sha256"] or output.exists():
        raise ReleaseError("分离副本或新目标在发布边界变化，未覆盖目录")
    stage.rename(output)
    _checkpoint("published")
    recheck()
    if digest(_files(output)) != evidence["files_sha256"]:
        raise ReleaseError("发布后的新目录发生变化，保留目录与日志供恢复复核")
    _save(folder, journal, "committed")
    _checkpoint("committed")
    return _result(journal)


def import_data(preparation, legacy, data, target, output, *, trusted_keys, migration_id,
                expected_preparation_sha256, expected_review_sha256):
    review = review_import(preparation, legacy, data, target, output, trusted_keys=trusted_keys,
        migration_id=migration_id, expected_preparation_sha256=expected_preparation_sha256)
    if review["review_sha256"] != expected_review_sha256:
        raise ReleaseError("分离导入计划与明确审阅的指纹不符")
    preparation, legacy, data, target, output, state = _roots(preparation, legacy, data, target, output)
    state.mkdir(exist_ok=True)
    with _exclusive(state / "import.lock"), database_reservation(data / "scholarflow.db"):
        if (state / "pending.json").exists():
            raise ReleaseError("已有分离导入事务，请显式恢复并审阅，不覆盖原事务")
        _same_review(review, review_import(preparation, legacy, data, target, output, trusted_keys=trusted_keys,
            migration_id=migration_id, expected_preparation_sha256=expected_preparation_sha256))
        folder = state / uuid4().hex
        folder.mkdir()
        journal = {"format": FORMAT, "version": 1, "id": folder.name, "state": "preparing", "review": review}
        _save(folder, journal, "preparing")
        atomic_write_json(state / "pending.json", {"transaction_id": folder.name})
        try:
            _checkpoint("preparing")
            evidence = _build(folder / "stage", preparation, output, review, folder.name)
            _checkpoint("staged")
            _same_review(review, review_import(preparation, legacy, data, target, output, trusted_keys=trusted_keys,
                migration_id=migration_id, expected_preparation_sha256=expected_preparation_sha256))
            def recheck():
                _same_review(review, _review(preparation, legacy, data, target, output, trusted_keys=trusted_keys,
                    migration_id=migration_id, expected_preparation_sha256=expected_preparation_sha256, allow_output=True))
            return _publish(folder, journal, folder / "stage", output, evidence, recheck)
        except Exception:
            _save(folder, journal, "interrupted")
            raise


def recover_import(preparation, legacy, data, target, output, *, trusted_keys, migration_id,
                   expected_preparation_sha256, expected_review_sha256):
    preparation, legacy, data, target, output, state = _roots(preparation, legacy, data, target, output)
    if not state.is_dir():
        raise ReleaseError("没有此新数据目录的分离导入事务")
    with _exclusive(state / "import.lock"), database_reservation(data / "scholarflow.db"):
        pending = read_json(no_link(state / "pending.json"), 4096)
        if (not isinstance(pending, dict) or set(pending) != {"transaction_id"}
                or not isinstance(pending["transaction_id"], str) or not re.fullmatch("[a-f0-9]{32}", pending["transaction_id"])):
            raise ReleaseError("分离导入事务指针无效；未改动任何数据目录")
        folder = no_link(state / pending["transaction_id"])
        journal = read_json(no_link(folder / "journal.json"), 512 * 1024)
        if (not isinstance(journal, dict) or set(journal) - {"format", "version", "id", "state", "review", "evidence"}
                or journal.get("format") != FORMAT or type(journal.get("version")) is not int or journal["version"] != 1
                or journal.get("id") != folder.name or journal.get("state") not in {"preparing", "interrupted", "ready", "publishing", "committed"}):
            raise ReleaseError("分离导入日志无效；不会据此执行任意路径或迁移")
        review = _review(preparation, legacy, data, target, output, trusted_keys=trusted_keys, migration_id=migration_id,
            expected_preparation_sha256=expected_preparation_sha256, allow_output=True)
        if review["review_sha256"] != expected_review_sha256:
            raise ReleaseError("恢复所需的明确审阅指纹不符")
        _same_review(journal.get("review"), review)
        # Never trust a rehashed journal/body alone. Recreate output from the
        # verified source archive and exact registered transform in a new slot.
        rebuilt = no_link(folder / ("recovery-" + uuid4().hex))
        evidence = _build(rebuilt, preparation, output, review, folder.name)
        _same_review(review, _review(preparation, legacy, data, target, output, trusted_keys=trusted_keys,
            migration_id=migration_id, expected_preparation_sha256=expected_preparation_sha256, allow_output=True))
        if output.exists():
            if not output.is_dir() or digest(_files(output)) != evidence["files_sha256"]:
                raise ReleaseError("已有新数据与独立重建结果不符；保留所有目录，不删除或覆盖")
            journal["evidence"] = evidence
            _save(folder, journal, "committed")
            return _result(journal)
        def recheck():
            _same_review(review, _review(preparation, legacy, data, target, output, trusted_keys=trusted_keys,
                migration_id=migration_id, expected_preparation_sha256=expected_preparation_sha256, allow_output=True))
        return _publish(folder, journal, rebuilt, output, evidence, recheck)
