"""Fixed export storage schema, exact source upgrades, and safe DDL snapshots."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine

from app.db import Base
from app import db as database, legacy_cross_schema as cross, legacy_migrations as migrations
from app.legacy_schema_contracts import (
    OLD20_FTS_OBJECTS, OLD20_FTS_SHA256, OLD20_NO_FTS_OBJECTS, OLD20_NO_FTS_SHA256,
    PRE_EXPORT_OBJECTS, PRE_EXPORT_SHA256, TARGET_OBJECTS, TARGET_SHA256,
)
from app.release_schema import create_target_schema, schema_snapshot
from app.release_info import ReleaseError
from app import schema_backups


def create_objects(path, objects):
    with closing(sqlite3.connect(path)) as db:
        for kind in ("table", "index"):
            for typ, name, _, sql in objects:
                if typ == kind and sql and not name.startswith("chunks_fts_"):
                    db.execute(sql)
        db.commit()


def seed_retained_blob(path):
    payload = b"\x00\xffretained source bytes\r\n" + "中文😀".encode()
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO source_snapshots (id,scope_type,scope_id,fingerprint,raw_sha256,text_sha256,media_type,raw_bytes,text,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("1" * 32, "conversation", "old-owner", "2" * 64, hashlib.sha256(payload).hexdigest(), None,
             "application/octet-stream", payload, None, "2026-11-16 00:00:00.000000"))
        db.execute("INSERT INTO desktop_state (key,revision,value,updated_at) VALUES (?,?,?,?)",
            ("retained", 7, json.dumps({"text": "中文😀", "literal": "unchanged"}), "2026-11-16 00:00:00.000000"))
        db.commit()
    return payload


def test_fixed_export_blob_storage_is_registered():
    assert "artifact_exports" in Base.metadata.tables, "Fixed export bytes have no registered persistent table"
    assert Base.metadata.tables["artifact_exports"].c.origin_run_id.type.length == 96


def test_real_orm_static_target_and_registry_are_exactly_the_same_schema():
    root = Path(__file__).resolve().parents[2]
    actual = create_target_schema(root)
    cross.verify_contracts()
    assert actual["sha256"] == TARGET_SHA256 == migrations.CURRENT_SCHEMA
    assert {(item[0], item[1]) for item in TARGET_OBJECTS} - {(item[0], item[1]) for item in PRE_EXPORT_OBJECTS} == {
        ("table", "artifact_exports"), ("index", "ix_artifact_exports_artifact_id"),
        ("index", "ix_artifact_exports_conversation_id"), ("index", "ix_artifact_exports_revision_id")}
    assert PRE_EXPORT_SHA256 == "22e5eb1d2bc008f1e026c2801f59e74a674e187ab866b5019e8ef16cfe6c84e1"
    for ident, source in ((migrations.MIGRATION_ID, TARGET_SHA256), (cross.PRE_EXPORT_ID, PRE_EXPORT_SHA256), *cross.SOURCES.items()):
        assert migrations.resolve(ident, source, TARGET_SHA256, migrations.CONTRACT)["target_schema_sha256"] == TARGET_SHA256


@pytest.mark.parametrize("objects,source,ident", [
    (OLD20_FTS_OBJECTS, OLD20_FTS_SHA256, cross.FTS_ID),
    (OLD20_NO_FTS_OBJECTS, OLD20_NO_FTS_SHA256, cross.NO_FTS_ID),
], ids=["exact-old20-fts", "exact-old20-no-fts"])
def test_each_unchanged_old20_source_reaches_new_target_without_fabricating_exports(tmp_path, objects, source, ident):
    path = tmp_path / "old20.db"
    create_objects(path, objects)
    assert schema_snapshot(path)["sha256"] == source
    migrations.prepare_structure(path, ident)
    assert schema_snapshot(path)["sha256"] == TARGET_SHA256
    cross.verify_new_tables(path)
    cross.verify_empty_exports(path)


def test_previous_current_adds_only_empty_exports_and_preserves_retained_blob_and_rows(tmp_path):
    path = tmp_path / "previous.db"
    create_objects(path, PRE_EXPORT_OBJECTS)
    raw = seed_retained_blob(path)
    assert schema_snapshot(path)["sha256"] == PRE_EXPORT_SHA256
    with closing(sqlite3.connect(path)) as db:
        before = migrations.immutable_rows(db, cross.PRE_EXPORT_ID)
    migrations.prepare_structure(path, cross.PRE_EXPORT_ID)
    migrations.transform(path, cross.PRE_EXPORT_ID)
    proof = migrations.finish_cross_schema(path, cross.PRE_EXPORT_ID, stage=tmp_path, output=tmp_path / "output", manifest={}, transaction_id="synthetic")
    assert proof == {"mapping_protocol": "fixed_export_table_addition_v1", "fixed_exports_created": 0}
    assert schema_snapshot(path)["sha256"] == TARGET_SHA256
    with closing(sqlite3.connect(path)) as db:
        assert migrations.immutable_rows(db, cross.PRE_EXPORT_ID) == before
        assert db.execute("SELECT raw_bytes FROM source_snapshots").fetchone() == (raw,)
        assert db.execute("SELECT count(*) FROM artifact_exports").fetchone() == (0,)


def test_unreviewed_drift_in_previous_current_is_refused_before_export_ddl(tmp_path):
    path = tmp_path / "drift.db"
    create_objects(path, PRE_EXPORT_OBJECTS)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE INDEX unreviewed_export_source_drift ON desktop_state (revision)")
        db.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ReleaseError, match="精确导出前"):
        migrations.prepare_structure(path, cross.PRE_EXPORT_ID)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='artifact_exports'").fetchone() is None


@pytest.mark.parametrize("ident,source,target", [
    (cross.PRE_EXPORT_ID, TARGET_SHA256, TARGET_SHA256),
    (cross.PRE_EXPORT_ID, PRE_EXPORT_SHA256, PRE_EXPORT_SHA256),
    (migrations.MIGRATION_ID, PRE_EXPORT_SHA256, TARGET_SHA256),
    ("current-schema-dormant-copy-20261116-v1", PRE_EXPORT_SHA256, TARGET_SHA256),
    ("git-old20-fts-to-current-dormant-20261116-v1", OLD20_FTS_SHA256, TARGET_SHA256),
    ("git-old20-no-fts-to-current-dormant-20261116-v1", OLD20_NO_FTS_SHA256, TARGET_SHA256),
])
def test_migration_identity_never_silently_retargets_an_existing_or_wrong_contract(ident, source, target):
    with pytest.raises(ReleaseError):
        migrations.resolve(ident, source, target, migrations.CONTRACT)


def test_startup_takes_verified_previous_schema_snapshot_before_export_ddl(tmp_path, monkeypatch):
    path = tmp_path / "startup.db"
    create_objects(path, PRE_EXPORT_OBJECTS)
    raw = seed_retained_blob(path)
    engine = create_engine("sqlite:///" + path.as_posix())
    original = schema_backups.prepare_schema_snapshot
    observed = []

    def protected(selected_engine, metadata):
        assert schema_snapshot(path)["sha256"] == PRE_EXPORT_SHA256
        assert schema_backups.schema_changes(selected_engine, metadata) == {"tables": ["artifact_exports"], "columns": {}}
        snapshot = original(selected_engine, metadata)
        assert snapshot and schema_snapshot(snapshot)["sha256"] == PRE_EXPORT_SHA256
        observed.append(snapshot)
        return snapshot

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(schema_backups, "prepare_schema_snapshot", protected)
    try:
        database.init_db()
        assert schema_snapshot(path)["sha256"] == TARGET_SHA256
        manifest = json.loads(observed[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert manifest["status"] == "migration_succeeded"
        with closing(sqlite3.connect(observed[0])) as db:
            assert db.execute("SELECT raw_bytes FROM source_snapshots").fetchone() == (raw,)
    finally:
        engine.dispose()


def test_failed_snapshot_verification_leaves_previous_schema_and_blob_unmodified(tmp_path, monkeypatch):
    path = tmp_path / "blocked.db"
    create_objects(path, PRE_EXPORT_OBJECTS)
    raw = seed_retained_blob(path)
    engine = create_engine("sqlite:///" + path.as_posix())
    monkeypatch.setattr(database, "engine", engine)

    def broken(*args, **kwargs):
        raise schema_backups.MigrationProtectionError("synthetic export migration snapshot verification failure")

    monkeypatch.setattr(schema_backups, "verify_database_snapshot", broken)
    try:
        with pytest.raises(schema_backups.MigrationProtectionError, match="synthetic export"):
            database.init_db()
        assert schema_snapshot(path)["sha256"] == PRE_EXPORT_SHA256
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT raw_bytes FROM source_snapshots").fetchone() == (raw,)
    finally:
        engine.dispose()
