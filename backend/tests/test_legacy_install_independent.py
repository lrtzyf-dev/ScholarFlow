"""Independent legacy-boundary checks on disposable installs and real SQLite."""
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from app import legacy_install as module
from app.release_info import ReleaseError
from .test_release_install import setup
from .test_legacy_install import ROOT, legacy, observe, prepare, files


def test_readonly_inspection_of_closed_wal_database_does_not_create_input_files(legacy):
    s = legacy
    with closing(sqlite3.connect(s["data"] / "scholarflow.db")) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    before_data, before_program = files(s["data"]), files(s["legacy"])
    assert "scholarflow.db-wal" not in before_data and "scholarflow.db-shm" not in before_data
    value = observe(s)
    assert value["read_only"] is True
    assert files(s["data"]) == before_data
    assert files(s["legacy"]) == before_program
    assert not s["destination"].exists() and not s["new_target"].exists()


def test_wal_committed_rows_are_not_lost_by_readonly_preparation_or_credential_export(legacy, tmp_path):
    s = legacy
    with closing(sqlite3.connect(s["data"] / "scholarflow.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO samples VALUES ('committed-in-wal')")
        writer.commit()
        assert (s["data"] / "scholarflow.db-wal").stat().st_size > 0
        result = prepare(s)
        assert result["state"] == "prepared" and result["install_allowed"] is False
        assert result["legacy_process_shutdown_verified"] is False
        with zipfile.ZipFile(s["destination"] / "data.sfbak") as archive:
            portable = tmp_path / "portable-database.sqlite3"
            portable.write_bytes(archive.read("data/scholarflow.db"))
            assert b"synthetic-never-export" not in archive.read("data/config.json")
        with closing(sqlite3.connect(portable)) as archived:
            assert archived.execute("SELECT value FROM samples ORDER BY rowid").fetchall() == [
                ("original",), ("committed-in-wal",)]
        assert writer.execute("SELECT value FROM samples ORDER BY rowid").fetchall() == [
            ("original",), ("committed-in-wal",)]
    assert not s["new_target"].exists()


@pytest.mark.parametrize("inside", ["legacy", "data", "candidate", "recovery"])
def test_proposed_target_cannot_live_inside_any_retained_or_candidate_tree(legacy, inside):
    s = legacy
    if inside == "recovery":
        parent = module.data_backup.recovery_root(s["data"])
        parent.mkdir()
    else:
        parent = s[inside]
    s["new_target"] = parent / "external-looking-name"
    before_data, before_program = files(s["data"]), files(s["legacy"])
    with pytest.raises(ReleaseError):
        observe(s)
    assert files(s["data"]) == before_data and files(s["legacy"]) == before_program
    assert not s["new_target"].exists()


def test_legacy_cli_rejects_a_trust_store_inside_user_data_before_preparation(legacy):
    s = legacy
    trust = s["data"] / "untrusted-release-keys.json"
    trust.write_text(json.dumps(s["trust"]), encoding="utf-8")
    command = [sys.executable, str(ROOT / "desktop/install_tool.py"), "inspect-legacy", str(s["candidate"]),
        "--legacy", str(s["legacy"]), "--data", str(s["data"]), "--target", str(s["new_target"]),
        "--trusted-keys", str(trust)]
    before_data, before_program = files(s["data"]), files(s["legacy"])
    result = subprocess.run(command, capture_output=True, timeout=30, text=True, encoding="utf-8",
                            env={**os.environ, "PYTHONUTF8": "1"})
    assert result.returncode == 2
    assert json.loads(result.stderr)["ok"] is False
    assert files(s["data"]) == before_data and files(s["legacy"]) == before_program
    assert not s["destination"].exists() and not s["new_target"].exists()


def test_observation_rejects_external_wal_commit_during_temporary_snapshot_read(legacy, monkeypatch):
    s = legacy
    original = module.schema_snapshot
    with closing(sqlite3.connect(s["data"] / "scholarflow.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO samples VALUES ('before-observation')"); writer.commit()
        changed = []
        def change_source_after_schema(snapshot):
            schema = original(snapshot)
            if not changed:
                writer.execute("INSERT INTO samples VALUES ('external-during-observation')"); writer.commit()
                changed.append(True)
            return schema
        monkeypatch.setattr(module, "schema_snapshot", change_source_after_schema)
        with pytest.raises(ReleaseError, match="WAL.*变化|混合版本"):
            observe(s)
        assert changed
        assert writer.execute("SELECT value FROM samples ORDER BY rowid").fetchall()[-1] == ("external-during-observation",)
    assert not s["destination"].exists() and not s["new_target"].exists()


def test_unresolved_rollback_journal_is_refused_without_opening_original_sqlite(legacy):
    s = legacy
    (s["data"] / "scholarflow.db-journal").write_bytes(b"Unresolved legacy recovery data; do not delete")
    before_data, before_program = files(s["data"]), files(s["legacy"])
    with pytest.raises(ReleaseError, match="恢复日志"):
        observe(s)
    assert files(s["data"]) == before_data and files(s["legacy"]) == before_program
    assert not s["destination"].exists() and not s["new_target"].exists()
