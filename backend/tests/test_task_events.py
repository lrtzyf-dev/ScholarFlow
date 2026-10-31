"""Task snapshots, cross-thread notifications and background state transitions."""
import asyncio
import json
import threading

from fastapi.testclient import TestClient

from app import approval_store, task_events, task_runner
from app.api import tasks as tasks_api
from app.db import SessionLocal
from app.main import app
from app.models import TaskRecord


def test_broker_coalesces_thread_notifications_and_unsubscribes():
    async def check():
        async with task_events.subscribe() as queue:
            thread = threading.Thread(target=lambda: [task_events.notify_tasks_changed() for _ in range(10)])
            thread.start()
            thread.join()
            await asyncio.sleep(0)
            assert queue.qsize() == 1
            await asyncio.wait_for(queue.get(), 1)
        task_events.notify_tasks_changed()
        await asyncio.sleep(0)
        assert queue.empty()

    asyncio.run(check())


def test_sse_initial_snapshot_and_changed_commit(db):
    task = TaskRecord(type="agent", title="SSE test", status="running", events=[])
    db.add(task)
    db.commit()

    async def check():
        response = await tasks_api.events()
        stream = response.body_iterator
        try:
            first = await asyncio.wait_for(anext(stream), 2)
            assert first.startswith("event: tasks\n")
            initial = json.loads(first.split("data: ", 1)[1])
            assert initial["tasks"][0]["status"] == "running"
            task.status = "done"
            db.commit()
            task_events.notify_tasks_changed()
            changed = await asyncio.wait_for(anext(stream), 2)
            assert json.loads(changed.split("data: ", 1)[1])["tasks"][0]["status"] == "done"
        finally:
            await stream.aclose()

    asyncio.run(check())


def test_pending_approvals_are_actionable_and_cannot_be_decided_twice(db):
    task = TaskRecord(type="agent", title="Approval test", status="running", events=[])
    db.add(task)
    db.commit()
    pending = approval_store.create("write_file", {"path": "output.md"}, "high", "Write output", task.id)
    try:
        client = TestClient(app)
        snapshot = client.get(f"/api/tasks/{task.id}").json()
        assert snapshot["approvals"] == [pending.to_request()]
        body = {"approval_id": pending.id, "decision": "approve", "scope": "session"}
        assert client.post("/api/approvals/decide", json=body).status_code == 200
        assert pending.event.is_set()
        assert pending.scope == "session"
        assert client.get(f"/api/tasks/{task.id}").json()["approvals"] == []
        assert client.post("/api/approvals/decide", json=body).status_code == 404
    finally:
        approval_store.discard(pending.id)


def test_background_runner_publishes_committed_progress_and_preserves_events(db, monkeypatch):
    task = TaskRecord(type="agent", title="Progress test", status="pending", events=[])
    db.add(task)
    db.commit()
    snapshots = []

    def capture():
        with SessionLocal() as observer:
            snapshots.append(task_runner.get_task(observer, task.id))

    def agent(*args, **kwargs):
        yield "thinking_delta", {"text": "ignored token"}
        yield "todo_update", {"steps": [{"title": "Search", "status": "done"}, {"title": "Write", "status": "pending"}]}
        yield "status", {"text": "Search complete"}
        yield "tool_call", {"name": "search", "text": "query"}
        yield "approval_result", {"approval_id": "test", "decision": "deny", "text": "Denied"}
        yield "answer", {"answer": "Result", "evidence": []}

    monkeypatch.setattr(task_runner, "run_agent", agent)
    monkeypatch.setattr(task_runner, "notify_tasks_changed", capture)
    monkeypatch.setattr(task_runner.notifier, "notify", lambda *a, **kw: None)
    task_runner._run_in_background(task.id, "question", None, [], "test_conversation")
    assert snapshots[0]["status"] == "running"
    assert any(s["progress"] == 0.5 for s in snapshots)
    assert snapshots[-1]["status"] == "done"
    assert snapshots[-1]["result"]["answer"] == "Result"
    assert [e["kind"] for e in snapshots[-1]["events"]] == ["status", "tool_call", "approval_result"]
    assert len(snapshots) == 8  # running, five meaningful events, done, thread cleanup


def test_background_error_event_cannot_finish_as_success(db, monkeypatch):
    task = TaskRecord(type="agent", title="Failed test", status="pending", events=[])
    db.add(task)
    db.commit()
    monkeypatch.setattr(task_runner, "run_agent", lambda *a, **kw: iter([
        ("error", {"text": "Provider unavailable"}),
        ("answer", {"answer": "Please retry", "evidence": []}),
    ]))
    monkeypatch.setattr(task_runner.notifier, "notify", lambda *a, **kw: None)
    task_runner._run_in_background(task.id, "question", None, [], "failed_conversation")
    db.refresh(task)
    assert task.status == "failed"
    assert task.error == "Provider unavailable"
    assert task.result["answer"] == "Please retry"
