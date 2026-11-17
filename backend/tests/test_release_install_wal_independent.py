"""Independent raw-byte checks of signed install/recovery with a real exited WAL.

All programs, keys and data are synthetic in pytest temporary directories. The
health callback is the existing test surrogate, never a user or packaged EXE.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from app import release_install as module
from app import release_database as database_io
from app.release_info import ReleaseError
from .test_release_install import PowerLoss, setup
from .test_release_engineering import prepared
from .test_release_install_independent import kill_probe


EXITED_WAL = """import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute("INSERT INTO samples VALUES ('committed-only-in-real-exited-wal')")
c.commit();os._exit(0)
"""


def crashed_wal(s):
    database = s["data"] / "scholarflow.db"
    result = subprocess.run([sys.executable, "-c", EXITED_WAL, str(database)],
                            capture_output=True, timeout=20)
    assert result.returncode == 0
    wal = database.with_name(database.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 32
    return database


def originals(database):
    return {database.name + suffix: database.with_name(database.name + suffix).read_bytes()
            for suffix in ("", "-wal")}


def assert_originals(database, expected):
    assert originals(database) == expected


def install(s):
    return module.install(s["candidate"], s["target"], s["data"], trusted_keys=s["trust"])


def recover(s):
    return module.recover(s["target"], s["data"], trusted_keys=s["trust"])


def test_real_exited_wal_signed_install_retains_main_and_wal_bytes_and_backup_rows(setup, tmp_path):
    s = setup
    database = crashed_wal(s)
    before = originals(database)
    result = install(s)
    assert result["state"] == "committed" and result["data_modified"] is False
    assert_originals(database, before)
    folder = s["target"].parent / ".installed-updates" / result["transaction_id"]
    from app.data_backup import verify_archive
    extracted = tmp_path / "independently-checked-backup"
    verify_archive(folder / "data.sfbak", extract_to=extracted)
    with closing(sqlite3.connect(extracted / "scholarflow.db")) as connection:
        rows = connection.execute("SELECT value FROM samples ORDER BY rowid").fetchall()
    assert rows == [("original",), ("committed-only-in-real-exited-wal",)]
    assert "synthetic-never-export" not in (extracted / "config.json").read_text(encoding="utf-8")
    assert_originals(database, before)
    assert recover(s)["state"] == "committed"
    assert_originals(database, before)


def test_external_observation_reads_committed_wal_without_recreating_source_shm(setup):
    database = crashed_wal(setup)
    # This is a closed synthetic fixture, not a live user's SQLite index.
    database.with_name(database.name + "-shm").unlink(missing_ok=True)
    before = originals(database)
    with module._observation_database(database) as snapshot:
        assert snapshot.parent != database.parent
        with closing(sqlite3.connect(snapshot)) as connection:
            assert connection.execute("SELECT value FROM samples ORDER BY rowid").fetchall() == [
                ("original",), ("committed-only-in-real-exited-wal",)]
    assert_originals(database, before)
    assert not database.with_name(database.name + "-shm").exists()


def test_install_and_recovery_schema_queries_never_open_the_original_for_observation(setup, monkeypatch):
    s = setup
    database = crashed_wal(s)
    before = originals(database)
    real_schema, real_pending = module.schema_snapshot, module.pending_runs
    seen = []

    def schema(path):
        assert Path(path) != database
        seen.append(("schema", Path(path)))
        return real_schema(path)

    def pending(path):
        assert Path(path) != database
        seen.append(("pending", Path(path)))
        return real_pending(path)

    def interrupt(name):
        if name == "candidate_activated":
            raise PowerLoss()

    monkeypatch.setattr(module, "schema_snapshot", schema)
    monkeypatch.setattr(module, "pending_runs", pending)
    monkeypatch.setattr(module, "_checkpoint", interrupt)
    with pytest.raises(PowerLoss):
        install(s)
    assert_originals(database, before)
    assert recover(s)["state"] == "rolled_back"
    assert {kind for kind, _ in seen} == {"schema", "pending"}
    assert_originals(database, before)


@pytest.mark.parametrize("operation", ["install", "recover"])
def test_missing_checkpoint_protection_refuses_before_any_source_connection(setup, monkeypatch, operation):
    s = setup
    database = crashed_wal(s)
    if operation == "recover":
        def interrupt(name):
            if name == "candidate_activated":
                raise PowerLoss()
        monkeypatch.setattr(module, "_checkpoint", interrupt)
        with pytest.raises(PowerLoss):
            install(s)
    before, program = originals(database), module._tree_fingerprint(s["target"])
    monkeypatch.delattr(database_io.sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE")
    original_connect = database_io.sqlite3.connect

    def no_source_connect(path, *args, **kwargs):
        assert not str(path).startswith(database.as_uri())
        assert Path(str(path)) != database
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(database_io.sqlite3, "connect", no_source_connect)
    with pytest.raises(ReleaseError, match="保留原WAL"):
        install(s) if operation == "install" else recover(s)
    assert_originals(database, before)
    assert module._tree_fingerprint(s["target"]) == program


def test_checkpoint_option_is_enabled_and_verified_before_first_source_sql(setup, monkeypatch):
    s = setup
    database = crashed_wal(s)
    before = originals(database)
    original_connect = sqlite3.connect
    source_sql = []
    option = sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE

    class ProtectedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            assert self.getconfig(option) is True
            source_sql.append(sql)
            return super().execute(sql, *args, **kwargs)

    def connect(path, *args, **kwargs):
        if str(path) == database.as_uri() + "?mode=rw":
            kwargs["factory"] = ProtectedConnection
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(database_io.sqlite3, "connect", connect)
    assert install(s)["state"] == "committed"
    assert source_sql == ["PRAGMA trusted_schema=OFF", "BEGIN IMMEDIATE"]
    assert_originals(database, before)


@pytest.mark.parametrize("failure", ["schema", "pending", "health", "space"])
def test_rejected_signed_install_retains_exited_wal_and_old_program(setup, monkeypatch, failure):
    s = setup
    database = crashed_wal(s)
    before, program = originals(database), module._tree_fingerprint(s["target"])
    if failure == "schema":
        original_schema = module.schema_snapshot
        monkeypatch.setattr(module, "schema_snapshot", lambda path: {**original_schema(path), "sha256": "0" * 64})
    elif failure == "pending":
        monkeypatch.setattr(module, "pending_runs", lambda path: {"runtime_runs": {"running": 1}})
    elif failure == "space":
        usage = module.shutil.disk_usage(s["target"].parent)
        monkeypatch.setattr(module.shutil, "disk_usage", lambda path: type(usage)(usage.total, usage.total, 0))
    else:
        def fail(*args):
            raise ReleaseError("synthetic health failure")
        monkeypatch.setattr(module, "_health", fail)
    with pytest.raises(ReleaseError):
        install(s)
    assert_originals(database, before)
    assert module._tree_fingerprint(s["target"]) == program


def test_active_sqlite_writer_is_not_bypassed_by_copy_preflight(setup):
    s = setup
    database = crashed_wal(s)
    program = module._tree_fingerprint(s["target"])
    option = sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE
    with closing(sqlite3.connect(database)) as writer:
        writer.setconfig(option, True)
        writer.execute("BEGIN IMMEDIATE")
        before = originals(database)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            install(s)
        writer.rollback()
    assert_originals(database, before)
    assert module._tree_fingerprint(s["target"]) == program


@pytest.mark.parametrize("operation", ["install", "recover", "observe"])
def test_unresolved_rollback_journal_refuses_before_source_sql(setup, monkeypatch, operation):
    s = setup
    database = crashed_wal(s)
    if operation == "recover":
        def interrupt(name):
            if name == "candidate_activated":
                raise PowerLoss()
        monkeypatch.setattr(module, "_checkpoint", interrupt)
        with pytest.raises(PowerLoss):
            install(s)
    journal = database.with_name(database.name + "-journal")
    journal.write_bytes(b"synthetic-unresolved-recovery-do-not-touch")
    before, original_journal = originals(database), journal.read_bytes()
    original_connect = database_io.sqlite3.connect

    def no_source(path, *args, **kwargs):
        assert not str(path).startswith(database.as_uri())
        assert Path(str(path)) != database
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(database_io.sqlite3, "connect", no_source)
    with pytest.raises(ReleaseError, match="恢复日志"):
        if operation == "observe":
            with module._observation_database(database):
                pytest.fail("hot journal observation must not be entered")
        elif operation == "install":
            install(s)
        else:
            recover(s)
    assert_originals(database, before)
    assert journal.read_bytes() == original_journal


def test_changing_source_wal_cannot_be_accepted_as_a_consistent_observation(setup):
    database = crashed_wal(setup)
    with pytest.raises(ReleaseError, match="WAL.*变化|混合版本"):
        with module._observation_database(database):
            with closing(sqlite3.connect(database)) as writer:
                writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                writer.execute("INSERT INTO samples VALUES ('new-version-during-observation')")
                writer.commit()


@pytest.mark.parametrize("boundary", ["ready", "previous_moved", "candidate_activated", "before_commit", "committed"])
def test_real_installer_process_exit_and_idempotent_recovery_preserve_existing_wal(setup, tmp_path, boundary):
    s = setup
    database = crashed_wal(s)
    before = originals(database)
    trust = tmp_path / "external-test-publisher.json"
    trust.write_text(json.dumps(s["trust"]), encoding="utf-8")
    # Terminate a real installer interpreter while its actual SQLite reservation
    # and OS locks are held. Synthetic payloads are never executed.
    code = """import json,os,sys
from pathlib import Path
from app import release_install as module
candidate,target,data,trust,boundary=sys.argv[1:]
module._health=lambda *args: None
def checkpoint(name):
 if name==boundary: os._exit(73)
module._checkpoint=checkpoint
module.install(Path(candidate),Path(target),Path(data),trusted_keys=json.loads(Path(trust).read_text()))
raise SystemExit(74)
"""
    env = {**os.environ, "DATA_DIR": str(tmp_path / "child-isolated-config"),
           "LLM_API_KEY": "", "EMBEDDING_API_KEY": "", "LLM_DEMO_MODE": "1"}
    child = subprocess.run([sys.executable, "-c", code, str(s["candidate"]), str(s["target"]),
                            str(s["data"]), str(trust), boundary], cwd=Path(__file__).resolve().parents[1],
                           env=env, capture_output=True, timeout=30)
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    assert_originals(database, before)
    expected = "committed" if boundary == "committed" else "rolled_back"
    assert recover(s)["state"] == expected
    assert_originals(database, before)
    assert recover(s)["state"] == expected
    assert_originals(database, before)


def test_interrupted_recovery_keeps_schema_incompatibility_fence_without_checkpointing(setup, monkeypatch):
    s = setup
    database = crashed_wal(s)

    def interrupt(name):
        if name == "candidate_activated":
            raise PowerLoss()

    monkeypatch.setattr(module, "_checkpoint", interrupt)
    with pytest.raises(PowerLoss):
        install(s)
    code = """import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE new_incompatible_schema(value TEXT)');c.commit();os._exit(0)
"""
    child = subprocess.run([sys.executable, "-c", code, str(database)], capture_output=True, timeout=20)
    assert child.returncode == 0
    before, target = originals(database), module._tree_fingerprint(s["target"])
    with pytest.raises(ReleaseError, match="结构已变化"):
        recover(s)
    assert module.barrier_path(s["data"]).exists()
    assert_originals(database, before)
    assert module._tree_fingerprint(s["target"]) == target


@pytest.mark.parametrize("backup_present", [False, True])
def test_generic_preflight_never_creates_source_wal_or_shm(setup, tmp_path, backup_present):
    s = setup
    database = s["data"] / "scholarflow.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    manifest = module.verify_candidate(s["candidate"], s["trust"])
    backup = None
    if backup_present:
        from app.data_backup import create_backup
        backup = create_backup(s["data"], destination=tmp_path / "preflight.sfbak")["path"]
        # Close a real fixture-owned connection to remove empty WAL/SHM before
        # freezing the actual read-only preflight input.
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("SELECT count(*) FROM samples").fetchone()
    before = {p.name: p.read_bytes() for p in s["data"].iterdir() if p.is_file()}
    assert not any(name.endswith(("-wal", "-shm")) for name in before)
    from app.release_schema import compatibility_preflight
    result = compatibility_preflight(database, manifest["target_schema"], manifest["definition"]["data_contract"],
                                     current_contract=manifest["definition"]["data_contract"], backup=backup)
    assert result["installation_performed"] is False
    assert result["eligible_for_install_recheck"] is backup_present
    after = {p.name: p.read_bytes() for p in s["data"].iterdir() if p.is_file()}
    assert after == before


def test_actual_release_cli_preflight_leaves_source_directory_bytes_unchanged(prepared):
    source, folder, database, _ = prepared
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    before = {p.name: p.read_bytes() for p in database.parent.iterdir() if p.is_file()}
    assert not any(name.endswith(("-wal", "-shm")) for name in before)
    root = Path(__file__).resolve().parents[2]
    process = subprocess.run([sys.executable, str(root / "desktop/release_tool.py"),
        "compatibility-preflight", str(folder), "--database", str(database),
        "--current-definition", str(source / "release.json")],
        cwd=root, capture_output=True, timeout=20)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    result = json.loads(process.stdout)
    assert result["blockers"] == ["verified_full_backup_required"]
    assert result["installation_performed"] is False
    assert {p.name: p.read_bytes() for p in database.parent.iterdir() if p.is_file()} == before


def test_generic_preflight_keeps_wal_commits_and_detects_stale_backup(setup, tmp_path):
    s = setup
    from app.data_backup import create_backup
    from app.release_schema import compatibility_preflight
    backup = create_backup(s["data"], destination=tmp_path / "stale.sfbak")
    database = crashed_wal(s)
    before = originals(database)
    manifest = module.verify_candidate(s["candidate"], s["trust"])
    result = compatibility_preflight(database, manifest["target_schema"], manifest["definition"]["data_contract"],
        current_contract=manifest["definition"]["data_contract"], backup=backup["path"])
    assert result["backup"]["status"] == "verified"
    assert result["backup"]["matches_database"] is False
    assert "backup_database_differs" in result["blockers"]
    assert_originals(database, before)


def test_generic_preflight_rejects_source_change_during_snapshot_review(setup, monkeypatch):
    s = setup
    database = crashed_wal(s)
    from app import release_schema
    manifest = module.verify_candidate(s["candidate"], s["trust"])
    original_schema = release_schema.schema_snapshot

    def schema(snapshot):
        assert Path(snapshot) != database
        result = original_schema(snapshot)
        with closing(sqlite3.connect(database)) as writer:
            writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            writer.execute("INSERT INTO samples VALUES ('changed-by-test-during-preflight')")
            writer.commit()
        return result

    monkeypatch.setattr(release_schema, "schema_snapshot", schema)
    with pytest.raises(ReleaseError, match="WAL.*变化|混合版本"):
        release_schema.compatibility_preflight(database, manifest["target_schema"],
            manifest["definition"]["data_contract"], current_contract=manifest["definition"]["data_contract"])


def test_no_pending_recovery_does_not_open_source_sql_or_create_sidecars(setup, monkeypatch):
    s = setup
    database = s["data"] / "scholarflow.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    before = {p.name: p.read_bytes() for p in s["data"].iterdir() if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("recovery with no transaction must not open source SQL")

    monkeypatch.setattr(database_io.sqlite3, "connect", forbidden)
    assert recover(s)["state"] == "nothing_pending"
    after = {p.name: p.read_bytes() for p in s["data"].iterdir()
             if p.is_file() and p.name != ".runtime.lock"}
    assert after == before


def test_unverified_checkpoint_setting_refuses_before_first_source_sql(setup, monkeypatch):
    s = setup
    database = crashed_wal(s)
    before = originals(database)
    original_connect = sqlite3.connect

    class UnconfirmedConnection(sqlite3.Connection):
        def getconfig(self, option):
            return False

        def execute(self, *args, **kwargs):
            pytest.fail("unconfirmed protection cannot execute any source SQL")

    def connect(path, *args, **kwargs):
        if str(path) == database.as_uri() + "?mode=rw":
            kwargs["factory"] = UnconfirmedConnection
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(database_io.sqlite3, "connect", connect)
    with pytest.raises(ReleaseError, match="无法禁止"):
        install(s)
    assert_originals(database, before)


@pytest.mark.parametrize("boundary", ["previous_moved", "candidate_activated", "before_commit"])
def test_external_process_tree_kill_and_recovery_preserve_exited_wal(setup, tmp_path, boundary):
    s = setup
    database = crashed_wal(s)
    before = originals(database)
    program = module._tree_fingerprint(s["target"])
    marker = tmp_path / "owned-installer-paused"
    trust = tmp_path / "external-kill-test-trust.json"
    trust.write_text(json.dumps(s["trust"]), encoding="utf-8")
    code = """import json,sys,time
from pathlib import Path
from app import release_install as module
candidate,target,data,trust,boundary,marker=sys.argv[1:]
module._health=lambda *args:None
def checkpoint(name):
 if name==boundary:
  Path(marker).write_text('paused',encoding='ascii')
  time.sleep(600)
module._checkpoint=checkpoint
module.install(candidate,target,data,trusted_keys=json.loads(Path(trust).read_text()))
"""
    env = {key: value for key, value in os.environ.items() if not any(
        word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PYTHONPATH", "DATA_DIR"))}
    env.update(DATA_DIR=str(tmp_path / "kill-child-config"), PYTHONUTF8="1")
    process = subprocess.Popen([sys.executable, "-c", code, str(s["candidate"]), str(s["target"]),
        str(s["data"]), str(trust), boundary, str(marker)], cwd=Path(__file__).resolve().parents[1], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.03)
        assert marker.exists(), process.communicate(timeout=2)
        kill_probe(process)
    finally:
        if process.poll() is None:
            kill_probe(process)
    assert module.barrier_path(s["data"]).exists()
    assert_originals(database, before)
    assert recover(s)["state"] == "rolled_back"
    assert recover(s)["state"] == "rolled_back"
    assert module._tree_fingerprint(s["target"]) == program
    assert_originals(database, before)

