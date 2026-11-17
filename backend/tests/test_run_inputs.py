"""Durable steering reception/application and scoped CAS; no provider calls."""
import pytest
import threading
from sqlalchemy import event, select, text
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.run_inputs import router
from app import run_inputs
from app.models import Conversation, ForegroundRun, TaskRecord
from app.goal_ledger import ensure_run_goal, snapshot_goal
from app.db import engine, SessionLocal

app = FastAPI()
app.include_router(router)
client = TestClient(app)


def seed(db, *, background=False, status="running", workspace_revision=0):
    db.add(Conversation(id="input_conversation", workspace_revision=workspace_revision))
    if background:
        run = TaskRecord(id="task_inputs_123", type="agent", conversation_id="input_conversation",
                         status=status, payload={"question": "original", "workspace": {
                             "workspace_revision": workspace_revision}})
    else:
        run = ForegroundRun(id="chat_inputs_12345678", conversation_id="input_conversation",
                            question="original", status=status,
                            workspace={"workspace_revision": workspace_revision})
    db.add(run)
    db.commit()
    return run.id


def body(**changes):
    return {"input_id": "input_stable_123", "conversation_id": "input_conversation",
            "kind": "steer", "content": "改用B，保留已完成结果", "expected_revision": 0,
            "workspace_revision": 0, **changes}


def test_reception_replay_and_mismatched_id(db):
    run_id = seed(db)
    endpoint = f"/api/chat/runs/{run_id}/inputs"
    accepted = client.post(endpoint, json=body())
    assert accepted.status_code == 200
    assert accepted.json()["input"]["status"] == "queued"
    assert accepted.json()["revision"] == 1
    assert client.post(endpoint, json=body()).json()["input"]["input_id"] == "input_stable_123"
    assert client.post(endpoint, json=body(content="different")).status_code == 409
    saved = client.get(endpoint, params={"conversation_id": "input_conversation"}).json()
    assert len(saved["inputs"]) == 1
    assert saved["next_turn_dispatch"] == "automatic"


def test_stale_revision_keeps_queue_unchanged(db):
    run_id = seed(db)
    endpoint = f"/api/chat/runs/{run_id}/inputs"
    client.post(endpoint, json=body())
    assert client.post(endpoint, json=body(input_id="input_second_123")).status_code == 409
    result = client.post(endpoint, json=body(input_id="input_second_123", expected_revision=1))
    assert result.status_code == 200
    assert result.json()["revision"] == 2


@pytest.mark.parametrize("background", [False, True])
def test_scope_workspace_and_terminal_guards(db, background):
    run_id = seed(db, background=background)
    endpoint = f"/api/chat/runs/{run_id}/inputs"
    assert client.post(endpoint, json=body(conversation_id="other_conversation")).status_code == 404
    assert client.get(endpoint, params={"conversation_id": "other_conversation"}).status_code == 404
    assert client.post(endpoint, json=body(workspace_revision=9)).status_code == 409
    model = TaskRecord if background else ForegroundRun
    run = db.get(model, run_id)
    run.status = "failed"
    db.commit()
    assert client.post(endpoint, json=body()).status_code == 409


def test_cancel_cas_and_idempotent_cancel(db):
    run_id = seed(db)
    endpoint = f"/api/chat/runs/{run_id}/inputs"
    client.post(endpoint, json=body(kind="next_turn"))
    assert client.delete(endpoint + "/input_stable_123", params={
        "conversation_id": "input_conversation", "expected_revision": 0}).status_code == 409
    result = client.delete(endpoint + "/input_stable_123", params={
        "conversation_id": "input_conversation", "expected_revision": 1})
    assert result.status_code == 200
    assert result.json()["input"]["status"] == "cancelled"
    assert client.delete(endpoint + "/input_stable_123", params={
        "conversation_id": "input_conversation", "expected_revision": 1}).status_code == 200


def test_application_is_atomic_with_goal_and_rollback(db):
    run_id = seed(db)
    original = ensure_run_goal(db, run_id, "input_conversation", "original")
    db.commit()
    run_inputs.submit_input(db, run_id, **body())
    db.commit()
    applied = run_inputs.consume_steering_inputs(db, run_id, "input_conversation", attempt_id="attempt-test")
    assert applied[0]["status"] == "applied"
    assert applied[0]["goal_revision"] > original["revision"]
    assert len(snapshot_goal(db, original["id"])["requirements"]) == len(original["requirements"])
    db.rollback()
    assert run_inputs.list_inputs(db, run_id, "input_conversation")["inputs"][0]["status"] == "queued"
    assert snapshot_goal(db, original["id"])["revision"] == original["revision"]
    applied = run_inputs.consume_steering_inputs(db, run_id, "input_conversation", attempt_id="attempt-test")
    db.commit()
    assert applied[0]["attempt_id"] == "attempt-test"
    assert run_inputs.consume_steering_inputs(db, run_id, "input_conversation") == []
    assert client.delete(f"/api/chat/runs/{run_id}/inputs/input_stable_123", params={
        "conversation_id": "input_conversation", "expected_revision": 2}).status_code == 409


def test_next_turn_never_claims_applied_or_starts_work(db):
    run_id = seed(db, background=True)
    run_inputs.submit_input(db, run_id, **body(kind="next_turn"))
    db.commit()
    assert run_inputs.consume_steering_inputs(db, run_id, "input_conversation") == []
    result = run_inputs.list_inputs(db, run_id, "input_conversation")
    assert result["inputs"][0]["status"] == "queued"
    assert result["next_turn_dispatch"] == "automatic"


@pytest.mark.parametrize("content", [" ", "\x00"])
def test_blank_or_control_content_is_rejected(db, content):
    run_id = seed(db)
    assert client.post(f"/api/chat/runs/{run_id}/inputs", json=body(content=content)).status_code == 422


def test_http_completion_commit_precedes_reception_without_late_queued_input(db, monkeypatch):
    run_id = seed(db)
    attempted, read_target = threading.Event(), threading.Event()
    original_target = run_inputs._target
    def observed_target(*args, **kwargs):
        read_target.set()
        return original_target(*args, **kwargs)
    monkeypatch.setattr(run_inputs, "_target", observed_target)
    responses = []
    def query_started(connection, cursor, statement, parameters, context, many):
        if statement.startswith("BEGIN IMMEDIATE") or statement.startswith("INSERT INTO run_input_queues"):
            attempted.set()
    with SessionLocal() as finalizer:
        finalizer.execute(text("BEGIN IMMEDIATE"))
        finalizer.get(ForegroundRun, run_id).status = "completed"
        finalizer.flush()
        event.listen(engine, "before_cursor_execute", query_started)
        thread = threading.Thread(target=lambda: responses.append(client.post(
            f"/api/chat/runs/{run_id}/inputs", json=body())), daemon=True)
        try:
            thread.start()
            assert attempted.wait(3)
            assert not read_target.is_set(), "run.status must not be read before acquiring the write lock"
            finalizer.commit()
            thread.join(timeout=4)
            assert not thread.is_alive()
        finally:
            finalizer.rollback()
            event.remove(engine, "before_cursor_execute", query_started)
            thread.join(timeout=4)
    assert responses[0].status_code == 409
    db.expire_all()
    assert db.scalar(select(run_inputs.RunInput.input_id)) is None


@pytest.mark.parametrize("phase", ["deliver", "completed"])
@pytest.mark.parametrize("kind", ["steer", "next_turn"])
def test_background_committed_delivery_rejects_new_inputs_but_replays_receipts(db, phase, kind):
    run_id = seed(db, background=True)
    endpoint = f"/api/chat/runs/{run_id}/inputs"
    assert client.post(endpoint, json=body(kind=kind)).status_code == 200
    task = db.get(TaskRecord, run_id)
    task.checkpoint = {"phase": phase}
    task.status = "running"  # Worker cleanup has not updated the UI projection.
    db.commit()
    assert client.post(endpoint, json=body(kind=kind)).status_code == 200
    assert client.post(endpoint, json=body(kind=kind, input_id="input_after_delivery", expected_revision=1)).status_code == 409
    assert db.query(run_inputs.RunInput).count() == 1
