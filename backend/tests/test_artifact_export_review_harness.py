"""Independent ART03 read-only accounting against actual saved exports.

Only synthetic fixtures and real in-process app routes/read tools are used.
Evidence contains identifiers, counts, hashes and verdicts; never saved bytes.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts import verify_real_acceptance as runner

OUTPUT = Path(__file__).resolve().parents[2] / "verification/artifact-export-review-harness-current/receipts"


def save_receipt(kind, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / f"{kind}-{uuid4().hex}.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


@contextmanager
def actual_review(db):
    from app.agent.tools import execute_tool
    from app.main import app
    from app.runtime_context import bind_runtime_config
    client = TestClient(app)
    try:
        response = client.post("/api/conversations", json={"title": "Independent read-only export accounting"})
        assert response.status_code == 200
        cid, rid = response.json()["id"], "independent-review-" + uuid4().hex
        before = runner.prepare_memo_fixture(client, cid, history=True)
        result = {"answer": "Review only: confirm the fixed revision before proposing a restore.",
            "tools": ["list_research_artifact_versions", "read_research_artifact"],
            "turns": [{"run_id": rid, "http_status": 200, "answer": "Review only.",
                "completed": True, "outcome": "completed"}]}
        with runner.public_artifact_read_probe() as observed:
            with bind_runtime_config({"conversation_id": cid, "run_id": rid}):
                listing = json.loads(execute_tool(db, "list_research_artifact_versions",
                    {"artifact_id": before["artifact_id"]}, conversation_id=cid))
                for version in (listing["items"][-1], listing["items"][0]):
                    value = json.loads(execute_tool(db, "read_research_artifact", {
                        "artifact_id": before["artifact_id"], "revision_id": version["id"]}, conversation_id=cid))
                    assert value["version"] == version["version"]
        result["artifact_history_review"] = runner.artifact_history_review_receipt(client, cid, before, result, observed)
        assert runner.artifact_history_reviewed(result, cid), "Valid actual read receipts must pass the unchanged read-only control"
        case = next(case for case in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"] if case["id"] == "ART03")
        assert all(runner.evaluate_receipts(case, result, client, cid, "unused").values())
        yield client, cid, result, case
    finally:
        client.close()


def create_export(client, cid, before):
    response = client.post(f"/api/artifacts/{before['artifact_id']}/exports", json={
        "conversation_id": cid, "request_id": "independent_harness_" + uuid4().hex,
        "revision_id": before["current_revision_id"], "format": "docx"})
    assert response.status_code == 200
    value = response.json()
    actual = client.get(value["download_url"])
    assert actual.status_code == 200
    assert hashlib.sha256(actual.content).hexdigest() == value["payload_sha256"]
    return value


def test_art03_cannot_report_read_only_after_actual_api_persisted_a_new_export(db):
    from app.artifact_models import ArtifactExport
    with actual_review(db) as (client, cid, result, case):
        proof = result["artifact_history_review"]
        before = proof["before"]
        before_count = db.query(ArtifactExport).count()
        exported = create_export(client, cid, before)
        proof["after"] = runner.artifact_review_snapshot(client, cid, before["artifact_id"])
        after_count = db.query(ArtifactExport).count()
        reviewed = runner.artifact_history_reviewed(result, cid)
        checks = runner.evaluate_receipts(case, result, client, cid, "unused")
        save_receipt("actual-post-export", {"artifact_id": before["artifact_id"], "export_id": exported["export_id"],
            "actual_export_count_before": before_count, "actual_export_count_after": after_count,
            "actual_payload_sha256": exported["payload_sha256"],
            "artifact_head_unchanged": before["current_revision_id"] == proof["after"]["current_revision_id"],
            "snapshots_equal": before == proof["after"],
            "persistent_tables_before": sorted(before["persistent_state"]),
            "persistent_tables_after": sorted(proof["after"]["persistent_state"]),
            "artifact_history_reviewed": reviewed, "art03_machine_checks": checks})
        assert after_count == before_count + 1
        assert not reviewed, "ART03 falsely accepted read-only history review after real persisted export creation"
        assert checks["artifact_history_reviewed"] is False


def test_export_accounting_keeps_actual_get_only_review_passing(db):
    with actual_review(db) as (client, cid, result, case):
        proof = result["artifact_history_review"]
        exported = create_export(client, cid, proof["before"])
        # The export exists before the measured GET-only interval.
        proof["before"] = runner.artifact_review_snapshot(client, cid, proof["before"]["artifact_id"])
        for _ in range(2):
            assert client.get(exported["download_url"]).status_code == 200
            assert client.get(f"/api/artifacts/{exported['artifact_id']}/exports", params={"conversation_id": cid}).status_code == 200
        proof["after"] = runner.artifact_review_snapshot(client, cid, proof["before"]["artifact_id"])
        assert proof["before"] == proof["after"]
        assert runner.artifact_history_reviewed(result, cid)
        assert all(runner.evaluate_receipts(case, result, client, cid, "unused").values())


def test_export_accounting_binds_every_saved_column_including_actual_blob_without_exposing_bytes(db):
    from sqlalchemy import update
    from app.artifact_models import ArtifactExport
    with actual_review(db) as (client, cid, result, _):
        proof = result["artifact_history_review"]
        exported = create_export(client, cid, proof["before"])
        proof["before"] = runner.artifact_review_snapshot(client, cid, proof["before"]["artifact_id"])
        row = db.get(ArtifactExport, exported["export_id"], populate_existing=True)
        originals = {column.name: deepcopy(getattr(row, column.name)) for column in ArtifactExport.__table__.columns}
        inspected = []
        # Change each database column independently, including leaving the
        # declared payload hash/size untouched when changing the actual BLOB.
        # These are corruption probes, not application API permissions.
        for column, old in originals.items():
            if column == "payload":
                changed = bytes([old[0] ^ 1]) + old[1:]
            elif column == "created_at":
                changed = old + timedelta(seconds=1)
            elif isinstance(old, dict):
                changed = {**old, "independent_probe": True}
            elif type(old) is int:
                changed = old + 1
            elif old is None:
                changed = "independent_probe"
            elif isinstance(old, str):
                changed = ("f" if old[:1] != "f" else "e") + old[1:]
            else:
                raise AssertionError("Unexpected persisted column type in the export accounting test")
            db.execute(update(ArtifactExport).where(ArtifactExport.id == exported["export_id"]).values({column: changed}))
            db.commit()
            proof["after"] = runner.artifact_review_snapshot(client, cid, proof["before"]["artifact_id"])
            encoded = json.dumps(proof["after"]["persistent_state"], ensure_ascii=False, sort_keys=True)
            assert "PK\\u0003\\u0004" not in encoded
            assert not runner.artifact_history_reviewed(result, cid), f"Saved export column {column} changed without invalidating read-only review"
            inspected.append(column)
            current_id = changed if column == "id" else exported["export_id"]
            db.execute(update(ArtifactExport).where(ArtifactExport.id == current_id).values({column: old}))
            db.commit()
            restored = runner.artifact_review_snapshot(client, cid, proof["before"]["artifact_id"])
            assert restored == proof["before"], f"Corruption probe failed to restore column {column}"
        save_receipt("all-export-columns", {"artifact_id": exported["artifact_id"], "export_id": exported["export_id"],
            "independently_changed_columns": inspected, "column_count": len(inspected),
            "actual_blob_changed_with_declared_hash_and_size_unchanged": True,
            "full_source_bytes_or_document_body_written_to_evidence": False})
