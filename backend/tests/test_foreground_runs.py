"""Foreground cancellation, connection cleanup and deleted conversation races."""
import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from app import approval_store, cancel_store, run_store
from app.agent import loop
from app.api import chat
from app.llm import LLMResponse
from app.main import app
from app.models import Conversation, DeletedConversation, QARecord, TaskRecord
from app.schemas import ChatRequest

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_runs(monkeypatch):
    monkeypatch.setattr(run_store, "_active", {})
    monkeypatch.setattr(run_store, "_finished", {})
    monkeypatch.setattr(run_store, "_early_cancels", {})
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(approval_store, "_pending", {})


async def wait_finished(run_id):
    for _ in range(200):
        if run_id not in run_store._active:
            return
        await asyncio.sleep(0.01)
    pytest.fail("worker did not clean up its execution")


def test_cancel_before_stream_post_and_duplicate_run_id(monkeypatch):
    run_id = "chat_early_cancel"
    assert client.post(f"/api/chat/runs/{run_id}/cancel").json()["status"] == "cancelling"

    def fake_agent(*args, **kwargs):
        assert cancel_store.is_cancelled(kwargs["run_id"])
        yield "cancelled", {"kind": "cancelled"}

    monkeypatch.setattr(chat, "run_agent", fake_agent)
    response = client.post("/api/chat/stream", json={"question": "q", "run_id": run_id})
    assert response.status_code == 200
    assert response.headers["x-run-id"] == run_id
    assert "event: cancelled" in response.text
    assert client.post(f"/api/chat/runs/{run_id}/cancel").json()["status"] == "cancelled"
    assert not cancel_store.is_cancelled(run_id)
    assert client.post("/api/chat/stream", json={"question": "q", "run_id": run_id}).status_code == 409
    assert client.post("/api/chat/runs/task_other_id/cancel").status_code == 422


@pytest.mark.parametrize("disconnect", [False, True])
def test_cancel_or_disconnect_releases_waiter_and_worker(monkeypatch, disconnect):
    run_id = f"chat_waiter_{disconnect}"
    ready, closed = threading.Event(), threading.Event()

    def fake_agent(*args, **kwargs):
        pending = approval_store.create("write_report", {}, "guarded", run_id=run_id)
        ready.set()
        try:
            yield "approval_request", {"kind": "approval_request", **pending.to_request()}
            assert pending.event.wait(2), "cancel did not wake approval"
            assert cancel_store.is_cancelled(run_id)
            yield "cancelled", {"kind": "cancelled"}
        finally:
            closed.set()

    monkeypatch.setattr(chat, "run_agent", fake_agent)

    async def scenario():
        response = chat.chat_stream(ChatRequest(question="q", run_id=run_id))
        stream = response.body_iterator
        assert "event: run" in await anext(stream)
        assert await asyncio.to_thread(ready.wait, 2)
        assert "event: approval_request" in await anext(stream)
        if disconnect:
            await stream.aclose()
        else:
            result = chat.cancel_run(run_id)
            assert result["status"] == "cancelling"
            frames = [frame async for frame in stream]
            assert any("event: cancelled" in frame for frame in frames)
        await wait_finished(run_id)
        assert closed.is_set()
        assert approval_store.list_for_run(run_id) == []
        assert not approval_store._pending
        assert not cancel_store.is_cancelled(run_id)
        assert run_store.cancel(run_id) == "cancelled"

    asyncio.run(scenario())


def test_worker_start_failure_cleans_registry(monkeypatch):
    run_id = "chat_start_failure"

    def fail_start(self):
        raise RuntimeError("cannot create worker")

    monkeypatch.setattr(chat.threading.Thread, "start", fail_start)

    async def scenario():
        stream = chat.chat_stream(ChatRequest(question="q", run_id=run_id)).body_iterator
        with pytest.raises(RuntimeError, match="cannot create worker"):
            await anext(stream)
        assert run_id not in run_store._active
        assert run_store.cancel(run_id) == "failed"

    asyncio.run(scenario())


def test_disconnect_with_backpressure_closes_source(monkeypatch):
    run_id = "chat_full_queue"
    closed = threading.Event()

    def fake_agent(*args, **kwargs):
        try:
            for i in range(10000):
                yield "answer_delta", {"kind": "answer_delta", "text": str(i)}
        finally:
            closed.set()

    monkeypatch.setattr(chat, "run_agent", fake_agent)

    async def scenario():
        stream = chat.chat_stream(ChatRequest(question="q", run_id=run_id)).body_iterator
        await anext(stream)
        await stream.aclose()
        await wait_finished(run_id)
        assert closed.is_set()

    asyncio.run(scenario())


def test_cancel_during_model_stream_closes_source_without_saving(db, monkeypatch):
    run_id = "chat_midstream_cancel"
    closed = []

    class Model:
        def stream_chat(self, *args, **kwargs):
            try:
                yield "delta", "answer", "partial"
                cancel_store.request_cancel(run_id)
                yield "delta", "answer", "must not be delivered"
                yield "final", LLMResponse(content="must not be saved")
            finally:
                closed.append(True)

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "make_plan", lambda *args: [])
    events = list(loop.run_agent(db, "q", run_id=run_id))
    assert [e["text"] for k, e in events if k == "answer_delta"] == ["partial"]
    assert "cancelled" in [k for k, _ in events]
    assert not any(k in ("answer", "done") for k, _ in events)
    assert closed == [True]
    assert db.query(QARecord).count() == 0


def test_cancel_between_approval_creation_and_wait_never_executes(db, monkeypatch):
    run_id = "chat_approval_race"

    class Model:
        def stream_chat(self, *args, **kwargs):
            yield "final", LLMResponse(content="", tool_calls=[
                {"id": "call1", "name": "mcp__custom__write", "arguments": {}}
            ])

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "make_plan", lambda *args: [])
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **k: pytest.fail("cancelled tool ran"))
    monkeypatch.setattr(approval_store, "wait", lambda *a, **k: pytest.fail("waited after cancellation"))
    events = []
    for kind, data in loop.run_agent(db, "q", run_id=run_id):
        events.append(kind)
        if kind == "approval_request":
            cancel_store.request_cancel(run_id)
    assert "approval_request" in events
    assert "cancelled" in events
    assert approval_store.list_for_run(run_id) == []
    assert not approval_store._pending
    assert db.query(QARecord).count() == 0


def test_cancel_during_tool_stops_remaining_calls_and_final_save(db, monkeypatch):
    run_id = "chat_tool_checkpoint"
    executed = []

    class Model:
        def stream_chat(self, *args, **kwargs):
            yield "final", LLMResponse(content="", tool_calls=[
                {"id": "call1", "name": "list_papers", "arguments": {}},
                {"id": "call2", "name": "list_skills", "arguments": {}},
            ])

    def execute(db, name, arguments, scope_paper_id=None):
        executed.append(name)
        cancel_store.request_cancel(run_id)
        return "{}"

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "make_plan", lambda *args: [])
    monkeypatch.setattr(loop, "execute_tool", execute)
    kinds = [k for k, _ in loop.run_agent(db, "q", run_id=run_id)]
    assert executed == ["list_papers"]
    assert "cancelled" in kinds
    assert not any(k in ("answer", "done") for k in kinds)
    assert db.query(QARecord).count() == 0


@pytest.mark.parametrize("active", ["foreground", "pending", "running"])
def test_delete_blocks_active_work_then_tombstone_rejects_stale_requests(db, active):
    cid, run_id = f"c_delete_{active}", "chat_delete_guard"
    db.add(Conversation(id=cid, title="q"))
    db.commit()
    if active == "foreground":
        assert run_store.begin(run_id, cid)
    else:
        task = TaskRecord(type="agent", title="q", conversation_id=cid, status=active)
        db.add(task)
        db.commit()
    assert client.delete(f"/api/conversations/{cid}").status_code == 409
    assert db.get(Conversation, cid)
    if active == "foreground":
        run_store.finish(run_id, "cancelled")
    else:
        task.status = "cancelled"
        db.commit()
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    db.expire_all()
    assert db.get(Conversation, cid) is None
    assert db.get(DeletedConversation, cid) is not None
    for endpoint in ("/api/chat/stream", "/api/tasks/start"):
        response = client.post(endpoint, json={"question": "stale", "conversation_id": cid})
        assert response.status_code == 410
    assert not client.get("/api/conversations").json()["conversations"]
    assert db.query(TaskRecord).filter(TaskRecord.conversation_id == cid).count() == 0
