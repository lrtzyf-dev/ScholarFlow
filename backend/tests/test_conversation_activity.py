"""Read receipts acknowledge observed answers and activity stays reachable."""
import asyncio
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import approval_store, run_store, task_runner
from app.api import tasks as tasks_api
from app.main import app
from app.models import Conversation, DeletedConversation, ForegroundRun, QARecord, TaskRecord

client = TestClient(app)


def summaries():
    return {row["id"]: row for row in client.get("/api/conversations").json()["conversations"]}


def test_receipts_are_idempotent_scoped_and_do_not_clear_concurrent_new_answers(db):
    db.add_all([Conversation(id="c_read", title="Read"), Conversation(id="c_other", title="Other"),
                QARecord(id="qa_seen", conversation_id="c_read", answer="Seen"),
                QARecord(id="qa_elsewhere", conversation_id="c_other", answer="Elsewhere")])
    db.commit()
    assert client.get("/api/conversations/c_read").status_code == 200
    assert summaries()["c_read"]["unread_count"] == 1, "loading history is not reading it"
    # Another answer commits while the original read acknowledgement is in flight.
    db.add(QARecord(id="qa_later", conversation_id="c_read", answer="Not shown yet"))
    db.commit()
    body = {"message_ids": ["qa_seen", "qa_elsewhere", "qa_missing", "qa_seen"]}
    first = client.post("/api/conversations/c_read/read", json=body)
    assert first.status_code == 200 and first.json()["message_ids"] == ["qa_seen"]
    first_time = client.get("/api/conversations/c_read").json()["messages"][0]["read_at"]
    assert first_time
    assert client.post("/api/conversations/c_read/read", json=body).json() == first.json()
    assert client.get("/api/conversations/c_read").json()["messages"][0]["read_at"] == first_time
    items = summaries()
    assert items["c_read"]["unread_count"] == 1 and items["c_other"]["unread_count"] == 1
    assert client.post("/api/conversations/c_read/read", json={"message_ids": []}).status_code == 422
    assert client.post("/api/conversations/c_read/read", json={"message_ids": ["x"] * 101}).status_code == 422
    fork = client.post("/api/conversations/c_read/fork", json={}).json()["id"]
    assert summaries()[fork]["unread_count"] == 0
    assert client.delete("/api/conversations/c_read").status_code == 200
    assert client.post("/api/conversations/c_read/read", json=body).status_code == 410


def test_activity_includes_old_runs_approvals_finishing_and_legacy_background_entry(db, monkeypatch):
    old = datetime.utcnow() - timedelta(days=20)
    db.add_all(ForegroundRun(id=f"chat_archived_{i:03}", conversation_id=f"c_archived_{i:03}",
                             question="Archived", status="interrupted") for i in range(125))
    db.add_all([
        ForegroundRun(id="chat_live_old_001", conversation_id="c_live", question="Old live question",
                      created_at=old, updated_at=old),
        ForegroundRun(id="chat_finishing_001", conversation_id="c_live", question="Finished question",
                      status="completed"),
        ForegroundRun(id="chat_stale_001", conversation_id="c_stale", question="Stale question",
                      heartbeat_at=old),
        TaskRecord(id="legacy_background", type="agent", conversation_id="c_background", title="Legacy background",
                   status="running", payload={"images": ["image-payload-must-not-appear"]}),
        TaskRecord(id="finishing_background", type="agent", conversation_id="c_background", status="cancelled"),
        DeletedConversation(id="c_deleted"),
        ForegroundRun(id="chat_deleted_001", conversation_id="c_deleted", question="Deleted question"),
    ])
    db.commit()
    monkeypatch.setattr(run_store, "_active", {"chat_live_old_001": "c_live", "chat_finishing_001": "c_live"})
    monkeypatch.setattr(task_runner, "_running", {"finishing_background": object()})
    approval = approval_store.create("write_file", {"secret": "arguments-must-not-appear"}, "high", "", "chat_live_old_001")
    try:
        items = summaries()
        assert items["c_live"]["running_count"] == 1
        assert items["c_live"]["finishing_count"] == 1
        assert items["c_live"]["approval_count"] == 1
        assert items["c_background"]["running_count"] == items["c_background"]["finishing_count"] == 1
        assert client.get("/api/conversations/c_background").json()["messages"] == []
        assert items["c_stale"]["running_count"] == 0 and "c_deleted" not in items
        assert len(items) == 128
        encoded = json.dumps(items)
        assert "arguments-must-not-appear" not in encoded and "image-payload-must-not-appear" not in encoded
        restored = client.get("/api/foreground-runs", params={"conversation_id": "c_live"}).json()["runs"]
        assert restored[0]["approvals"] == [approval.to_request()]
        approval_store.resolve(approval.id, "deny")
        assert summaries()["c_live"]["approval_count"] == 0
    finally:
        approval_store.discard(approval.id)


def test_start_background_creates_reachable_conversation_before_delivery(db, monkeypatch):
    monkeypatch.setattr(task_runner, "_launch", lambda db, task: task_runner.task_to_dict(task))
    task = task_runner.start_agent_task(db, "Research before delivery", None, [], None)
    assert db.get(Conversation, task["conversation_id"]).title == "Research before delivery"
    assert client.get(f"/api/conversations/{task['conversation_id']}").json()["messages"] == []
    assert summaries()[task["conversation_id"]]["running_count"] == 1


def test_legacy_read_migration_preserves_existing_history_and_leaves_new_answers_unread(monkeypatch):
    from app import db as database

    legacy = create_engine("sqlite://")
    try:
        with legacy.begin() as conn:
            conn.execute(text("CREATE TABLE qa_records (id TEXT PRIMARY KEY, conversation_id TEXT, answer TEXT)"))
            conn.execute(text("INSERT INTO qa_records VALUES ('qa_old', 'c_old', 'Old answer')"))
        monkeypatch.setattr(database, "engine", legacy)
        database._lightweight_migrations()
        with legacy.begin() as conn:
            conn.execute(text("INSERT INTO qa_records (id, conversation_id, answer) VALUES ('qa_new', 'c_new', 'New answer')"))
        database._lightweight_migrations()
        with legacy.connect() as conn:
            rows = {row.id: row for row in conn.execute(text("SELECT id, answer, read_at FROM qa_records"))}
            assert rows["qa_old"].read_at and rows["qa_old"].answer == "Old answer"
            assert rows["qa_new"].read_at is None
    finally:
        legacy.dispose()


def test_sse_delivers_unread_receipts_pins_renames_and_deletions_without_a_second_connection(db):
    db.add_all([Conversation(id="c_sse_read", title="Before"),
                QARecord(id="qa_sse_read", conversation_id="c_sse_read", answer="Answer")])
    db.commit()

    async def check():
        response = await tasks_api.events()
        stream = response.body_iterator

        async def item():
            frame = await asyncio.wait_for(anext(stream), 3)
            return json.loads(frame.split("data: ", 1)[1])["conversations"]

        try:
            assert (await item())[0]["unread_count"] == 1
            await asyncio.to_thread(client.post, "/api/conversations/c_sse_read/read", json={"message_ids": ["qa_sse_read"]})
            assert (await item())[0]["unread_count"] == 0
            await asyncio.to_thread(client.put, "/api/conversations/c_sse_read/pin", json={"pinned": True})
            assert (await item())[0]["pinned_at"]
            await asyncio.to_thread(client.put, "/api/conversations/c_sse_read", json={"title": "After"})
            assert (await item())[0]["title"] == "After"
            await asyncio.to_thread(client.delete, "/api/conversations/c_sse_read")
            assert await item() == []
        finally:
            await stream.aclose()

    asyncio.run(check())
