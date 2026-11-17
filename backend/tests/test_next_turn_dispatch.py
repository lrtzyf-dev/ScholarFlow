"""Server dispatch contract with an in-process admission fake; no provider calls."""
import json
import threading
import time
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import next_turn_dispatch as dispatcher, run_inputs
from app.db import SessionLocal
from app.models import Conversation, ForegroundRun, ConversationOptions, ModelCredential, Paper
from app.model_profiles import save_profile, update_options
from app.timeutil import utcnow


CID = "dispatch_conversation"
PARENT = "chat_dispatch_parent_12345678"


def seed(db):
    db.add(Conversation(id=CID))
    db.add(ForegroundRun(id=PARENT, conversation_id=CID, question="original"))
    db.commit()


def enqueue(db, identity="input_dispatch_001", revision=0):
    value = run_inputs.submit_input(db, PARENT, input_id=identity, conversation_id=CID,
        kind="next_turn", content=identity, expected_revision=revision, workspace_revision=0)
    db.commit()
    return value


def complete(db, identity=PARENT, *, completed=True, status="completed"):
    row = db.get(ForegroundRun, identity, populate_existing=True)
    row.status, row.agent_checkpoint = status, {"result": {"completed": completed}}
    db.commit()


@pytest.fixture
def launches(monkeypatch):
    calls = []
    def launch(req):
        calls.append(req.run_id)
        with SessionLocal() as db:
            child = db.get(ForegroundRun, req.run_id)
            child.started_at = utcnow()
            db.commit()
    monkeypatch.setattr(dispatcher, "_launch", launch)
    return calls


def test_two_inputs_are_serial_and_survive_observer_absence(db, launches):
    seed(db)
    enqueue(db)
    enqueue(db, "input_dispatch_002", 1)
    assert dispatcher.dispatch_once() == 0
    complete(db)
    assert dispatcher.dispatch_once() == 1
    assert launches == [dispatcher.child_run_id("input_dispatch_001")]
    assert dispatcher.dispatch_once() == 0  # A live child blocks its successor.
    complete(db, launches[0])
    assert dispatcher.dispatch_once() == 1
    assert launches[1] == dispatcher.child_run_id("input_dispatch_002")
    with SessionLocal() as check:
        rows = check.scalars(select(run_inputs.RunInput).order_by(run_inputs.RunInput.created_at)).all()
        assert [row.status for row in rows] == ["dispatched", "dispatched"]


def test_repeat_pass_and_restart_do_not_repeat_admission(db, launches):
    seed(db)
    enqueue(db)
    complete(db)
    assert dispatcher.dispatch_once() == 1
    # Simulate a restarted dispatcher: it has only durable input/child rows.
    assert dispatcher.dispatch_once() == 0
    assert len(launches) == 1
    assert len(db.scalars(select(ForegroundRun)).all()) == 2


def test_same_clock_tick_and_status_revisions_cannot_reorder_inputs(db, launches, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(run_inputs, "utcnow", lambda: datetime(2026, 11, 15))
    seed(db)
    enqueue(db, "input_z_received_first")
    enqueue(db, "input_a_received_second", 1)
    assert dispatcher.dispatch_once() == 0  # Updates first row's status revision.
    complete(db)
    assert dispatcher.dispatch_once() == 1
    assert launches == [dispatcher.child_run_id("input_z_received_first")]


@pytest.mark.parametrize("status,completed", [("completed", False), ("cancelled", False),
    ("paused", False), ("failed", False), ("interrupted", False)])
def test_partial_stopped_and_failed_parents_retain_queue(db, launches, status, completed):
    seed(db)
    enqueue(db)
    complete(db, completed=completed, status=status)
    assert dispatcher.dispatch_once() == 0
    db.expire_all()
    row = db.get(run_inputs.RunInput, "input_dispatch_001")
    assert row.status == "queued" and row.reason
    assert not launches


def test_cancelled_input_never_launches(db, launches):
    seed(db)
    enqueue(db)
    run_inputs.cancel_input(db, PARENT, "input_dispatch_001", CID, 1)
    db.commit()
    complete(db)
    assert dispatcher.dispatch_once() == 0
    assert not launches


def test_unapplied_last_moment_steering_prevents_next_turn_overtaking(db, launches):
    seed(db)
    run_inputs.submit_input(db, PARENT, input_id="input_late_steering", conversation_id=CID,
        kind="steer", content="late correction", expected_revision=0, workspace_revision=0)
    db.commit()
    enqueue(db, revision=1)
    complete(db)
    assert dispatcher.dispatch_once() == 0
    db.expire_all()
    row = db.get(run_inputs.RunInput, "input_dispatch_001")
    assert row.status == "queued" and "尚未生效" in row.reason
    queue = db.get(run_inputs.RunInputQueue, PARENT)
    run_inputs.cancel_input(db, PARENT, "input_late_steering", CID, queue.revision)
    db.commit()
    assert dispatcher.dispatch_once() == 1


@pytest.mark.parametrize("change", ["archive", "workspace", "permission", "model"])
def test_changed_scope_or_settings_requires_review(db, launches, change):
    seed(db)
    enqueue(db)
    complete(db)
    if change == "archive":
        db.get(Conversation, CID).archived_at = utcnow()
    elif change == "workspace":
        db.get(Conversation, CID).workspace_revision = 1
    elif change == "permission":
        options = db.get(ConversationOptions, CID)
        options.approval_mode, options.revision = "always", options.revision + 1
    else:
        profile = save_profile(db, {"name": "offline model", "base_url": "https://example.test/v1",
                                  "model_id": "frozen-test", "api_key": ""})
        options = db.get(ConversationOptions, CID)
        update_options(db, CID, {"model_profile_id": profile.id}, options.revision)
    db.commit()
    assert dispatcher.dispatch_once() == 0
    db.expire_all()
    row = db.get(run_inputs.RunInput, "input_dispatch_001")
    assert row.status == "queued" and row.reason
    assert not launches


def test_revoked_credentials_are_not_substituted_or_exposed(db, launches):
    seed(db)
    profile = save_profile(db, {"name": "offline model", "base_url": "https://example.test/v1",
        "model_id": "frozen-test", "api_key": "never-send-this-test-key"})
    update_options(db, CID, {"model_profile_id": profile.id}, 0)
    db.commit()
    result = enqueue(db)
    assert "never-send-this-test-key" not in json.dumps(result)
    row = db.get(run_inputs.RunInput, "input_dispatch_001")
    assert "api_key" not in row.runtime_config and row.runtime_config["credential_ref"]
    db.get(ModelCredential, row.runtime_config["credential_ref"]).revoked_at = utcnow()
    db.commit()
    complete(db)
    assert dispatcher.dispatch_once() == 0
    assert not launches


def test_uncertain_launch_is_recorded_and_never_replayed(db, monkeypatch):
    seed(db)
    enqueue(db)
    complete(db)
    calls = []
    def uncertain(req):
        calls.append(req.run_id)
        raise ConnectionError("lost local admission acknowledgement")
    monkeypatch.setattr(dispatcher, "_launch", uncertain)
    assert dispatcher.dispatch_once() == 0
    assert dispatcher.dispatch_once() == 0
    assert len(calls) == 1
    db.expire_all()
    row = db.get(run_inputs.RunInput, "input_dispatch_001")
    assert row.status == "dispatched" and row.child_run_id and "待核实" in row.reason


def test_definite_admission_rejection_keeps_input_cancellable(db, monkeypatch):
    seed(db)
    enqueue(db)
    complete(db)
    calls = []
    def rejected(req):
        calls.append(req.run_id)
        # Simulate a policy update between the dispatch transaction and admission.
        with SessionLocal() as changed:
            options = changed.get(ConversationOptions, CID)
            options.revision += 1
            options.approval_mode = "always"
            changed.commit()
        raise HTTPException(409, "policy changed before admission")
    monkeypatch.setattr(dispatcher, "_launch", rejected)
    assert dispatcher.dispatch_once() == 0
    assert dispatcher.dispatch_once() == 0
    assert len(calls) == 1
    db.expire_all()
    row = db.get(run_inputs.RunInput, "input_dispatch_001")
    assert row.status == "queued" and row.child_run_id is None
    assert db.get(ForegroundRun, dispatcher.child_run_id(row.input_id)) is None
    assert run_inputs.cancel_input(db, PARENT, row.input_id, CID,
        db.get(run_inputs.RunInputQueue, PARENT).revision)["input"]["status"] == "cancelled"


@pytest.mark.parametrize("change", ["permission", "model", "paper"])
def test_admission_rechecks_binding_changed_after_dispatch_commit(db, monkeypatch, change):
    """No provider request can slip through dispatch's post-commit launch gap."""
    seed(db)
    if change == "paper":
        db.add(Paper(id="dispatch_source", title="Frozen source", pdf_path="unused-test.pdf",
                     content_hash="source-before", status="pending"))
        db.get(ForegroundRun, PARENT).paper_id = "dispatch_source"
        db.commit()
    identity = f"input_admission_change_{change}"
    enqueue(db, identity)
    complete(db)
    original_launch = dispatcher._launch
    calls = []
    def change_then_launch(req):
        calls.append(req.run_id)
        with SessionLocal() as changed:
            options = changed.get(ConversationOptions, CID)
            if change == "permission":
                options.approval_mode, options.revision = "always", options.revision + 1
            elif change == "model":
                profile = save_profile(changed, {"name": "Changed model", "base_url": "https://example.test/v1",
                                                "model_id": "changed", "api_key": ""})
                update_options(changed, CID, {"model_profile_id": profile.id}, options.revision)
            else:
                changed.get(Paper, "dispatch_source").content_hash = "source-after"
            changed.commit()
        return original_launch(req)
    monkeypatch.setattr(dispatcher, "_launch", change_then_launch)
    assert dispatcher.dispatch_once() == 0
    assert dispatcher.dispatch_once() == 0
    assert len(calls) == 1
    db.expire_all()
    row = db.get(run_inputs.RunInput, identity)
    assert row.status == "queued" and row.child_run_id is None
    assert db.get(ForegroundRun, dispatcher.child_run_id(identity)) is None


def test_get_is_read_only_even_when_parent_is_complete(db, launches):
    seed(db)
    enqueue(db)
    complete(db)
    for _ in range(3):
        result = run_inputs.list_inputs(db, PARENT, CID)
        assert result["inputs"][0]["status"] == "queued"
    assert not launches


def test_real_worker_admission_and_observer_drain_chain_without_browser(db, monkeypatch):
    """Exercise chat_stream's actual independent worker using a local generator."""
    from app.api import chat as chat_api
    from app import chat_events, run_store, run_lifecycle
    from app.runtime_models import RunAttempt
    seed(db)
    enqueue(db, "input_real_worker_001")
    enqueue(db, "input_real_worker_002", 1)
    complete(db)
    calls, ready, release = [], threading.Event(), threading.Event()
    def controlled_agent(worker_db, question, paper_id, **options):
        identity = options["run_id"]
        calls.append(identity)
        yield "thinking_delta", {"text": "public thinking"}
        yield "answer_delta", {"text": "visible output"}
        if len(calls) == 1:
            ready.set()
            assert release.wait(5)
        attempt = run_lifecycle.current_attempt(worker_db, identity)
        result = {"kind": "answer", "answer": question, "completed": True,
                  "outcome": "completed", "evidence": []}
        foreground = worker_db.get(ForegroundRun, identity)
        foreground.status = "completed"
        options["save_checkpoint"]({"phase": "completed", "result": result,
            "attempt_id": attempt.id, "writer_token": attempt.writer_token})
        yield "answer", result
        yield "done", {}
    monkeypatch.setattr(chat_api, "run_agent", controlled_agent)
    first, second = dispatcher.child_run_id("input_real_worker_001"), dispatcher.child_run_id("input_real_worker_002")
    try:
        assert dispatcher.dispatch_once() == 1
        assert ready.wait(3)
        snapshot = chat_events.get(first).snapshot()
        assert snapshot["partial_answer"] == "visible output"
        assert snapshot["thinking"] == "public thinking"
        assert dispatcher.dispatch_once() == 0
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while first in run_store.active_runs() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert first not in run_store.active_runs()
    assert dispatcher.dispatch_once() == 1
    deadline = time.monotonic() + 5
    while second in run_store.active_runs() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert second not in run_store.active_runs()
    assert dispatcher.dispatch_once() == 0
    assert calls == [first, second]
    with SessionLocal() as check:
        assert len(check.scalars(select(RunAttempt).where(RunAttempt.run_id.in_((first, second)))).all()) == 2


def test_http_inbox_and_lifespan_dispatch_two_turns_without_polling_browser(db, monkeypatch):
    from app.api import chat as chat_api
    from app.api.run_inputs import router as inputs_router
    from app import run_lifecycle, run_store
    seed(db)
    calls = []
    def controlled_agent(worker_db, question, paper_id, **options):
        identity = options["run_id"]
        calls.append(identity)
        yield "answer_delta", {"text": "HTTP child output"}
        attempt = run_lifecycle.current_attempt(worker_db, identity)
        result = {"kind": "answer", "answer": question, "completed": True, "evidence": []}
        worker_db.get(ForegroundRun, identity).status = "completed"
        options["save_checkpoint"]({"phase": "completed", "result": result,
            "attempt_id": attempt.id, "writer_token": attempt.writer_token})
        yield "answer", result
        yield "done", {}
    monkeypatch.setattr(chat_api, "run_agent", controlled_agent)
    @asynccontextmanager
    async def lifecycle(_):
        dispatcher.start()
        try:
            yield
        finally:
            dispatcher.stop()
    app = FastAPI(lifespan=lifecycle)
    app.include_router(inputs_router)
    app.include_router(chat_api.router)
    endpoint = f"/api/chat/runs/{PARENT}/inputs"
    with TestClient(app) as client:
        for number in (1, 2):
            for retry in range(4):
                queue = client.get(endpoint, params={"conversation_id": CID}).json()
                response = client.post(endpoint, json={"input_id": f"input_http_chain_00{number}",
                    "conversation_id": CID, "kind": "next_turn", "content": f"HTTP follow-up {number}",
                    "workspace_revision": 0, "expected_revision": queue["revision"]})
                if response.status_code == 200:
                    break
                assert response.status_code == 409 and retry < 3
            assert response.json()["input"]["status"] == "queued"
        assert calls == []
        complete(db)
        dispatcher.wake()  # Same post-commit signal as normal foreground completion.
        deadline = time.monotonic() + 8
        while (len(calls) < 2 or any(identity in run_store.active_runs() for identity in calls)) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == [dispatcher.child_run_id("input_http_chain_001"),
                         dispatcher.child_run_id("input_http_chain_002")]
        rows = client.get(endpoint, params={"conversation_id": CID}).json()["inputs"]
        assert [row["status"] for row in rows] == ["dispatched", "dispatched"]
        # Re-entering after execution only reads the original child identity.
        replay = client.get(f"/api/chat/runs/{calls[0]}/events", params={"conversation_id": CID})
        assert replay.status_code == 200 and 'HTTP follow-up 1' in replay.text
        assert len(calls) == 2
