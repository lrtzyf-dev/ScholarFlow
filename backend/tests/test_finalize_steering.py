"""Final delivery and input reception serialize without losing accepted steering."""
from copy import deepcopy
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import run_inputs, run_lifecycle
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.db import SessionLocal, engine
from app.foreground_run_store import save_agent_checkpoint
from app.llm import LLMResponse
from app.main import app
from app.models import ForegroundRun, QARecord, TaskRecord
from app.run_inputs import RunInput
from app.runtime_models import RunAttempt


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **kw: pytest.fail("finalization correction must not repeat a tool/write"))


def seed(db, *, evidence=False, owner="foreground"):
    rid, cid = f"{owner}_finalize_steering", "c_finalize_steering"
    attempt, _ = run_lifecycle.admit_attempt(db, rid, cid, "Explain")
    state = AgentState(question="Explain", conversation_id=cid, phase="finalize", answer="Old draft" + ("[^e1]." if evidence else "."),
        messages=[{"role": "user", "content": "Explain"}],
        runtime_config={"version": 1, "mode": "mock", "model_id": "Synthetic", "approval_mode": "full_access"},
        operations={"tool/0/0": {"result": "already completed", "executed": True}},
        usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        evidence=[{"id": "e1", "quote": "Result", "page": 1, "paper_title": "Synthetic"}] if evidence else [])
    from app.memory_governance import fingerprint
    state.memory_fingerprint = fingerprint(db, cid)
    snapshot = run_lifecycle.bind_checkpoint(state.snapshot(), attempt)
    if owner == "foreground":
        db.add(ForegroundRun(id=rid, conversation_id=cid, question="Explain", skills=[], images=[],
                            status="running", phase="finalize", agent_checkpoint=snapshot, runtime_config=state.runtime_config))
    else:
        db.add(TaskRecord(id=rid, conversation_id=cid, type="agent", title="Explain",
                          status="running", checkpoint=snapshot, runtime_config=state.runtime_config,
                          payload={"question": "Explain", "skills": []}))
    db.commit()
    return rid, cid, snapshot


def request(cid, *, text="请改用中文", input_id="finalize_steer_001", revision=0):
    return {"input_id": input_id, "conversation_id": cid, "kind": "steer", "content": text,
            "expected_revision": revision, "workspace_revision": 0}


def model(monkeypatch, *, evidence=False):
    seen = []

    class Model:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            yield "delta", "answer", "新答案"
            yield "final", LLMResponse("新答案" + ("[^e1]." if evidence else "。"),
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    return seen


def execute_thread(rid, cid, checkpoint, *, persist_hook=None, owner="foreground"):
    errors, events = [], []

    def drive():
        try:
            with SessionLocal() as session:
                def persist(snapshot):
                    if persist_hook:
                        persist_hook(snapshot)
                    if owner == "foreground":
                        save_agent_checkpoint(session, rid, snapshot)
                    else:
                        # Match TaskRunner's saver while deliberately leaving its
                        # status running to exercise the pre-cleanup interval.
                        run_lifecycle.save_checkpoint(session, rid, snapshot)
                        task = session.get(TaskRecord, rid)
                        task.checkpoint = deepcopy(snapshot)
                        task.result = deepcopy(snapshot.get("result"))
                        session.commit()
                events.extend(loop.run_agent(session, "Explain", conversation_id=cid, run_id=rid,
                    checkpoint=deepcopy(checkpoint), save_checkpoint=persist))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=drive, name="finalize-test-executor", daemon=True)
    thread.start()
    return thread, events, errors


def assert_replanned(db, rid, checkpoint, events, seen, *, owner="foreground"):
    db.expire_all()
    row = db.get(ForegroundRun if owner == "foreground" else TaskRecord, rid)
    saved = row.agent_checkpoint if owner == "foreground" else row.checkpoint
    assert len(seen) == 1 and seen[0][-1]["content"] == "请改用中文"
    assert row.status == ("completed" if owner == "foreground" else "running")
    assert saved["phase"] == "completed"
    assert saved["attempt_id"] == checkpoint["attempt_id"]
    assert saved["record_id"] == checkpoint["record_id"]
    assert saved["operations"]["tool/0/0"] == checkpoint["operations"]["tool/0/0"]
    assert saved["applied_input_ids"] == ["finalize_steer_001"]
    assert db.query(RunAttempt).count() == 1 and db.query(QARecord).count() == 1
    assert db.query(QARecord).one().answer.startswith("新答案")
    assert db.query(QARecord).one().usage["total_tokens"] == 15
    assert len([event for kind, event in events if kind == "run_input"]) == 1
    assert db.query(RunInput).one().status == "applied"


@pytest.mark.parametrize("owner", ["foreground", "background"])
def test_finalize_without_file_write_consumes_input_once_and_keeps_attempt_qa_and_usage(db, monkeypatch, owner):
    rid, cid, checkpoint = seed(db, owner=owner)
    seen = model(monkeypatch)
    client = TestClient(app)
    payload = request(cid)
    assert client.post(f"/api/chat/runs/{rid}/inputs", json=payload).status_code == 200
    thread, events, errors = execute_thread(rid, cid, checkpoint, owner=owner)
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert_replanned(db, rid, checkpoint, events, seen, owner=owner)
    duplicate = client.post(f"/api/chat/runs/{rid}/inputs", json=payload)
    assert duplicate.status_code == 200 and duplicate.json()["input"]["status"] == "applied"
    assert db.query(RunInput).count() == 1 and len(seen) == 1


@pytest.mark.parametrize("owner", ["foreground", "background"])
def test_input_received_during_final_audit_returns_to_model_without_delivering_old_draft(db, monkeypatch, owner):
    rid, cid, checkpoint = seed(db, evidence=True, owner=owner)
    seen = model(monkeypatch, evidence=True)
    audit_ready, release = threading.Event(), threading.Event()
    audits = []
    monkeypatch.setattr(loop, "citation_gate", lambda *a, **kw: {"passed": True, "grounded_ratio": 1.0,
        "unsupported": [], "invalid_markers": [], "total_claims": 1, "marked_claims": 1})

    def audit(*args, **kw):
        audits.append(True)
        if len(audits) == 1:
            audit_ready.set()
            assert release.wait(5)
        return {"passed": True, "status": "passed", "mode": "test", "unsupported": []}

    monkeypatch.setattr(loop, "audit_answer", audit)
    thread, events, errors = execute_thread(rid, cid, checkpoint, owner=owner)
    try:
        assert audit_ready.wait(3)
        received = TestClient(app).post(f"/api/chat/runs/{rid}/inputs", json=request(cid))
        assert received.status_code == 200
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors and len(audits) == 2
    assert_replanned(db, rid, checkpoint, events, seen, owner=owner)
    assert [event["answer"] for kind, event in events if kind == "answer"] == ["新答案[^e1]."]


@pytest.mark.parametrize("owner", ["foreground", "background"])
def test_completion_transaction_wins_then_input_post_returns_conflict_without_queued_row(db, monkeypatch, owner):
    rid, cid, checkpoint = seed(db, owner=owner)
    seen = model(monkeypatch)
    delivery_ready, release, post_attempted = threading.Event(), threading.Event(), threading.Event()
    replies = []

    def before_commit(snapshot):
        if snapshot["phase"] == "deliver":
            delivery_ready.set()
            assert release.wait(5)

    executor, events, errors = execute_thread(rid, cid, checkpoint, persist_hook=before_commit, owner=owner)
    assert delivery_ready.wait(3)

    def post():
        replies.append(TestClient(app).post(f"/api/chat/runs/{rid}/inputs", json=request(cid)))

    def before_query(connection, cursor, statement, parameters, context, many):
        if statement.startswith("BEGIN IMMEDIATE"):
            post_attempted.set()

    event.listen(engine, "before_cursor_execute", before_query)
    receiver = threading.Thread(target=post, daemon=True)
    receiver.start()
    try:
        assert post_attempted.wait(3)
        assert not replies, "input reception must wait behind the final delivery transaction"
    finally:
        release.set()
        executor.join(5)
        receiver.join(5)
        event.remove(engine, "before_cursor_execute", before_query)
    assert not errors and not executor.is_alive() and not receiver.is_alive()
    assert replies[0].status_code == 409 and not seen
    db.expire_all()
    assert db.query(RunInput).count() == 0 and db.query(QARecord).count() == 1
    assert db.query(QARecord).one().answer == "Old draft."


@pytest.mark.parametrize("owner", ["foreground", "background"])
def test_input_transaction_wins_then_final_delivery_consumes_it_instead_of_losing_it(db, monkeypatch, owner):
    rid, cid, checkpoint = seed(db, owner=owner)
    seen = model(monkeypatch)
    input_ready, release, final_waiting = threading.Event(), threading.Event(), threading.Event()
    replies = []
    original_submit, original_assert = run_inputs.submit_input, run_lifecycle.assert_writer

    def held_submit(*args, **kw):
        result = original_submit(*args, **kw)
        input_ready.set()
        assert release.wait(5)
        return result

    def detect_final_fence(*args, **kw):
        if kw.get("lock") and threading.current_thread().name == "finalize-test-executor":
            final_waiting.set()
        return original_assert(*args, **kw)

    monkeypatch.setattr(run_inputs, "submit_input", held_submit)
    monkeypatch.setattr(run_lifecycle, "assert_writer", detect_final_fence)
    receiver = threading.Thread(target=lambda: replies.append(TestClient(app).post(
        f"/api/chat/runs/{rid}/inputs", json=request(cid))), daemon=True)
    receiver.start()
    assert input_ready.wait(3)
    executor, events, errors = execute_thread(rid, cid, checkpoint, owner=owner)
    try:
        assert final_waiting.wait(3)
    finally:
        release.set()
        receiver.join(5)
        executor.join(5)
    assert replies[0].status_code == 200 and not errors
    assert not executor.is_alive() and not receiver.is_alive()
    assert_replanned(db, rid, checkpoint, events, seen, owner=owner)
