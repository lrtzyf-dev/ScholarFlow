"""Unbound legacy upload receipts survive upgrades without gaining trust."""
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import db as database
from app import schema_backups, source_store
from app.main import app
from app.models import Conversation
from app.source_models import Source, SourceCaptureReceipt, SourceRevision, SourceSnapshot


CID, REQUEST, TITLE = "legacy-source-scope", "legacy-upload-request", "legacy.txt"
RAW = b"original first line\noriginal second line"


@pytest.fixture
def legacy_receipt(tmp_path, monkeypatch):
    folder = tmp_path / "旧版 回执"
    folder.mkdir()
    path = folder / "old.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    database.Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE source_capture_receipts"))
        connection.execute(text("""CREATE TABLE source_capture_receipts (
            id VARCHAR(32) PRIMARY KEY, scope_type VARCHAR(16) NOT NULL,
            scope_id VARCHAR(96) NOT NULL, request_id VARCHAR(96) NOT NULL,
            payload_sha256 VARCHAR(64) NOT NULL,
            revision_id VARCHAR(32) NOT NULL REFERENCES source_revisions(id),
            created_at DATETIME NOT NULL,
            UNIQUE(scope_type, scope_id, request_id))"""))
    with Session(engine) as session:
        session.add(Conversation(id=CID, title="Legacy source"))
        scope = source_store.SourceScope("conversation", CID)
        captured = source_store.capture_source(session, scope=scope,
            authorization=source_store.SourceAuthorization(scope, "upload:" + REQUEST, "upload"),
            kind="upload", title=TITLE, raw_bytes=RAW, text=RAW.decode(), media_type="text/plain",
            origin_ref=TITLE, text_origin="parsed_text", parser="utf8-sig/1")
        payload_hash = hashlib.sha256(json.dumps([TITLE, "text/plain", hashlib.sha256(RAW).hexdigest(),
            None, None], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        session.execute(text("""INSERT INTO source_capture_receipts
            (id, scope_type, scope_id, request_id, payload_sha256, revision_id, created_at)
            VALUES (:id, 'conversation', :cid, :request, :digest, :revision, CURRENT_TIMESTAMP)"""),
            {"id": "a" * 32, "cid": CID, "request": REQUEST, "digest": payload_hash,
             "revision": captured["revision_id"]})
        session.commit()
    with engine.connect() as connection:
        original = tuple(connection.execute(text("SELECT * FROM source_capture_receipts")).one())
    monkeypatch.setattr(database, "engine", engine)
    try:
        yield engine, path, captured, original
    finally:
        engine.dispose()


def test_source_receipt_upgrade_preserves_original_rows_and_verified_pre_ddl_snapshot(legacy_receipt, tmp_path):
    engine, path, captured, original = legacy_receipt
    database.init_db()
    database.init_db()  # Repeating startup neither blesses the old row nor adds another backup.
    assert "revision_fingerprint" in {column["name"] for column in inspect(engine).get_columns("source_capture_receipts")}
    with engine.connect() as connection:
        row = tuple(connection.execute(text("SELECT * FROM source_capture_receipts")).one())
    assert row[:-1] == original and row[-1] is None
    with Session(engine) as session:
        assert session.get(Source, captured["source_id"]).version == 1
        revision = session.get(SourceRevision, captured["revision_id"])
        assert revision.authorization_id == "upload:" + REQUEST
        assert session.get(SourceSnapshot, captured["snapshot_id"]).raw_bytes == RAW
    manifests = list((path.parent / "backups" / "schema").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "migration_succeeded"
    assert manifest["changes"]["columns"]["source_capture_receipts"] == ["revision_fingerprint"]
    restored = schema_backups.stage_database_restore(manifests[0].with_suffix(".sqlite3"), tmp_path / "restored.sqlite3")
    with sqlite3.connect(restored) as connection:
        assert "revision_fingerprint" not in {column[1] for column in connection.execute("PRAGMA table_info(source_capture_receipts)")}
        assert connection.execute("SELECT * FROM source_capture_receipts").fetchone() == original


def test_upgraded_null_receipt_is_rejected_until_user_makes_a_fresh_capture(legacy_receipt, monkeypatch):
    engine, _, captured, original = legacy_receipt
    database.init_db()

    def scoped_db():
        with Session(engine) as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, database.get_db, scoped_db)
    client = TestClient(app)

    def upload(request):
        return client.post("/api/sources/upload", files={"file": (TITLE, RAW, "text/plain")},
                           data={"conversation_id": CID, "request_id": request})

    assert upload(REQUEST).status_code == 409
    fresh = upload("fresh-upload-request")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["source_id"] != captured["source_id"]
    replay = upload("fresh-upload-request")
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["revision_id"] == fresh.json()["revision_id"]
    with Session(engine) as session:
        legacy = session.scalar(select(SourceCaptureReceipt).where(SourceCaptureReceipt.request_id == REQUEST))
        new = session.scalar(select(SourceCaptureReceipt).where(SourceCaptureReceipt.request_id == "fresh-upload-request"))
        assert legacy.revision_fingerprint is None and legacy.revision_id == captured["revision_id"]
        assert legacy.payload_sha256 == original[4]
        assert new.revision_fingerprint == session.get(SourceRevision, new.revision_id).fingerprint
        assert len(session.scalars(select(SourceSnapshot)).all()) == 1
        assert len(session.scalars(select(Source)).all()) == 2


def test_source_receipt_migration_stops_before_ddl_when_snapshot_verification_fails(legacy_receipt, monkeypatch):
    engine, _, _, original = legacy_receipt

    def fail_snapshot(*args, **kwargs):
        raise schema_backups.MigrationProtectionError("synthetic failed snapshot verification")

    monkeypatch.setattr(schema_backups, "verify_database_snapshot", fail_snapshot)
    with pytest.raises(schema_backups.MigrationProtectionError, match="synthetic failed snapshot"):
        database.init_db()
    assert "revision_fingerprint" not in {column["name"] for column in inspect(engine).get_columns("source_capture_receipts")}
    with engine.connect() as connection:
        assert tuple(connection.execute(text("SELECT * FROM source_capture_receipts")).one()) == original
