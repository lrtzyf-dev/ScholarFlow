"""Schema snapshots use isolated files, including WAL and failed upgrades."""
import json
import sqlite3

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text

from app import db as database
from app.schema_backups import (
    MigrationProtectionError, prepare_schema_snapshot, schema_changes,
    stage_database_restore, verify_database_snapshot,
)


@pytest.fixture
def old_database(tmp_path):
    path = tmp_path / "中文 空间" / "old.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO legacy VALUES (1, 'original')")
    connection.commit()
    engine = create_engine(f"sqlite:///{path}")
    metadata = MetaData()
    Table("legacy", metadata, Column("id", Integer, primary_key=True), Column("value", String), Column("revision", Integer))
    yield path, engine, metadata, connection
    connection.close()
    engine.dispose()


def test_online_snapshot_includes_committed_wal_and_restores_to_new_file(old_database, tmp_path):
    path, engine, metadata, connection = old_database
    assert schema_changes(engine, metadata)["columns"] == {"legacy": ["revision"]}
    snapshot = prepare_schema_snapshot(engine, metadata)
    manifest = json.loads(snapshot.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["scope"] == "database_only" and manifest["status"] == "prepared"
    verify_database_snapshot(snapshot, expected_hash=manifest["sha256"])
    destination = stage_database_restore(snapshot, tmp_path / "staged.sqlite3")
    with sqlite3.connect(destination) as restored:
        assert restored.execute("SELECT value FROM legacy").fetchall() == [("original",)]
        assert "revision" not in {row[1] for row in restored.execute("PRAGMA table_info(legacy)")}
    with pytest.raises(MigrationProtectionError):
        stage_database_restore(snapshot, path)
    assert connection.execute("SELECT value FROM legacy").fetchone() == ("original",)


def test_corrupted_backup_is_not_restored(old_database, tmp_path):
    _, engine, metadata, _ = old_database
    snapshot = prepare_schema_snapshot(engine, metadata)
    with snapshot.open("ab") as stream:
        stream.write(b"tamper")
    destination = tmp_path / "do-not-create.sqlite3"
    with pytest.raises(MigrationProtectionError):
        stage_database_restore(snapshot, destination)
    assert not destination.exists()


def test_fresh_or_current_schema_does_not_make_backup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite3'}")
    metadata = MetaData()
    Table("sample", metadata, Column("id", Integer, primary_key=True))
    try:
        assert prepare_schema_snapshot(engine, metadata) is None
        metadata.create_all(engine)
        assert prepare_schema_snapshot(engine, metadata) is None
        assert not (tmp_path / "backups").exists()
    finally:
        engine.dispose()


def test_snapshot_failure_prevents_any_schema_change(old_database, monkeypatch):
    _, engine, metadata, _ = old_database
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database.Base, "metadata", metadata)
    import app.schema_backups as backups
    monkeypatch.setattr(backups, "verify_database_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(MigrationProtectionError("synthetic integrity failure")))
    with pytest.raises(MigrationProtectionError):
        database.init_db()
    assert "revision" not in {column["name"] for column in inspect(engine).get_columns("legacy")}


def test_failed_migration_keeps_recoverable_pre_upgrade_schema(old_database, monkeypatch, tmp_path):
    path, engine, metadata, _ = old_database
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database.Base, "metadata", metadata)

    def broken_migration():
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE legacy ADD COLUMN revision INTEGER"))
        raise RuntimeError("synthetic upgrade interruption")

    monkeypatch.setattr(database, "_lightweight_migrations", broken_migration)
    with pytest.raises(RuntimeError, match="synthetic upgrade"):
        database.init_db()
    manifests = list((path.parent / "backups/schema").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "migration_failed"
    restored = stage_database_restore(manifests[0].with_suffix(".sqlite3"), tmp_path / "verified-rollback.sqlite3")
    with sqlite3.connect(restored) as connection:
        assert "revision" not in {row[1] for row in connection.execute("PRAGMA table_info(legacy)")}


def test_input_dispatch_columns_upgrade_preserves_received_text(tmp_path, monkeypatch):
    path = tmp_path / "old-inputs.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE run_inputs (input_id VARCHAR(80) PRIMARY KEY, content TEXT)"))
            connection.execute(text("INSERT INTO run_inputs VALUES ('stable_input_1', '中文排队输入')"))
        monkeypatch.setattr(database, "engine", engine)
        database._lightweight_migrations()
        database._lightweight_migrations()
        columns = {column["name"] for column in inspect(engine).get_columns("run_inputs")}
        assert {"runtime_config", "workspace", "dispatch_options", "child_run_id", "dispatch_started_at"} <= columns
        with engine.connect() as connection:
            assert connection.execute(text("SELECT content, child_run_id FROM run_inputs")).all() == [("中文排队输入", None)]
    finally:
        engine.dispose()
