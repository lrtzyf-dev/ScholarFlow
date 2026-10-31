"""Persistent conversation pins and desktop sidebar preferences."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app
from app.models import Conversation, QARecord
from app.conversation_store import ordered_pins

client = TestClient(app)


def test_pin_is_idempotent_preserves_activity_and_is_not_inherited_by_fork(db):
    original = datetime(2026, 1, 1)
    db.add(Conversation(id="c_pin", title="Pinned conversation", created_at=original, updated_at=original))
    db.add(QARecord(id="qa_pin", conversation_id="c_pin", question="q", answer="a"))
    db.commit()
    url = "/api/conversations/c_pin/pin"
    pinned = client.put(url, json={"pinned": True}).json()
    assert pinned["pinned_at"]
    assert client.put(url, json={"pinned": True}).json() == pinned
    listed = client.get("/api/conversations").json()["conversations"][0]
    assert listed["pinned_at"] == pinned["pinned_at"]
    assert listed["updated_at"] == original.isoformat()
    fork = client.post("/api/conversations/c_pin/fork", json={}).json()
    items = client.get("/api/conversations").json()["conversations"]
    assert next(item for item in items if item["id"] == fork["id"])["pinned_at"] is None
    assert client.put(url, json={"pinned": False}).json()["pinned_at"] is None


def test_pin_unsent_draft_preserves_text_and_delete_prevents_late_pin():
    draft = "/api/desktop-state/conversations/c_pin_draft"
    assert client.put(draft, json={"revision": 0, "value": {"text": "Unsent draft\nSecond line"}}).status_code == 200
    before = client.get(draft).json()
    url = "/api/conversations/c_pin_draft/pin"
    assert client.put(url, json={"pinned": True}).status_code == 200
    item = client.get("/api/conversations").json()["conversations"][0]
    assert item["title"] == "Unsent draft" and item["draft_only"] is True
    assert item["message_count"] == 0 and item["pinned_at"]
    assert client.get(draft).json() == before
    assert client.delete("/api/conversations/c_pin_draft").status_code == 200
    assert client.put(url, json={"pinned": True}).status_code == 410
    assert client.put("/api/conversations/c_unknown/pin", json={"pinned": True}).status_code == 404
    assert client.get("/api/conversations").json()["conversations"] == []


def test_sidebar_sort_is_validated_and_uses_revisions():
    url = "/api/desktop-state/sidebar"
    assert client.get(url).json() == {"revision": 0, "value": {"sort": "updated_at"}}
    saved = client.put(url, json={"revision": 0, "value": {"sort": "created_at"}})
    assert saved.status_code == 200
    assert client.get(url).json() == saved.json()
    conflict = client.put(url, json={"revision": 0, "value": {"sort": "updated_at"}})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["value"] == {"sort": "created_at"}
    assert client.put(url, json={"revision": 1, "value": {"sort": "arbitrary"}}).status_code == 422


def test_pins_migrate_legacy_database_without_changing_history(monkeypatch):
    from app import db as database

    legacy = create_engine("sqlite://")
    try:
        with legacy.begin() as conn:
            conn.execute(text("CREATE TABLE conversations (id VARCHAR(32) PRIMARY KEY, title VARCHAR(512))"))
            conn.execute(text("INSERT INTO conversations (id, title) VALUES ('c_old', 'Old title')"))
        monkeypatch.setattr(database, "engine", legacy)
        database._lightweight_migrations()
        database._lightweight_migrations()
        with legacy.connect() as conn:
            assert conn.execute(text("SELECT id, title, pinned_at FROM conversations")).one() == ("c_old", "Old title", None)
    finally:
        legacy.dispose()


def seed_pins(db):
    original = datetime(2026, 1, 1)
    for index, cid in enumerate(("c_a", "c_b", "c_c")):
        db.add(Conversation(id=cid, title=cid, created_at=original, updated_at=original,
                            pinned_at=original + timedelta(seconds=index), pin_position=2 - index))
    db.commit()
    return original


def pin_ids(db):
    db.expire_all()
    return [conv.id for conv in ordered_pins(db)]


def save_order(ids, expected):
    return client.put("/api/conversations/pins/order", json={"conversation_ids": ids, "expected_order": expected})


def test_manual_pin_order_preserves_activity_and_supports_lost_response_retry(db):
    original = seed_pins(db)
    before = {conv.id: conv.pinned_at for conv in ordered_pins(db)}
    expected, desired = ["c_c", "c_b", "c_a"], ["c_a", "c_c", "c_b"]
    first = save_order(desired, expected)
    assert first.status_code == 200 and first.json()["conversation_ids"] == desired
    assert save_order(desired, expected).json() == first.json()
    assert pin_ids(db) == desired
    for position, cid in enumerate(desired):
        conv = db.get(Conversation, cid)
        assert conv.pin_position == position and conv.pinned_at == before[cid]
        assert conv.created_at == conv.updated_at == original
    summaries = client.get("/api/tasks").json()["conversations"]
    assert {row["id"]: row["pin_position"] for row in summaries} == {cid: i for i, cid in enumerate(desired)}


def test_new_pin_goes_first_unpin_and_repin_use_current_order(db):
    seed_pins(db)
    assert save_order(["c_a", "c_c", "c_b"], ["c_c", "c_b", "c_a"]).status_code == 200
    assert client.put("/api/conversations/c_c/pin", json={"pinned": False}).json()["pin_position"] is None
    assert pin_ids(db) == ["c_a", "c_b"]
    assert client.put("/api/conversations/c_c/pin", json={"pinned": True}).json()["pin_position"] == 0
    assert pin_ids(db) == ["c_c", "c_a", "c_b"]
    db.add(Conversation(id="c_new", title="New"))
    db.commit()
    assert client.put("/api/conversations/c_new/pin", json={"pinned": True}).status_code == 200
    assert pin_ids(db) == ["c_new", "c_c", "c_a", "c_b"]


def test_stale_reorder_cannot_overwrite_other_window_unpin_or_deletion(db):
    seed_pins(db)
    initial = ["c_c", "c_b", "c_a"]
    assert save_order(["c_a", "c_b", "c_c"], initial).status_code == 200
    stale = save_order(["c_b", "c_c", "c_a"], initial)
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_order"] == ["c_a", "c_b", "c_c"]
    assert client.put("/api/conversations/c_b/pin", json={"pinned": False}).status_code == 200
    assert save_order(initial, ["c_a", "c_b", "c_c"]).status_code == 409
    assert pin_ids(db) == ["c_a", "c_c"]
    assert client.delete("/api/conversations/c_c").status_code == 200
    assert save_order(["c_c", "c_a"], ["c_a", "c_c"]).status_code == 409
    assert pin_ids(db) == ["c_a"]
    assert db.get(Conversation, "c_c") is None


def test_pin_reorder_rejects_duplicates_partial_and_foreign_ids_atomically(db):
    seed_pins(db)
    expected = ["c_c", "c_b", "c_a"]
    for ids in (["c_a", "c_a", "c_c"], ["c_a", "c_b"], ["c_foreign", "c_b", "c_a"]):
        assert save_order(ids, expected).status_code == 422
        assert pin_ids(db) == expected
    assert save_order(["c_foreign"], ["c_foreign"]).status_code == 409
    assert save_order(["bad/id"], ["bad/id"]).status_code == 422
    assert save_order(["a"] * 10001, ["a"] * 10001).status_code == 422
    assert pin_ids(db) == expected


def test_legacy_pin_positions_keep_timestamp_order_and_do_not_reset_on_startup(monkeypatch):
    from app import db as database

    legacy = create_engine("sqlite://")
    try:
        with legacy.begin() as conn:
            conn.execute(text("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, pinned_at DATETIME)"))
            conn.execute(text("INSERT INTO conversations VALUES ('c_a', 'A', '2026-01-01'), ('c_b', 'B', '2026-01-02'), ('c_c', 'C', NULL)"))
        monkeypatch.setattr(database, "engine", legacy)
        database._lightweight_migrations()
        with legacy.begin() as conn:
            assert conn.execute(text("SELECT id FROM conversations WHERE pinned_at IS NOT NULL ORDER BY pin_position")).scalars().all() == ["c_b", "c_a"]
            assert conn.execute(text("SELECT pin_position FROM conversations WHERE id='c_c'")).scalar() is None
            conn.execute(text("UPDATE conversations SET pin_position = 1 - pin_position WHERE pinned_at IS NOT NULL"))
        database._lightweight_migrations()
        with legacy.connect() as conn:
            assert conn.execute(text("SELECT id FROM conversations WHERE pinned_at IS NOT NULL ORDER BY pin_position")).scalars().all() == ["c_a", "c_b"]
    finally:
        legacy.dispose()


def test_reorder_pushes_updated_positions_to_existing_sse(db):
    import asyncio
    import json
    from app.api import tasks as tasks_api

    seed_pins(db)

    async def check():
        stream = (await tasks_api.events()).body_iterator
        try:
            await asyncio.wait_for(anext(stream), 3)
            response = await asyncio.to_thread(save_order, ["c_a", "c_b", "c_c"], ["c_c", "c_b", "c_a"])
            assert response.status_code == 200
            frame = await asyncio.wait_for(anext(stream), 3)
            items = json.loads(frame.split("data: ", 1)[1])["conversations"]
            assert [row["id"] for row in sorted(items, key=lambda row: row["pin_position"])] == ["c_a", "c_b", "c_c"]
        finally:
            await stream.aclose()

    asyncio.run(check())
