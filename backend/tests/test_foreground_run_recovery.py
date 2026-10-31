"""Foreground answer checkpoints survive browser/process loss without entering QA history."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Conversation, DeletedConversation, ForegroundRun, QARecord

client = TestClient(app)


def create(run_id="chat_recovery_001", conversation_id="c_front_recovery"):
    return client.post("/api/foreground-runs", json={
        "id": run_id, "conversation_id": conversation_id, "question": "Explain the partial result",
        "paper_id": "p_one", "skills": ["research"],
        "images": [{"media_type": "image/png", "data": "YWJj"}],
    })


def test_partial_answer_is_restored_and_crashed_run_expires(db):
    response = create()
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    saved = client.patch("/api/foreground-runs/chat_recovery_001", json={
        "revision": 1, "partial_answer": "First verified paragraph.", "phase": "answer_delta",
    })
    assert saved.status_code == 200
    assert saved.json()["revision"] == 2
    assert client.get("/api/foreground-runs", params={"conversation_id": "c_front_recovery"}).json()["runs"][0]["partial_answer"] == "First verified paragraph."
    history = client.get("/api/conversations").json()["conversations"]
    assert history[0]["draft_only"] is True and history[0]["interrupted_only"] is True
    assert client.get("/api/conversations/c_front_recovery").json()["messages"] == []
    assert db.query(QARecord).count() == 0

    run = db.get(ForegroundRun, "chat_recovery_001")
    run.heartbeat_at = datetime.utcnow() - timedelta(seconds=30)
    db.commit()
    restored = client.get("/api/foreground-runs", params={"conversation_id": "c_front_recovery"}).json()["runs"][0]
    assert restored["status"] == "interrupted"
    assert restored["phase"] == "answer_delta"
    assert restored["partial_answer"] == "First verified paragraph."
    assert "中断" in restored["error"]


def test_live_foreground_run_blocks_delete_then_recovery_can_be_dismissed(db):
    cid = "c_front_delete"
    db.add(Conversation(id=cid, title="Active foreground"))
    db.commit()
    assert create("chat_recovery_002", cid).status_code == 200
    assert client.delete(f"/api/conversations/{cid}").status_code == 409
    assert client.delete("/api/foreground-runs/chat_recovery_002").status_code == 409
    assert client.patch("/api/foreground-runs/chat_recovery_002", json={
        "revision": 1, "status": "cancelled", "partial_answer": "Stopped safely.",
    }).status_code == 200
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert db.get(DeletedConversation, cid) is not None
    assert db.get(ForegroundRun, "chat_recovery_002") is None


def test_revision_conflict_lost_response_and_terminal_state_are_idempotent():
    assert create("chat_recovery_003", "c_front_revision").status_code == 200
    first = client.patch("/api/foreground-runs/chat_recovery_003", json={
        "revision": 1, "partial_answer": "Persisted once", "phase": "answer_delta",
    })
    assert first.status_code == 200
    retry = client.patch("/api/foreground-runs/chat_recovery_003", json={
        "revision": 1, "partial_answer": "Persisted once", "phase": "answer_delta",
    })
    assert retry.status_code == 200
    assert retry.json()["revision"] == 2
    conflict = client.patch("/api/foreground-runs/chat_recovery_003", json={
        "revision": 1, "partial_answer": "Overwrite",
    })
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current"]["partial_answer"] == "Persisted once"
    stopped = client.patch("/api/foreground-runs/chat_recovery_003", json={
        "revision": 2, "status": "interrupted", "error": "Connection closed",
    })
    assert stopped.status_code == 200
    assert client.patch("/api/foreground-runs/chat_recovery_003", json={
        "revision": 3, "status": "running",
    }).status_code == 409
    assert client.delete("/api/foreground-runs/chat_recovery_003").status_code == 200
    assert client.get("/api/foreground-runs", params={"conversation_id": "c_front_revision"}).json()["runs"] == []


@pytest.mark.parametrize("payload", [
    {"id": "bad", "conversation_id": "c_valid", "question": "q"},
    {"id": "chat_invalid_001", "conversation_id": "c_valid", "question": " "},
    {"id": "chat_invalid_002", "conversation_id": "c_valid", "question": "q", "images": [{"media_type": "image/svg+xml", "data": "YWJj"}]},
    {"id": "chat_invalid_003", "conversation_id": "c_valid", "question": "q", "images": [{"media_type": "image/png", "data": "not-base64"}]},
])
def test_invalid_frontend_checkpoint_payload_is_rejected(payload):
    assert client.post("/api/foreground-runs", json=payload).status_code == 422


def test_tombstone_rejects_new_or_late_foreground_checkpoints(db):
    db.add(DeletedConversation(id="c_front_deleted"))
    db.commit()
    response = client.post("/api/foreground-runs", json={
        "id": "chat_recovery_004", "conversation_id": "c_front_deleted", "question": "stale",
    })
    assert response.status_code == 410
    assert client.get("/api/foreground-runs", params={"conversation_id": "c_front_deleted"}).status_code == 410
