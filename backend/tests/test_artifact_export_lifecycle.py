"""Real export BLOB persistence through backup, dormant import, and deletion."""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from docx import Document
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import artifact_store, data_backup, data_cleanup, legacy_cross_schema as cross, legacy_migrations as migrations, storage_policy
from app.artifact_models import ArtifactExport
from app.models import Conversation, ConversationOptions, DeletedConversation
from app.api.conversations import delete as delete_conversation
from app.exporter import build_docx
from app.release_info import ReleaseError, read_json
from app.release_files import digest
from app.release_schema import schema_snapshot
from app.atomic_files import atomic_write_json
from .test_artifact_export_schema import create_objects, seed_retained_blob
from app.legacy_schema_contracts import PRE_EXPORT_OBJECTS
from .test_legacy_install import legacy, setup, prepare, files
from .test_legacy_import import review, run


def create_export(db, cid=None, *, stored_cid="same", request_id=None):
    request_id = request_id or uuid4().hex
    item = artifact_store.create(db, request_id=uuid4().hex, title="固定成果", kind="document",
        content="固定正文。", evidence=[], analysis={}, conversation_id=cid)
    payload = build_docx("固定成果", "固定正文。", [])
    row = ArtifactExport(artifact_id=item["id"], revision_id=item["current_revision_id"],
        conversation_id=cid if stored_cid == "same" else stored_cid,
        scope_key="conversation:" + cid if cid else "local", request_id=request_id,
        request_sha256="1" * 64, content_sha256=item["revision"]["content_sha256"],
        manifest_sha256=item["revision"]["manifest_sha256"], title=item["title"], format="docx",
        renderer_version="synthetic-docx-storage-v1", filename="fixed.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(), payload_size=len(payload),
        validation={"kind": "storage_fixture", "semantic_verification": "not_assessed"})
    db.add(row)
    db.commit()
    return row


def create_root(root):
    root.mkdir()
    cross.create_target(root / "scholarflow.db")
    (root / "config.json").write_text(json.dumps({"approval_mode": "full_access"}), encoding="utf-8")
    return root


def test_blob_defaults_and_unique_request_scope_are_enforced_by_database(db):
    first = create_export(db, request_id="same-fixed-request")
    assert first.protocol == "artifact-export-v1" and first.options == {}
    assert first.payload_size == len(first.payload) and isinstance(first.payload, bytes)
    fields = {column.name: getattr(first, column.name) for column in ArtifactExport.__table__.columns
              if column.name not in ("id", "created_at")}
    db.add(ArtifactExport(**fields))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.scalar(select(ArtifactExport).where(ArtifactExport.id == first.id)).payload == first.payload
    fields["scope_key"] = "conversation:other-explicit-scope"
    db.add(ArtifactExport(**fields))
    db.commit()
    assert len(db.scalars(select(ArtifactExport)).all()) == 2


def test_conversation_delete_removes_export_blob_via_direct_and_revision_links_only(db):
    db.add_all([Conversation(id="remove-export"), Conversation(id="keep-export")])
    db.commit()
    own = create_export(db, "remove-export")
    legacy_scope = create_export(db, "remove-export", stored_cid=None)
    other = create_export(db, "keep-export")
    local = create_export(db)
    owned_ids = {own.id, legacy_scope.id}
    retained = {other.id: other.payload, local.id: local.payload}
    preview = data_cleanup.preview(db, "remove-export")
    assert preview["records"]["artifact_exports"] == 2
    assert preview["storage_preview"]["by_table"]["artifact_exports"] >= len(own.payload) + len(legacy_scope.payload)
    assert preview["storage_preview"]["physical_reclaimed_bytes"] is None
    db.rollback()
    result = delete_conversation("remove-export", db=db)
    assert result["cleanup"]["records"]["artifact_exports"] == 2
    db.expire_all()
    assert db.get(DeletedConversation, "remove-export")
    assert all(db.get(ArtifactExport, ident) is None for ident in owned_ids)
    assert {row.id: row.payload for row in db.scalars(select(ArtifactExport)).all()} == retained


def test_deletion_failure_rolls_back_blob_and_owner_together(db, monkeypatch):
    db.add(Conversation(id="rollback-export"))
    db.commit()
    row = create_export(db, "rollback-export")
    identifier, payload = row.id, row.payload
    db.commit()
    db.execute(text("BEGIN IMMEDIATE"))

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic cleanup receipt failure")

    monkeypatch.setattr(storage_policy, "record_cleanup", broken)
    with pytest.raises(RuntimeError, match="synthetic cleanup"):
        data_cleanup.remove(db, "rollback-export")
    db.rollback()
    db.expire_all()
    assert db.get(ArtifactExport, identifier).payload == payload
    assert db.get(Conversation, "rollback-export")
    assert db.get(DeletedConversation, "rollback-export") is None


def test_backup_and_actual_restore_preserve_committed_wal_docx_bytes_hash_and_read_only_policy(tmp_path):
    source = create_root(tmp_path / "source")
    engine = create_engine("sqlite:///" + (source / "scholarflow.db").as_posix())
    try:
        with Session(engine) as db:
            db.add_all([Conversation(id="blob-backup"), ConversationOptions(conversation_id="blob-backup", approval_mode="full_access")])
            db.commit()
            row = create_export(db, "blob-backup")
            identifier = row.id
    finally:
        engine.dispose()
    payload = build_docx("WAL固定导出", "最终提交到WAL的正文。", [])
    expected = hashlib.sha256(payload).hexdigest()
    with closing(sqlite3.connect(source / "scholarflow.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE artifact_exports SET payload=?,payload_sha256=?,payload_size=? WHERE id=?", (payload, expected, len(payload), identifier))
        writer.commit()
        backup = data_backup.create_backup(source)
        unpacked = tmp_path / "verified"
        manifest = data_backup.verify_archive(backup["path"], extract_to=unpacked)
        assert manifest["credential_portability"] == data_backup.credentials.EXCLUDED_RECONFIGURE
        with closing(sqlite3.connect(unpacked / "scholarflow.db")) as check:
            assert check.execute("SELECT payload,payload_sha256,payload_size FROM artifact_exports").fetchone() == (payload, expected, len(payload))
    target = create_root(tmp_path / "target")
    preview = data_backup.prepare_restore(target, backup["path"])
    data_backup.schedule_restore(target, preview["id"])
    assert data_backup.apply_pending_restore(target)["previous_data_retained"]
    with closing(sqlite3.connect(target / "scholarflow.db")) as restored:
        actual = restored.execute("SELECT payload,payload_sha256,payload_size,protocol,renderer_version FROM artifact_exports").fetchone()
        assert actual == (payload, expected, len(payload), "artifact-export-v1", "synthetic-docx-storage-v1")
        assert restored.execute("SELECT approval_mode FROM conversation_options").fetchone() == ("read_only",)
    assert hashlib.sha256(actual[0]).hexdigest() == actual[1]
    assert any("最终提交到WAL" in p.text for p in Document(io.BytesIO(actual[0])).paragraphs)
    assert json.loads((target / "config.json").read_text(encoding="utf-8"))["approval_mode"] == "read_only"


def test_dormant_current_transform_preserves_completed_export_blob_without_effect_state(tmp_path):
    path = tmp_path / "current.db"
    cross.create_target(path)
    engine = create_engine("sqlite:///" + path.as_posix())
    try:
        with Session(engine) as db:
            row = create_export(db)
            identifier, payload, expected = row.id, row.payload, row.payload_sha256
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as db:
        before = migrations.immutable_rows(db)
    assert "artifact_exports" not in migrations._EFFECTS and before["artifact_exports"]["rows"] == 1
    migrations.transform(path, migrations.MIGRATION_ID)
    with closing(sqlite3.connect(path)) as db:
        assert migrations.immutable_rows(db) == before
        assert db.execute("SELECT payload,payload_sha256 FROM artifact_exports WHERE id=?", (identifier,)).fetchone() == (payload, expected)
    with pytest.raises(ReleaseError, match="固定导出"):
        cross.verify_empty_exports(path)
    with pytest.raises(ReleaseError, match="非审核"):
        cross.verify_new_tables(path)


def test_previous_current_signed_archive_full_import_preserves_originals_and_adds_no_export(legacy, monkeypatch):
    s = legacy
    path = s["data"] / "scholarflow.db"
    path.unlink()  # Exact test-owned fixture created by legacy fixture.
    create_objects(path, PRE_EXPORT_OBJECTS)
    payload = seed_retained_blob(path)
    manifest = read_json(s["candidate"] / "install-manifest.json")
    target = cross._body(cross.TARGET_OBJECTS)
    target["sha256"] = digest(target)
    manifest["target_schema"] = target
    manifest["candidate_id"] = digest({key: value for key, value in manifest.items() if key != "candidate_id"})
    atomic_write_json(s["candidate"] / "install-manifest.json", manifest)
    s["sign"](s["candidate"], manifest)
    s["prepared"] = prepare(s)
    s["output"] = s["data"].parent / "previous-current-independent"
    s["options"] = {"trusted_keys": s["trust"], "migration_id": cross.PRE_EXPORT_ID,
        "expected_preparation_sha256": s["prepared"]["preparation_sha256"]}
    original, program = files(s["data"]), files(s["legacy"])

    def forbidden(*args, **kwargs):
        pytest.fail("No candidate execution or provider HTTP is allowed during import")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    assert review(s)["structure_change_planned"]
    assert run(s)["cross_schema_migration_performed"]
    assert files(s["data"]) == original and files(s["legacy"]) == program and not s["new_target"].exists()
    target_path = s["output"] / "scholarflow.db"
    assert schema_snapshot(target_path)["sha256"] == migrations.CURRENT_SCHEMA
    with closing(sqlite3.connect(target_path)) as db:
        assert db.execute("SELECT raw_bytes FROM source_snapshots").fetchone() == (payload,)
        assert db.execute("SELECT count(*) FROM artifact_exports").fetchone() == (0,)
