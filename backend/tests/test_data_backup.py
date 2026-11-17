from contextlib import closing
import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app import data_backup as backup


def make_data(root, text="original"):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "scholarflow.db")
    db.executescript('''CREATE TABLE example (text TEXT); CREATE TABLE subscriptions(active INTEGER);
      CREATE TABLE run_inputs(status TEXT, reason TEXT);
      CREATE TABLE conversation_options(approval_mode TEXT,revision INTEGER);
      CREATE TABLE papers(id TEXT,pdf_path TEXT,parsed_path TEXT);''')
    db.execute("INSERT INTO example VALUES (?)", (text,))
    db.execute("INSERT INTO subscriptions VALUES (1)")
    db.execute("INSERT INTO run_inputs VALUES ('queued','')")
    db.execute("INSERT INTO conversation_options VALUES ('full_access',4)")
    db.execute("INSERT INTO papers VALUES ('p',?,'')", (str(root / "pdf" / "sample.pdf"),))
    db.commit()
    db.close()
    for name, value in {"pdf/sample.pdf": b"sample", "parsed/sample.json": b'{}',
                        "vectors.npz": b"vectors", "notes/note.md": text.encode(),
                        "config.json": b'{"approval_mode":"full_access"}',
                        "hooks.json": b'{"Stop":[{"hooks":[{"command":"echo unexpected"}]}]}',
                        "mcp_servers.json": b'{"servers":[{"name":"external","enabled":true}]}',
                        ".runtime.json": b'{"stale":true}'}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return root


def read_value(root):
    with closing(sqlite3.connect(root / "scholarflow.db")) as db:
        return db.execute("SELECT text FROM example").fetchone()[0]


def test_full_backup_wal_and_all_files(tmp_path):
    root = make_data(tmp_path / "data")
    with closing(sqlite3.connect(root / "scholarflow.db")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE example SET text='committed in WAL'")
        db.commit()
        result = backup.create_backup(root)
        manifest = backup.verify_archive(result["path"], extract_to=tmp_path / "unpacked")
    assert read_value(tmp_path / "unpacked") == "committed in WAL"
    assert {e["path"] for e in manifest["files"]} >= {"notes/note.md", "vectors.npz", "pdf/sample.pdf", "config.json"}
    assert ".runtime.json" not in {e["path"] for e in manifest["files"]}
    assert not manifest["external_project_files_included"]
    assert result["sha256"] == hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest()


def test_restore_preview_restart_and_rollback(tmp_path):
    source = make_data(tmp_path / "source", "backup data")
    target = make_data(tmp_path / "target", "current data")
    exported = backup.create_backup(source)
    preview = backup.prepare_restore(target, exported["path"])
    assert read_value(target) == "current data"  # preview is inert
    backup.schedule_restore(target, preview["id"])
    result = backup.apply_pending_restore(target)
    assert result["previous_data_retained"]
    assert read_value(target) == "backup data"
    assert json.loads((target / "hooks.json").read_text()) == {}
    assert json.loads((target / "mcp_servers.json").read_text())["servers"][0]["enabled"] is False
    with closing(sqlite3.connect(target / "scholarflow.db")) as db:
        assert db.execute("SELECT active FROM subscriptions").fetchone() == (0,)
        assert db.execute("SELECT status FROM run_inputs").fetchone() == ("cancelled",)
        assert db.execute("SELECT approval_mode,revision FROM conversation_options").fetchone() == ("read_only", 5)
        assert db.execute("SELECT pdf_path FROM papers").fetchone()[0] == str(target / "pdf/sample.pdf")
    undo = backup.prepare_rollback(target)
    backup.schedule_restore(target, undo["id"])
    backup.apply_pending_restore(target)
    assert read_value(target) == "current data"
    assert (target / "notes/note.md").read_text() == "current data"


def test_restored_dispatch_intents_are_dormant_and_history_is_retained(tmp_path):
    source = make_data(tmp_path / "source")
    target = make_data(tmp_path / "target")
    with closing(sqlite3.connect(source / "scholarflow.db")) as db:
        db.executemany("INSERT INTO run_inputs VALUES (?,?)", [(state, state) for state in (
            "dispatching", "dispatched", "applied", "cancelled", "failed")])
        db.commit()
    exported = backup.create_backup(source)
    preview = backup.prepare_restore(target, exported["path"])
    backup.schedule_restore(target, preview["id"])
    backup.apply_pending_restore(target)
    with closing(sqlite3.connect(target / "scholarflow.db")) as db:
        rows = db.execute("SELECT status,reason FROM run_inputs").fetchall()
    assert len(rows) == 6
    assert all(status == "cancelled" and "未自动执行" in reason for status, reason in rows[:3])
    assert rows[3:] == [("applied", "applied"), ("cancelled", "cancelled"), ("failed", "failed")]


def test_staged_tampering_never_replaces_live_data(tmp_path):
    root = make_data(tmp_path / "data")
    result = backup.create_backup(root)
    preview = backup.prepare_restore(root, result["path"])
    stage = backup.recovery_root(root) / "staged" / preview["id"] / "data"
    (stage / "notes/note.md").write_text("tampered")
    with pytest.raises(backup.BackupError, match="已变化"):
        backup.schedule_restore(root, preview["id"])
    assert read_value(root) == "original"


@pytest.mark.parametrize("bad_path", ["../outside", "/absolute", "C:/escape", "a\\b", "con.txt", "a:stream", "a/../b", "a."])
def test_reject_unsafe_archive_paths(tmp_path, bad_path):
    path = tmp_path / "bad.sfbak"
    body = b"x"
    manifest = {"format": backup.FORMAT, "version": 1, "database": "scholarflow.db", "files": [
        {"path": bad_path, "size": 1, "sha256": hashlib.sha256(body).hexdigest()}]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("data/" + bad_path, body)
    with pytest.raises(backup.BackupError):
        backup.verify_archive(path, extract_to=tmp_path / "extract")
    assert not (tmp_path / "extract").exists()
    assert not (tmp_path / "outside").exists()


def test_reject_hash_corruption_and_duplicate_names(tmp_path):
    root = make_data(tmp_path / "data")
    result = backup.create_backup(root)
    for duplicate in (False, True):
        bad = tmp_path / f"bad-{duplicate}.sfbak"
        with zipfile.ZipFile(result["path"]) as src, zipfile.ZipFile(bad, "w") as dest:
            for entry in src.infolist():
                body = src.read(entry)
                if not duplicate and entry.filename == "data/notes/note.md":
                    body = b"X" * len(body)
                dest.writestr(entry, body)
            if duplicate:
                dest.writestr("DATA/NOTES/NOTE.MD", b"duplicate")
        with pytest.raises(backup.BackupError):
            backup.verify_archive(bad)


def test_cancel_and_conflicting_restore(tmp_path):
    root = make_data(tmp_path / "data")
    exported = backup.create_backup(root)
    a = backup.prepare_restore(root, exported["path"])
    b = backup.prepare_restore(root, exported["path"])
    backup.schedule_restore(root, a["id"])
    assert backup.schedule_restore(root, a["id"])["status"] == "scheduled"
    with pytest.raises(backup.BackupError, match="另一份"):
        backup.schedule_restore(root, b["id"])
    assert backup.cancel_restore(root)["status"] == "cancelled"
    assert backup.apply_pending_restore(root) is None
    assert read_value(root) == "original"


def test_live_runtime_lock_prevents_replacement(tmp_path):
    root = make_data(tmp_path / "data")
    exported = backup.create_backup(root)
    preview = backup.prepare_restore(root, exported["path"])
    backup.schedule_restore(root, preview["id"])
    with backup._runtime_exclusive(root):
        with pytest.raises(backup.BackupError, match="仍在运行"):
            backup.apply_pending_restore(root)
    assert read_value(root) == "original"


@pytest.mark.parametrize("fail_at", list(range(1, 14)))
def test_interrupted_moves_resume_without_losing_previous(tmp_path, monkeypatch, fail_at):
    source = make_data(tmp_path / "source", "restored")
    target = make_data(tmp_path / "target", "previous")
    exported = backup.create_backup(source)
    preview = backup.prepare_restore(target, exported["path"])
    backup.schedule_restore(target, preview["id"])
    real = os.replace
    count = 0
    def interrupted(src, dst):
        nonlocal count
        real(src, dst)
        if str(dst).endswith("pending.json") or Path(src).parent == target or Path(dst).parent == target:
            count += 1
            if count == fail_at:
                raise OSError("simulated process interruption after atomic move")
    monkeypatch.setattr(backup.os, "replace", interrupted)
    try:
        backup.apply_pending_restore(target)
    except OSError:
        pass
    monkeypatch.setattr(backup.os, "replace", real)
    backup.apply_pending_restore(target)
    assert read_value(target) == "restored"
    previous = backup.recovery_root(target) / "staged" / preview["id"] / "previous"
    assert read_value(previous) == "previous"


def test_disk_failure_does_not_publish_incomplete_backup(tmp_path, monkeypatch):
    root = make_data(tmp_path / "data")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(backup, "_database_snapshot", fail)
    with pytest.raises(OSError):
        backup.create_backup(root)
    assert not list((backup.recovery_root(root) / "exports").glob("*.sfbak"))
    assert read_value(root) == "original"


@pytest.mark.parametrize("name", [".RUNTIME.LOCK", ".Runtime.Json", ".RUNTIME-OWNER", "BACKUPS/injected", "SCHOLARFLOW.DB-WAL"])
def test_archive_rejects_case_variants_of_reserved_runtime_paths(tmp_path, name):
    root = make_data(tmp_path / "data")
    exported = backup.create_backup(root)
    path = tmp_path / "collision.sfbak"
    with zipfile.ZipFile(exported["path"]) as src, zipfile.ZipFile(path, "w") as dest:
        manifest = json.loads(src.read("manifest.json"))
        body = b"injected"
        manifest["files"].append({"path": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        for entry in src.infolist():
            if entry.filename != "manifest.json":
                dest.writestr(entry, src.read(entry))
        dest.writestr("manifest.json", json.dumps(manifest))
        dest.writestr("data/" + name, body)
    with pytest.raises(backup.BackupError, match="禁止恢复"):
        backup.prepare_restore(root, path)
    assert read_value(root) == "original"


def test_injected_stage_runtime_file_is_refused_before_moving_old_data(tmp_path):
    root = make_data(tmp_path / "data")
    exported = backup.create_backup(root)
    preview = backup.prepare_restore(root, exported["path"])
    stage = backup.recovery_root(root) / "staged" / preview["id"] / "data"
    (stage / ".RUNTIME.LOCK").write_bytes(b"conflict")
    with pytest.raises(backup.BackupError, match="禁止恢复"):
        backup.schedule_restore(root, preview["id"])
    assert read_value(root) == "original"


def test_database_name_mismatch_refused_before_replacing_data(tmp_path):
    root = make_data(tmp_path / "data")
    exported = backup.create_backup(root)
    with pytest.raises(backup.BackupError, match="数据库名称"):
        backup.prepare_restore(root, exported["path"], expected_database="different.db")
    preview = backup.prepare_restore(root, exported["path"], expected_database="scholarflow.db")
    backup.schedule_restore(root, preview["id"])
    with pytest.raises(backup.BackupError, match="启动配置"):
        backup.apply_pending_restore(root, expected_database="different.db")
    assert read_value(root) == "original"


def test_external_project_path_and_source_provenance_untouched(tmp_path):
    source = make_data(tmp_path / "source")
    external = tmp_path / "outside.pdf"
    external.write_bytes(b"private")
    with closing(sqlite3.connect(source / "scholarflow.db")) as db:
        db.execute("INSERT INTO papers VALUES ('external',?,'')", (str(external),))
        db.commit()
    exported = backup.create_backup(source)
    target = tmp_path / "target"
    preview = backup.prepare_restore(target, exported["path"])
    backup.schedule_restore(target, preview["id"])
    backup.apply_pending_restore(target)
    with closing(sqlite3.connect(target / "scholarflow.db")) as db:
        assert db.execute("SELECT pdf_path FROM papers WHERE id='external'").fetchone()[0] == str(external)
    assert external.read_bytes() == b"private"
