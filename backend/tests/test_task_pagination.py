"""Active tasks survive large archives; history uses stable keyset pages."""
import asyncio
import base64
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import approval_store, task_events, task_runner
from app.api import tasks as tasks_api
from app.main import app
from app.models import TaskRecord

client = TestClient(app)


def seed_history(db, count=125):
    old = datetime.utcnow() - timedelta(days=10)
    db.add_all(TaskRecord(id=f"history_{i:03}", type="agent", title=f"History {i}", status="done",
                          created_at=old, updated_at=old) for i in range(count))
    db.commit()


def test_all_active_tasks_and_approvals_survive_more_than_100_newer_records(db, monkeypatch):
    seed_history(db)
    old = datetime.utcnow() - timedelta(days=20)
    db.add(TaskRecord(id="old_active", type="agent", title="Old active", status="running", created_at=old, updated_at=old))
    db.add(TaskRecord(id="still_finishing", type="agent", status="interrupted", created_at=old, updated_at=old))
    db.commit()
    monkeypatch.setattr(task_runner, "_running", {"still_finishing": object()})
    approval = approval_store.create("write_report", {"path": "report.md"}, "high", "Write report", "old_active")
    try:
        snapshot = client.get("/api/tasks").json()
        tasks = {task["id"]: task for task in snapshot["tasks"]}
        assert snapshot["history_total"] == 125
        assert len(tasks) == 32
        assert tasks["old_active"]["approvals"] == [approval.to_request()]
        assert tasks["still_finishing"]["executing"] is True
        assert snapshot["next_cursor"]
    finally:
        approval_store.discard(approval.id)


def test_history_pages_handle_equal_timestamps_and_newer_insertions(db):
    seed_history(db, 76)
    initial = client.get("/api/tasks").json()
    seen = [task["id"] for task in initial["tasks"]]
    cursor = initial["next_cursor"]
    # A newly finished task must not shift the existing cursor by an offset.
    db.add(TaskRecord(id="new_completion", type="agent", status="done"))
    db.commit()
    while cursor:
        response = client.get("/api/tasks/history", params={"cursor": cursor, "limit": 17})
        assert response.status_code == 200
        page = response.json()
        seen.extend(task["id"] for task in page["tasks"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 76
    assert set(seen) == {f"history_{i:03}" for i in range(76)}
    assert client.get("/api/tasks").json()["tasks"][0]["id"] == "new_completion"


def test_invalid_history_cursor_and_limits_are_rejected():
    for cursor in ("!invalid", base64.urlsafe_b64encode(b'[]').decode(),
                   base64.urlsafe_b64encode(b'["not-a-date", "id"]').decode()):
        assert client.get("/api/tasks/history", params={"cursor": cursor}).status_code == 422
    for limit in (0, 101):
        assert client.get("/api/tasks/history", params={"limit": limit}).status_code == 422


def test_archive_epoch_changes_on_conversation_deletion_but_not_normal_progress(db):
    cid = client.post("/api/conversations", json={"title": "Delete archived tasks"}).json()["id"]
    task = TaskRecord(id="archive_to_delete", type="agent", status="done", conversation_id=cid)
    db.add(task)
    db.commit()
    initial = client.get("/api/tasks").json()
    task.progress = 0.9
    db.commit()
    assert client.get("/api/tasks").json()["history_epoch"] == initial["history_epoch"]
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    deleted = client.get("/api/tasks").json()
    assert deleted["history_epoch"] != initial["history_epoch"]
    assert deleted["history_total"] == 0 and deleted["tasks"] == []


def test_sse_retains_completion_of_old_active_task_in_recent_history(db):
    seed_history(db)
    old = datetime.utcnow() - timedelta(days=20)
    task = TaskRecord(id="old_completion", type="agent", status="running", created_at=old, updated_at=old)
    db.add(task)
    db.commit()

    async def check():
        response = await tasks_api.events()
        stream = response.body_iterator
        try:
            first = await asyncio.wait_for(anext(stream), 2)
            snapshot = json.loads(first.split("data: ", 1)[1])
            assert any(row["id"] == task.id and row["status"] == "running" for row in snapshot["tasks"])
            task.status, task.updated_at = "done", datetime.utcnow()
            db.commit()
            task_events.notify_tasks_changed()
            changed = await asyncio.wait_for(anext(stream), 2)
            snapshot = json.loads(changed.split("data: ", 1)[1])
            assert any(row["id"] == task.id and row["status"] == "done" for row in snapshot["tasks"])
        finally:
            await stream.aclose()

    asyncio.run(check())
