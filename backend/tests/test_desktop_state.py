"""Durable desktop state, independent of conversation history and browser ports."""
import base64
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app.models import DesktopState

client = TestClient(app)
path = "/api/desktop-state/conversations/c_draft"


def state(text="draft", **extra):
    return {"text": text, **extra}


def test_new_draft_without_saved_conversation_survives_process_restart():
    assert client.get(path).json()["revision"] == 0
    payload = state("尚未发送", paper_id="paper_a", skills=["research"], images=[{
        "media_type": "image/png", "data": base64.b64encode(b"image payload").decode(),
    }], reading={"following": False, "top": 432, "anchor_id": "qa_a", "offset": 27})
    saved = client.put(path, json={"revision": 0, "value": payload})
    assert saved.status_code == 200
    selected = client.put("/api/desktop-state/selection", json={
        "revision": 0, "value": {"conversation_id": "c_draft"}})
    assert selected.status_code == 200
    drafts = client.get("/api/conversations").json()["conversations"]
    assert len(drafts) == 1 and drafts[0]["draft_only"] is True
    assert client.get("/api/conversations/c_draft").json()["messages"] == []
    # A fresh interpreter opens the same on-disk database, without browser state.
    script = """
import json
from app.db import SessionLocal, init_db
from app.models import DesktopState
init_db()
with SessionLocal() as db:
    print(json.dumps([db.get(DesktopState, 'selection').value,
                      db.get(DesktopState, 'conversation:c_draft').value]))
"""
    output = subprocess.check_output([sys.executable, "-c", script], env=dict(os.environ), text=True)
    restored = json.loads(output.strip())
    assert restored == [{"conversation_id": "c_draft"}, saved.json()["value"]]


def test_atomic_revisions_reject_other_window_and_preserve_its_snapshot():
    assert client.put(path, json={"revision": 0, "value": state("original")}).status_code == 200
    def write(value):
        return TestClient(app).put(path, json={"revision": 1, "value": state(value)})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["window A", "window B"]))
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(result.json() for result in results if result.status_code == 200)
    conflict = next(result.json()["detail"] for result in results if result.status_code == 409)
    assert conflict["revision"] == winner["revision"] == 2
    assert conflict["value"] == winner["value"]
    assert client.get(path).json() == winner
    assert client.put(path, json={"revision": 2, "value": state("resolved")}).status_code == 200


def test_delete_cleans_state_and_blocks_late_saves(db):
    cid = client.post("/api/conversations", json={"title": "delete state"}).json()["id"]
    url = f"/api/desktop-state/conversations/{cid}"
    assert client.put(url, json={"revision": 0, "value": state()}).status_code == 200
    assert client.put("/api/desktop-state/selection", json={
        "revision": 0, "value": {"conversation_id": cid}}).status_code == 200
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert db.get(DesktopState, f"conversation:{cid}") is None
    assert client.get(url).status_code == 410
    assert client.put(url, json={"revision": 1, "value": state("late")}).status_code == 410
    assert client.put(url, json={"revision": 0, "value": state("resurrect")}).status_code == 410
    assert client.put("/api/desktop-state/selection", json={
        "revision": 2, "value": {"conversation_id": cid}}).status_code == 410
    assert client.get("/api/desktop-state/selection").json() == {"revision": 2, "value": {"conversation_id": None}}


def test_state_validation_and_bounded_image_storage():
    invalid = [
        {"revision": -1, "value": state()},
        {"revision": 0, "value": state("x" * 200_001)},
        {"revision": 0, "value": state(images=[{"media_type": "image/svg+xml", "data": "YWJj"}])},
        {"revision": 0, "value": state(images=[{"media_type": "image/png", "data": "not base64"}])},
        {"revision": 0, "value": state(reading={"top": -1})},
        {"revision": 0, "value": state(images=[{"media_type": "image/png", "data": "YWJj"}] * 5)},
        {"revision": 0, "value": state(skills=["x" * 129])},
    ]
    for payload in invalid:
        assert client.put(path, json=payload).status_code == 422
    image = {"media_type": "image/png", "data": base64.b64encode(b"x" * (5 * 1024 * 1024)).decode()}
    assert client.put(path, json={"revision": 0, "value": state(images=[image, image])}).status_code == 422
    assert client.get(path).json()["revision"] == 0


def test_unsent_draft_history_rename_and_delete_without_creating_qa_rows(db):
    assert client.put(path, json={"revision": 0, "value": state("First draft line\nmore text")}).status_code == 200
    listed = client.get("/api/conversations").json()["conversations"]
    assert listed[0]["title"] == "First draft line"
    assert listed[0]["message_count"] == 0
    renamed = client.put("/api/conversations/c_draft", json={"title": "Named draft"})
    assert renamed.status_code == 200
    assert client.get("/api/conversations/c_draft").json()["messages"] == []
    assert client.delete("/api/conversations/c_draft").status_code == 200
    assert client.get(path).status_code == 410
    assert client.put("/api/desktop-state/conversations/c_delete_draft", json={"revision": 0, "value": state()}).status_code == 200
    assert client.delete("/api/conversations/c_delete_draft").status_code == 200
    assert client.get("/api/desktop-state/conversations/c_delete_draft").status_code == 410
