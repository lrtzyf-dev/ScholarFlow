"""Exact returned-unknown review and single-use dispatch grants, never fake proof."""
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.agent.checkpoint import AgentState
from app.models import Conversation, DeletedConversation, ForegroundRun, TaskRecord
from app.runtime_models import RuntimeRun
from app.operation_receipts import signature, consume_retry, retry_permission
from app.db import SessionLocal


ATTEMPT = "a" * 32
RID = "chat_operation_review"
CID = "operation-review"
KEY = "tool/0/0"
NAME = "mcp__custom__write"
ARGS = {"record": "fixed target", "api_key": "fixture-secret"}


def seed(db, kind="foreground"):
    sig = signature(NAME, ARGS)
    failure = {"id": "failure/" + KEY, "operation_key": KEY, "signature": sig, "tool_name": NAME,
        "arguments": ARGS, "phase": "write", "status": "open", "execution_unknown": True,
        "error": "Reply lost", "alternatives": []}
    state = AgentState(question="Write once", conversation_id=CID, phase="model", attempt_id=ATTEMPT,
        writer_token="old-writer", runtime_config={"conversation_id": CID},
        tool_failures=[failure], memory_side_effects={sig: {"operation_key": KEY, "execution_unknown": True, "user_input_count": 0}},
        operations={KEY: {"executed": True, "result": '{"execution_unknown":true,"error":"Reply lost"}'}},
        messages=[{"role": "system", "content": "private-system"}])
    db.add_all([Conversation(id=CID), RuntimeRun(id=RID, kind=kind, conversation_id=CID,
        current_attempt_id=ATTEMPT, status="interrupted", revision=5)])
    if kind == "foreground":
        db.add(ForegroundRun(id=RID, conversation_id=CID, question=state.question,
            status="interrupted", agent_checkpoint=state.snapshot()))
    else:
        db.add(TaskRecord(id=RID, conversation_id=CID, title=state.question, type="agent", status="failed", checkpoint=state.snapshot()))
    db.commit()
    return state


def form(action="not_executed"):
    return {"request_id": "unique-reconciliation", "expected_parent_revision": 5,
        "expected_attempt_id": ATTEMPT, "operation_key": KEY, "signature": signature(NAME, ARGS),
        "action": action, "reference": "Manually inspected request history; fabricated test record"}


def read_state(db, kind="foreground"):
    db.expire_all()
    row = db.get(ForegroundRun if kind == "foreground" else TaskRecord, RID)
    return AgentState.model_validate(row.agent_checkpoint if kind == "foreground" else row.checkpoint)


@pytest.mark.parametrize("kind", ["foreground", "background"])
@pytest.mark.parametrize("action", ["executed", "not_executed", "unknown"])
def test_same_run_reconciliation_is_immutable_idempotent_and_never_provider_proof(db, kind, action):
    before = seed(db, kind)
    client = TestClient(app); url = f"/api/subtasks/runs/{RID}/operations"
    view = client.get(url)
    assert view.status_code == 200 and view.json()["can_reconcile"]
    assert "fixture-secret" not in view.text and "private-system" not in view.text and "old-writer" not in view.text
    reply = client.post(url + "/reconcile", json=form(action))
    assert reply.status_code == 200, reply.text
    assert reply.json()["provider_verified"] is False
    assert client.post(url + "/reconcile", json=form(action)).json()["id"] == reply.json()["id"]
    assert client.post(url + "/reconcile", json={**form(action), "reference": "Different declaration"}).status_code == 409
    after = read_state(db, kind)
    assert after.operations == before.operations and after.todo == before.todo
    assert after.tool_failures[0]["status"] == "open" and after.tool_failures[0]["execution_unknown"]
    assert len(after.tool_failures[0]["reconciliations"]) == 1
    assert after.memory_side_effects == before.memory_side_effects
    assert bool(retry_permission(after, signature(NAME, ARGS))) is (action == "not_executed")


@pytest.mark.parametrize("invalid", ["revision", "attempt", "signature", "operation", "running", "in_process", "archived", "deleted", "pending"])
def test_changed_or_inactive_scope_never_receives_reconciliation(db, monkeypatch, invalid):
    seed(db); body = form()
    if invalid == "revision": body["expected_parent_revision"] -= 1
    elif invalid == "attempt": body["expected_attempt_id"] = "b" * 32
    elif invalid == "signature": body["signature"] = "f" * 64
    elif invalid == "operation": body["operation_key"] = "tool/9/9"
    elif invalid == "running": db.get(RuntimeRun, RID).status = "running"
    elif invalid == "in_process": monkeypatch.setattr("app.run_store.active_runs", lambda: {RID: CID})
    elif invalid == "archived":
        from app.timeutil import utcnow
        db.get(Conversation, CID).archived_at = utcnow()
    elif invalid == "deleted": db.add(DeletedConversation(id=CID))
    elif invalid == "pending":
        row = db.get(ForegroundRun, RID)
        row.agent_checkpoint = {**row.agent_checkpoint, "pending_operation": {"kind": "tool", "key": "tool/1/0", "name": NAME, "arguments": {}}}
    db.commit()
    reply = TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json=body)
    assert reply.status_code == (410 if invalid == "deleted" else 409), reply.text
    assert not read_state(db).tool_failures[0].get("reconciliations")


def test_single_use_retry_consumes_before_dispatch_and_does_not_revive_after_restore(db):
    seed(db)
    reply = TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json=form())
    assert reply.status_code == 200
    state = read_state(db)
    sig = signature(NAME, ARGS)
    # Actual pending intent is written first by Checkpointer.perform().
    state.pending_operation = {"key": "tool/1/0", "kind": "tool", "name": NAME, "arguments": ARGS}
    persisted = []
    assert consume_retry(state, sig, "tool/1/0", lambda: persisted.append(state.snapshot()))
    assert persisted[-1]["pending_operation"] and persisted[-1]["tool_failures"][0]["retry_consumptions"]
    restored = AgentState.model_validate(persisted[-1])
    assert not retry_permission(restored, sig)
    assert not consume_retry(restored, sig, "tool/2/0", lambda: pytest.fail("Consumed permission was persisted again"))
    assert not retry_permission(state, signature(NAME, {**ARGS, "record": "other target"}))
    assert len(restored.tool_failures[0]["reconciliations"]) == 1


def test_retry_grant_can_only_follow_the_declared_original_attempt(db):
    seed(db)
    assert TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json=form()).status_code == 200
    state = read_state(db); state.attempt_id = "b" * 32
    assert retry_permission(state, signature(NAME, ARGS)) is None
    state.source_attempt_id = ATTEMPT
    assert retry_permission(state, signature(NAME, ARGS))


@pytest.mark.parametrize("invalid", ["missing", "wrong_key", "wrong_arguments", "wrong_name"])
def test_retry_cannot_consume_permission_without_the_exact_saved_pending_intent(db, invalid):
    seed(db)
    assert TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json=form()).status_code == 200
    state = read_state(db)
    state.pending_operation = {"kind": "tool", "key": "tool/1/0", "name": NAME, "arguments": ARGS}
    if invalid == "missing": state.pending_operation = None
    elif invalid == "wrong_key": state.pending_operation["key"] = "tool/2/0"
    elif invalid == "wrong_arguments": state.pending_operation["arguments"] = {"record": "other target"}
    else: state.pending_operation["name"] = "mcp__custom__other"
    assert not consume_retry(state, signature(NAME, ARGS), "tool/1/0", lambda: pytest.fail("Invalid pending intent consumed permission"))
    assert not state.tool_failures[0].get("retry_consumptions")


def test_reference_cannot_store_credentials(db):
    seed(db)
    reply = TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json={**form(), "reference": "api_key=not-for-report"})
    assert reply.status_code == 422
    assert not read_state(db).tool_failures[0].get("reconciliations")
