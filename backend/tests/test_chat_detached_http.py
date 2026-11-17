"""Real local HTTP observers may detach without cancelling an admitted turn."""
from contextlib import contextmanager
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app import approval_store, cancel_store, run_store
from app.agent import loop
from app.api import chat
from app.db import SessionLocal
from app.llm import LLMResponse
from app.main import app
from app.models import ForegroundRun, QARecord


@pytest.fixture(autouse=True)
def isolated_runs(monkeypatch):
    monkeypatch.setattr(run_store, "_active", {})
    monkeypatch.setattr(run_store, "_finished", {})
    monkeypatch.setattr(run_store, "_early_cancels", {})
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(approval_store, "_pending", {})


def wait_for(predicate, message, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail(message)


@contextmanager
def local_server():
    # No application lifespan: this test only owns a temporary DB and HTTP
    # server, never the desktop runtime owner, tracking or the user's services.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started, "local HTTP server did not start")
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "local HTTP server did not shut down"


def create_run(client, run_id, cid, question):
    response = client.post("/api/foreground-runs", json={
        "id": run_id, "conversation_id": cid, "question": question,
    })
    assert response.status_code == 200, response.text


def get_run(client, cid):
    response = client.get("/api/foreground-runs", params={"conversation_id": cid})
    assert response.status_code == 200, response.text
    return next(iter(response.json()["runs"]), None)


def test_http_disconnect_keeps_progress_and_commits_exactly_one_answer(monkeypatch):
    run_id, cid, question = "chat_detached_http_001", "c_detached_http", "Finish this isolated turn"
    continue_generation, later_progress, finish_generation = (threading.Event() for _ in range(3))
    first, second = "BEFORE_DETACH", "AFTER_DETACH"
    model_calls = []

    class Model:
        def stream_chat(self, *_args, **_kwargs):
            model_calls.append(True)
            yield "delta", "answer", first
            assert continue_generation.wait(5), "test did not release detached generation"
            yield "delta", "answer", second
            later_progress.set()
            assert finish_generation.wait(5), "test did not release final answer"
            yield "final", LLMResponse(content=first + second)

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "make_plan", lambda *_args, **_kwargs: [])

    with local_server() as client:
        try:
            create_run(client, run_id, cid, question)
            with client.stream("POST", "/api/chat/stream", json={
                "question": question, "conversation_id": cid, "run_id": run_id,
            }) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if first in line:
                        break
                else:
                    pytest.fail("initial answer delta was not streamed")
            # The network response is now closed, just as when the view changes.
            continue_generation.set()
            assert later_progress.wait(5)
            partial = wait_for(
                lambda: (run if (run := get_run(client, cid)) and run["partial_answer"] == first + second else None),
                "server did not save output produced after the observer disconnected",
            )
            assert partial["status"] == "running"
            assert not cancel_store.is_cancelled(run_id)
            assert run_id in run_store.active_runs()
            assert client.delete(f"/api/conversations/{cid}").status_code == 409
            stale = client.patch(f"/api/foreground-runs/{run_id}", json={
                "revision": partial["revision"], "partial_answer": "old browser data", "status": "interrupted",
            })
            assert stale.status_code == 409
            assert stale.json()["detail"]["current"]["partial_answer"] == first + second
            finish_generation.set()
            wait_for(lambda: run_id not in run_store.active_runs(), "detached executor did not complete")
            history = client.get(f"/api/conversations/{cid}").json()["messages"]
            assert len(history) == 1
            assert history[0]["answer"] == first + second
            assert model_calls == [True], "detaching must not launch a new generation"
            assert get_run(client, cid) is None
            with SessionLocal() as db:
                assert db.get(ForegroundRun, run_id).status == "completed"
                assert db.query(QARecord).filter(QARecord.conversation_id == cid).count() == 1
        finally:
            continue_generation.set()
            finish_generation.set()
            if run_id in run_store.active_runs():
                run_store.cancel(run_id)
                wait_for(lambda: run_id not in run_store.active_runs(), "test worker did not finish")


def test_http_explicit_stop_still_cancels_after_observer_disconnect(monkeypatch):
    run_id, cid, question = "chat_detached_http_stop", "c_detached_stop", "Wait for explicit stop"
    waiting = threading.Event()

    def fake_agent(*_args, **kwargs):
        yield "answer_delta", {"kind": "answer_delta", "text": "PARTIAL_BEFORE_STOP"}
        waiting.set()
        deadline = time.monotonic() + 5
        while not cancel_store.is_cancelled(kwargs["run_id"]):
            assert time.monotonic() < deadline, "explicit stop did not reach detached worker"
            time.sleep(0.01)
        yield "cancelled", {"kind": "cancelled", "text": "Stopped explicitly"}

    monkeypatch.setattr(chat, "run_agent", fake_agent)
    with local_server() as client:
        try:
            create_run(client, run_id, cid, question)
            with client.stream("POST", "/api/chat/stream", json={
                "question": question, "conversation_id": cid, "run_id": run_id,
            }) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if "PARTIAL_BEFORE_STOP" in line:
                        break
            assert waiting.wait(5)
            assert run_id in run_store.active_runs()
            assert not cancel_store.is_cancelled(run_id)
            stopped = client.post(f"/api/chat/runs/{run_id}/cancel")
            assert stopped.status_code == 200
            wait_for(lambda: run_id not in run_store.active_runs(), "detached worker ignored explicit stop")
            saved = get_run(client, cid)
            assert saved["status"] == "cancelled"
            assert saved["partial_answer"] == "PARTIAL_BEFORE_STOP"
            with SessionLocal() as db:
                assert db.query(QARecord).filter(QARecord.conversation_id == cid).count() == 0
        finally:
            if run_id in run_store.active_runs():
                run_store.cancel(run_id)
                wait_for(lambda: run_id not in run_store.active_runs(), "test worker did not finish")


def test_another_http_observer_can_approve_the_original_detached_turn(monkeypatch):
    run_id, cid, question = "chat_detached_http_approve", "c_detached_approval", "Read after approval"
    model_calls, executed = [], []

    class Model:
        def stream_chat(self, *_args, **_kwargs):
            model_calls.append(True)
            if len(model_calls) == 1:
                yield "delta", "answer", "TOOL_PREAMBLE_MUST_NOT_BECOME_ANSWER"
                yield "final", LLMResponse(content="TOOL_PREAMBLE_MUST_NOT_BECOME_ANSWER", tool_calls=[{
                    "id": "synthetic_external_read", "name": "mcp__synthetic__guarded_read", "arguments": {},
                }])
            else:
                yield "delta", "answer", "APPROVED_FINAL_ANSWER"
                yield "final", LLMResponse(content="APPROVED_FINAL_ANSWER")

    def execute(_db, name, _arguments, **_kwargs):
        executed.append(name)
        return "{}"

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "make_plan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(loop, "execute_tool", execute)
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{
        "name": "mcp__synthetic__guarded_read", "parameters": {"type": "object", "properties": {}},
    }])
    with local_server() as client:
        try:
            create_run(client, run_id, cid, question)
            with client.stream("POST", "/api/chat/stream", json={
                "question": question, "conversation_id": cid, "run_id": run_id,
            }) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if "event: approval_request" in line:
                        break
                else:
                    pytest.fail("expected a guarded approval")
            pending = wait_for(
                lambda: (run if (run := get_run(client, cid)) and run["approvals"] and run["phase"] == "approval_request" else None),
                "detached approval/progress was not visible to another observer",
            )
            assert pending["status"] == "running"
            assert pending["partial_answer"] == "", "tool preamble must be cleared from the answer buffer"
            assert executed == [], "navigation must not imply permission to execute"
            # A new independent connection represents another desktop window.
            with httpx.Client(base_url=str(client.base_url), timeout=5) as other:
                response = other.post("/api/approvals/decide", json={
                    "approval_id": pending["approvals"][0]["approval_id"],
                    "decision": "approve", "scope": "once",
                })
                assert response.status_code == 200, response.text
            wait_for(lambda: run_id not in run_store.active_runs(), "approved detached turn did not finish")
            assert executed == ["mcp__synthetic__guarded_read"]
            history = client.get(f"/api/conversations/{cid}").json()["messages"]
            assert len(history) == 1 and history[0]["answer"] == "APPROVED_FINAL_ANSWER"
            assert len(model_calls) == 2
            assert approval_store.list_for_run(run_id) == []
        finally:
            if run_id in run_store.active_runs():
                run_store.cancel(run_id)
                wait_for(lambda: run_id not in run_store.active_runs(), "test worker did not finish")


def test_no_observer_cannot_stall_full_queue_or_leave_running_checkpoint(monkeypatch):
    run_id, cid = "chat_no_observer_queue", "c_no_observer"
    produced = []

    def fake_agent(*_args, **_kwargs):
        for index in range(500):
            produced.append(index)
            yield "answer_delta", {"kind": "answer_delta", "text": "x"}
        # Deliberately omit the final answer: this must be failed, not left
        # running or falsely marked completed just because the generator ended.

    monkeypatch.setattr(chat, "run_agent", fake_agent)
    with SessionLocal() as db:
        db.add(ForegroundRun(id=run_id, conversation_id=cid, question="q", status="running"))
        db.commit()
    response = chat.chat_stream(chat.ChatRequest(question="q", conversation_id=cid, run_id=run_id))
    try:
        wait_for(lambda: run_id not in run_store.active_runs(), "unobserved queue blocked the executor")
        assert len(produced) == 500
        with SessionLocal() as db:
            saved = db.get(ForegroundRun, run_id)
            assert saved.status == "failed"
            assert saved.partial_answer == "x" * 500
    finally:
        import asyncio
        asyncio.run(response.body_iterator.aclose())
