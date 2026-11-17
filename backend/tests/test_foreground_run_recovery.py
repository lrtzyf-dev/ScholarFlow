"""Foreground answer checkpoints survive browser/process loss without entering QA history."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.agent.checkpoint import AgentState
from app.foreground_run_store import finish as finish_foreground_run
from app.models import Conversation, DeletedConversation, ForegroundRun, QARecord
from app.timeutil import utcnow

client = TestClient(app)


def create(run_id="chat_recovery_001", conversation_id="c_front_recovery", paper_id=None):
    return client.post("/api/foreground-runs", json={
        "id": run_id, "conversation_id": conversation_id, "question": "Explain the partial result",
        "paper_id": paper_id, "skills": ["research"],
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
    run.heartbeat_at = utcnow() - timedelta(seconds=30)
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


def test_foreground_run_list_keeps_the_most_recent_twenty_in_chronological_order(db):
    base = utcnow() - timedelta(minutes=30)
    db.add_all([
        ForegroundRun(
            id=f"chat_recent_{index:03}",
            conversation_id="c_front_recent",
            question=f"Question {index}",
            status="paused",
            created_at=base + timedelta(seconds=index),
            updated_at=base + timedelta(seconds=index),
            heartbeat_at=base + timedelta(seconds=index),
        )
        for index in range(21)
    ])
    db.commit()

    runs = client.get(
        "/api/foreground-runs", params={"conversation_id": "c_front_recent"}
    ).json()["runs"]

    assert len(runs) == 20
    assert runs[0]["id"] == "chat_recent_001"
    assert runs[-1]["id"] == "chat_recent_020"


def test_budget_pause_persists_private_state_and_resumes_same_foreground_run(db, monkeypatch):
    run_id, conversation_id = "chat_budget_resume_001", "c_front_budget_resume"
    assert create(run_id, conversation_id, paper_id=None).status_code == 200
    run = db.get(ForegroundRun, run_id)
    run.status = "paused"
    run.phase = "paused"
    run.started_at = utcnow()
    run.error = "预算已用完"
    saved = AgentState(
        question=run.question,
        conversation_id=run.conversation_id,
        skills=run.skills,
        phase="paused",
        step=6,
        max_steps=6,
        budget_exhausted=True,
        workspace=run.workspace,
        messages=[{"role": "user", "content": "original prompt"}],
        todo=[{"title": "Read files", "phase": "read", "status": "in_progress"}],
        operations={"tool/0/0": {"result": "saved read"}},
    )
    run.agent_checkpoint = saved.snapshot()
    db.commit()

    listed = client.get("/api/foreground-runs", params={"conversation_id": conversation_id}).json()["runs"][0]
    assert listed["status"] == "paused" and listed["resume_available"] is True
    assert "agent_checkpoint" not in listed

    captured = []
    def fake_run_agent(db_session, *_args, checkpoint, save_checkpoint, run_id, **_kwargs):
        state = AgentState.model_validate(checkpoint)
        captured.append(state.snapshot())
        assert state.phase == "model"
        assert state.max_steps == 12 and state.step == 6
        assert not state.budget_exhausted
        assert state.messages == saved.messages
        assert state.operations == saved.operations
        state.step = state.max_steps
        state.phase = "paused"
        state.budget_exhausted = True
        finish_foreground_run(db_session, run_id, "paused", "预算已用完")
        save_checkpoint(state.snapshot())
        yield "paused", {"text": "已保存检查点"}

    monkeypatch.setattr("app.api.chat.run_agent", fake_run_agent)
    request = {
        "question": run.question,
        "paper_id": run.paper_id,
        "skills": run.skills,
        "conversation_id": run.conversation_id,
        "images": run.images,
        "run_id": run.id,
        "resume_checkpoint": True,
        "additional_steps": 6,
        "workspace_revision": (run.workspace or {}).get("workspace_revision", 0),
        "project_id": (run.workspace or {}).get("project_id"),
        "file_paths": [file["path"] for file in (run.workspace or {}).get("files", [])],
        "execution_mode": run.execution_mode,
        "reviewed_plan_id": run.reviewed_plan_id,
    }
    mismatched = {**request, "question": "different request"}
    assert client.post("/api/chat/stream", json=mismatched).status_code == 409
    assert client.post("/api/chat/stream", json={**request, "additional_steps": 25}).status_code == 422

    response = client.post("/api/chat/stream", json=request)
    assert response.status_code == 200
    assert "event: paused" in response.text
    assert captured
    db.refresh(run)
    assert run.status == "paused"
    assert run.agent_checkpoint["step"] == 12
    assert run.agent_checkpoint["max_steps"] == 12


def test_resume_available_requires_a_valid_checkpoint_without_pending_operation(db):
    conversation_id = "c_front_resume_eligibility"
    invalid_ids = ("chat_invalid_checkpoint_001", "chat_pending_checkpoint_001")
    for run_id in invalid_ids:
        assert create(run_id, conversation_id, paper_id=None).status_code == 200
        run = db.get(ForegroundRun, run_id)
        run.status = "paused"
        run.started_at = utcnow()
        if run_id == invalid_ids[0]:
            run.agent_checkpoint = {"phase": "paused"}
        else:
            run.agent_checkpoint = AgentState(
                question=run.question,
                conversation_id=run.conversation_id,
                paper_id=run.paper_id,
                skills=run.skills,
                phase="paused",
                step=6,
                max_steps=6,
                budget_exhausted=True,
                workspace=run.workspace,
                pending_operation={
                    "key": "tool/5/0",
                    "kind": "tool",
                    "name": "read_file",
                    "arguments": {},
                },
            ).snapshot()
        db.commit()
    db.commit()

    runs = client.get(
        "/api/foreground-runs", params={"conversation_id": conversation_id}
    ).json()["runs"]

    assert {run["id"] for run in runs} == set(invalid_ids)
    assert all(run["resume_available"] is False for run in runs)
