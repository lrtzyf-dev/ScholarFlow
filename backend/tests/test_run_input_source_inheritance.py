"""API contract for inheriting a run's frozen source authorization on steering."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.run_inputs import router
from app.models import Conversation, ForegroundRun, TaskRecord


app = FastAPI()
app.include_router(router)
client = TestClient(app)

SOURCE_A = "a" * 32
SOURCE_B = "b" * 32


def _seed(db, *, background: bool):
    conversation_id = "source_inherit_conversation"
    run_id = "task_source_inherit_123" if background else "chat_source_inherit_123"
    db.add(Conversation(id=conversation_id, workspace_revision=0))
    runtime_config = {"source_revision_ids": [SOURCE_A], "source_bindings": []}
    if background:
        run = TaskRecord(
            id=run_id,
            type="agent",
            conversation_id=conversation_id,
            status="running",
            payload={"question": "original", "workspace": {"workspace_revision": 0}},
            runtime_config=runtime_config,
        )
    else:
        run = ForegroundRun(
            id=run_id,
            conversation_id=conversation_id,
            question="original",
            status="running",
            workspace={"workspace_revision": 0},
            runtime_config=runtime_config,
        )
    db.add(run)
    db.commit()
    return run_id, conversation_id


def _payload(conversation_id, input_id="source_input_123"):
    return {
        "input_id": input_id,
        "conversation_id": conversation_id,
        "kind": "steer",
        "content": "沿用固定来源继续",
        "expected_revision": 0,
        "workspace_revision": 0,
    }


@pytest.mark.parametrize("background", [False, True])
def test_steering_source_scope_inherits_when_omitted_or_null(db, monkeypatch, background):
    """Missing/null means inherit; an explicit empty list remains a clear operation."""
    from app import source_runtime

    monkeypatch.setattr(
        source_runtime,
        "freeze_sources",
        lambda _db, _conversation_id, ids, binding: {
            **(binding or {}),
            "source_revision_ids": list(ids),
            "source_bindings": [],
        },
    )
    run_id, conversation_id = _seed(db, background=background)
    endpoint = f"/api/chat/runs/{run_id}/inputs"

    inherited = client.post(endpoint, json=_payload(conversation_id))
    assert inherited.status_code == 200
    assert inherited.json()["input"]["source_revision_ids"] == [SOURCE_A]

    # A distinct input proves null is interpreted at submission time, rather
    # than being confused with the already stored idempotency receipt.
    db.rollback()
    null_payload = _payload(conversation_id, input_id="source_input_null")
    null_payload["expected_revision"] = 1
    null_payload["source_revision_ids"] = None
    null_result = client.post(endpoint, json=null_payload)
    assert null_result.status_code == 200
    assert null_result.json()["input"]["source_revision_ids"] == [SOURCE_A]


@pytest.mark.parametrize("background", [False, True])
def test_steering_source_scope_explicit_clear_and_replay_are_stable(db, monkeypatch, background):
    from app import source_runtime

    monkeypatch.setattr(
        source_runtime,
        "freeze_sources",
        lambda _db, _conversation_id, ids, binding: {
            **(binding or {}),
            "source_revision_ids": list(ids),
            "source_bindings": [],
        },
    )
    run_id, conversation_id = _seed(db, background=background)
    endpoint = f"/api/chat/runs/{run_id}/inputs"
    payload = _payload(conversation_id)

    # The first omitted request freezes the run's original authorization.
    first = client.post(endpoint, json=payload)
    assert first.status_code == 200
    assert first.json()["input"]["source_revision_ids"] == [SOURCE_A]

    # Replaying the same omitted request must match its durable receipt.
    replay = client.post(endpoint, json=payload)
    assert replay.status_code == 200
    assert replay.json()["input"]["source_revision_ids"] == [SOURCE_A]

    # Explicit [] is a separate, intentional clear operation.
    clear = _payload(conversation_id, input_id="source_input_clear")
    clear["expected_revision"] = 1
    clear["source_revision_ids"] = []
    clear_result = client.post(endpoint, json=clear)
    assert clear_result.status_code == 200
    assert clear_result.json()["input"]["source_revision_ids"] == []
    assert client.post(endpoint, json=clear).status_code == 200

    # Reusing an input ID with a different explicit source scope remains a conflict.
    changed = dict(payload)
    changed["source_revision_ids"] = [SOURCE_B]
    assert client.post(endpoint, json=changed).status_code == 409


def test_steering_source_scope_rejects_non_list_values(db):
    run_id, conversation_id = _seed(db, background=False)
    payload = _payload(conversation_id)
    payload["source_revision_ids"] = SOURCE_A
    response = client.post(f"/api/chat/runs/{run_id}/inputs", json=payload)
    assert response.status_code == 422

