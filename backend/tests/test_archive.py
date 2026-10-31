"""Archive preserves data and coordinates with admission, deletion and other windows."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import run_store, task_runner
from app.main import app
from app.models import Conversation, DesktopState, ForegroundRun, QARecord, TaskRecord

client = TestClient(app)


def archive(cid="c_archive", archived=True, revision=0):
    return client.put(f"/api/conversations/{cid}/archive", json={"archived": archived, "expected_revision": revision})


def seed(db):
    at = datetime(2026, 1, 2, 3, 4, 5)
    db.add(Conversation(id="c_archive", title="Research archive", created_at=at, updated_at=at,
                        pinned_at=at, pin_position=0))
    db.add(QARecord(id="qa_archive", conversation_id="c_archive", question="Question", answer="Retained answer",
                    evidence=[{"text": "Evidence"}], usage={"total_tokens": 25}))
    db.add(DesktopState(key="conversation:c_archive", revision=3, value={"text": "Retained draft", "images": []}))
    db.add(DesktopState(key="selection", revision=2, value={"conversation_id": "c_archive"}))
    db.add(TaskRecord(id="task_archive", type="agent", conversation_id="c_archive", status="interrupted",
                      checkpoint={"retained": True}, result={"answer": "Retained task"}))
    db.add(ForegroundRun(id="chat_archive_test", conversation_id="c_archive", question="Interrupted question",
                        status="interrupted", partial_answer="Retained partial answer"))
    db.commit()
    return at


def ids(scope="active"):
    return {row["id"] for row in client.get("/api/conversations", params={"scope": scope}).json()["conversations"]}


def test_archive_preserves_history_tasks_drafts_receipts_usage_and_selection(db):
    at = seed(db)
    saved = archive().json()
    assert saved["archived_at"] and saved["archive_revision"] == 1
    assert archive().json() == saved  # retry after a lost response
    assert "c_archive" not in ids()
    assert "c_archive" in ids("archived") and "c_archive" in ids("all")
    detail = client.get("/api/conversations/c_archive").json()
    assert detail["archived_at"] == saved["archived_at"]
    assert detail["messages"][0]["answer"] == "Retained answer"
    assert detail["messages"][0]["evidence"] == [{"text": "Evidence"}]
    assert client.get("/api/conversations/c_archive/usage").json()["usage"]["total_tokens"] == 25
    assert client.get("/api/desktop-state/selection").json()["value"]["conversation_id"] == "c_archive"
    assert client.get("/api/desktop-state/conversations/c_archive").json()["revision"] == 3
    assert client.get("/api/foreground-runs", params={"conversation_id": "c_archive"}).json()["runs"][0]["partial_answer"] == "Retained partial answer"
    task = client.get("/api/tasks/task_archive").json()
    assert task["result"]["answer"] == "Retained task"
    db.expire_all()
    conv = db.get(Conversation, "c_archive")
    assert conv.updated_at == conv.created_at == at
    assert conv.pinned_at is None and conv.pin_position is None
    assert db.get(TaskRecord, "task_archive").checkpoint == {"retained": True}
    assert client.post("/api/conversations/c_archive/read", json={"message_ids": ["qa_archive"]}).status_code == 200
    fork = client.post("/api/conversations/c_archive/fork", json={}).json()
    assert client.get(f"/api/conversations/{fork['id']}").json()["archived_at"] is None


def test_restore_is_idempotent_unpinned_and_rejects_late_opposite_requests(db):
    seed(db)
    assert archive().status_code == 200
    assert archive(archived=False, revision=1).json()["archive_revision"] == 2
    assert archive(archived=False, revision=1).json()["archive_revision"] == 2
    assert archive().status_code == 409  # old archive cannot undo a restore
    assert archive(revision=2).json()["archive_revision"] == 3
    stale = archive(archived=False, revision=1)
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["archive_revision"] == 3
    assert archive(archived=False, revision=3).status_code == 200
    row = client.get("/api/conversations").json()["conversations"][0]
    assert row["id"] == "c_archive" and row["pinned_at"] is None
    assert client.put("/api/conversations/c_archive/pin", json={"pinned": True}).status_code == 200


@pytest.mark.parametrize("source", ["draft", "qa", "run"])
def test_archive_materializes_legacy_and_unsent_sources_without_resurrection(db, source):
    cid = "c_source"
    if source == "draft":
        db.add(DesktopState(key=f"conversation:{cid}", revision=1, value={"text": "Unsent draft", "images": []}))
    elif source == "qa":
        db.add(QARecord(id="qa_source", conversation_id=cid, question="Old question", answer="Old answer"))
    else:
        db.add(ForegroundRun(id="chat_legacy_run", conversation_id=cid, question="Lost stream", status="interrupted"))
    db.commit()
    assert archive(cid).status_code == 200
    for _ in range(2):
        assert cid not in ids() and cid in ids("archived")
    assert archive(cid, False, 1).status_code == 200
    assert cid in ids()


@pytest.mark.parametrize("kind", ["pending", "running", "finishing", "foreground", "live", "stale_live"])
def test_archive_rejects_running_and_finishing_work_atomically(db, monkeypatch, kind):
    seed(db)
    if kind in ("pending", "running", "finishing"):
        task = db.get(TaskRecord, "task_archive")
        task.status = "interrupted" if kind == "finishing" else kind
        if kind == "finishing":
            monkeypatch.setattr(task_runner, "running_task_ids", lambda: [task.id])
    elif kind in ("foreground", "stale_live"):
        run = db.get(ForegroundRun, "chat_archive_test")
        run.status = "running"
        if kind == "stale_live":
            run.heartbeat_at = datetime.utcnow() - timedelta(minutes=5)
    if kind in ("live", "stale_live"):
        monkeypatch.setattr(run_store, "active_for_conversation", lambda cid: cid == "c_archive")
    db.commit()
    response = archive()
    assert response.status_code == 409 and "先停止" in response.json()["detail"]
    db.expire_all()
    conv = db.get(Conversation, "c_archive")
    assert conv.archived_at is None and conv.pinned_at and conv.archive_revision == 0


def test_stale_foreground_checkpoint_can_be_archived_and_is_retained(db):
    seed(db)
    run = db.get(ForegroundRun, "chat_archive_test")
    run.status = "running"
    run.heartbeat_at = datetime.utcnow() - timedelta(minutes=5)
    db.commit()
    assert archive().status_code == 200
    db.expire_all()
    assert db.get(ForegroundRun, run.id).status == "interrupted"


def test_archived_admission_guards_do_not_create_runs_or_tasks(db):
    seed(db)
    assert archive().status_code == 200
    requests = [
        ("POST", "/api/chat/stream", {"question": "q", "conversation_id": "c_archive"}),
        ("POST", "/api/foreground-runs", {"id": "chat_should_not_start", "conversation_id": "c_archive", "question": "q"}),
        ("POST", "/api/tasks/start", {"question": "q", "conversation_id": "c_archive"}),
        ("POST", "/api/tasks/task_archive/resume", {}),
        ("POST", "/api/tasks/task_archive/restart", {}),
        ("PUT", "/api/conversations/c_archive/pin", {"pinned": True}),
    ]
    for method, url, body in requests:
        response = client.request(method, url, json=body)
        assert response.status_code == 409, (url, response.text)
        assert "归档" in response.json()["detail"]
    assert db.query(ForegroundRun).count() == db.query(TaskRecord).count() == 1
    # A delayed desktop save may persist, but can never unarchive the conversation.
    assert client.put("/api/desktop-state/conversations/c_archive", json={"revision": 3, "value": {"text": "Late draft"}}).status_code == 200
    assert "c_archive" not in ids()
    assert archive(archived=False, revision=1).status_code == 200
    assert client.get("/api/desktop-state/conversations/c_archive").json()["value"]["text"] == "Late draft"


def test_archive_deletion_cleans_retained_data_and_old_requests_cannot_restore(db):
    seed(db)
    assert archive().status_code == 200
    assert client.delete("/api/conversations/c_archive").status_code == 200
    assert archive(archived=False, revision=1).status_code == 410
    assert archive().status_code == 410
    assert not ids("all")
    db.expire_all()
    assert db.query(QARecord).count() == db.query(TaskRecord).count() == db.query(ForegroundRun).count() == 0
    assert db.get(DesktopState, "conversation:c_archive") is None


def test_archive_migration_preserves_existing_data_and_survives_second_migration(monkeypatch):
    from app import db as database
    legacy = create_engine("sqlite://")
    try:
        with legacy.begin() as conn:
            conn.execute(text("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT)"))
            conn.execute(text("INSERT INTO conversations VALUES ('c_old', 'Old title')"))
        monkeypatch.setattr(database, "engine", legacy)
        database._lightweight_migrations()
        with legacy.begin() as conn:
            row = conn.execute(text("SELECT title, archived_at, archive_revision FROM conversations")).one()
            assert tuple(row) == ("Old title", None, 0)
            conn.execute(text("UPDATE conversations SET archived_at='2026-01-01', archive_revision=3"))
        database._lightweight_migrations()
        with legacy.connect() as conn:
            assert conn.execute(text("SELECT archive_revision FROM conversations")).scalar() == 3
    finally:
        legacy.dispose()


def test_archive_and_restore_push_state_to_existing_sse(db):
    from app.api import tasks as tasks_api
    seed(db)

    async def check():
        stream = (await tasks_api.events()).body_iterator
        try:
            await asyncio.wait_for(anext(stream), 3)
            for archived, revision in ((True, 0), (False, 1)):
                assert (await asyncio.to_thread(archive, "c_archive", archived, revision)).status_code == 200
                frame = await asyncio.wait_for(anext(stream), 3)
                row = next(item for item in json.loads(frame.split("data: ", 1)[1])["conversations"] if item["id"] == "c_archive")
                assert bool(row["archived_at"]) == archived
                assert row["archive_revision"] == revision + 1
        finally:
            await stream.aclose()
    asyncio.run(check())


def test_archive_rejects_unknown_ids_bad_scopes_and_invalid_revisions():
    assert archive("missing").status_code == 404
    assert archive(revision=-1).status_code == 422
    assert client.get("/api/conversations?scope=missing").status_code == 422
