"""Version history and authorized local updates; synthetic documents only."""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import Conversation, DeletedConversation
from app.source_models import SourceRevision, SourceSnapshot
from app import workspace
from app.timeutil import utcnow


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    db.add_all([Conversation(id="versions-a", title="A"), Conversation(id="versions-b", title="B")])
    db.commit()
    with TestClient(app) as value:
        yield value


def upload(client, request, content, **extra):
    response = client.post("/api/sources/upload", files={"file": ("note.md", content, "text/markdown")},
        data={"conversation_id": "versions-a", "request_id": request, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def history_url(source, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f'/api/sources/{source["source_id"]}/history?conversation_id=versions-a' + ("&" + query if query else "")


def test_history_cursor_stays_stable_when_new_version_is_added(client, db):
    first = upload(client, "history-first", b"v1")
    second = upload(client, "history-second", b"v2", source_id=first["source_id"], expected_version=1)
    head = client.get(history_url(first, limit=1)).json()
    assert head["current_revision_id"] == second["revision_id"]
    assert head["items"][0]["version"] == 2 and head["next_before_version"] == 2
    upload(client, "history-third", b"v3", source_id=first["source_id"], expected_version=2)
    page = client.get(history_url(first, before_version=head["next_before_version"], limit=1)).json()
    assert [item["revision_id"] for item in page["items"]] == [first["revision_id"]]
    assert page["next_before_version"] is None and page["current_version"] == 3
    item = page["items"][0]
    assert item["created_at"].endswith("Z") and len(item["raw_sha256"]) == 64
    assert item["parse_status"] == "text_available" and item["document_format"] == "text"
    assert "raw_bytes" not in item and "text" not in item and not item["integrity_verified"]
    assert db.scalar(select(func.count()).select_from(SourceRevision)) == 3


def test_history_authorizes_before_enumeration_and_does_not_change_head(client, db):
    first = upload(client, "history-scope", b"private version")
    endpoint = f'/api/sources/{first["source_id"]}/history'
    assert client.get(endpoint, params={"conversation_id": "versions-b"}).status_code == 404
    assert client.get(endpoint, params={"conversation_id": "unknown"}).status_code == 404
    db.get(Conversation, "versions-a").archived_at = utcnow()
    db.commit()
    assert client.get(history_url(first)).status_code == 200
    db.add(DeletedConversation(id="versions-a"))
    db.commit()
    assert client.get(history_url(first)).status_code == 410


def test_selected_historical_version_import_does_not_import_newer(client):
    first = upload(client, "history-import-1", b"old original reference")
    second = upload(client, "history-import-2", b"new original reference", source_id=first["source_id"], expected_version=1)
    payload = {"conversation_id": "versions-a", "request_id": "import-only-v1", "revision_ids": [first["revision_id"]]}
    response = client.post("/api/sources/library-import", json=payload)
    assert response.status_code == 200 and response.json()["items"][0]["paper_id"]
    again = client.post("/api/sources/library-import", json=payload).json()
    assert again["items"][0]["status"] == "existing"
    items = client.get(history_url(first)).json()["items"]
    assert [item["version"] for item in items] == [2, 1]
    assert items[0]["library_paper_id"] is None and items[1]["library_paper_id"]
    assert items[0]["revision_id"] == second["revision_id"]
    assert client.get('/api/sources?conversation_id=versions-a').json()["items"][0]["library_paper_id"] is None
    client.post("/api/sources/library-import", json={**payload, "request_id": "import-only-v2", "revision_ids": [second["revision_id"]]})
    assert client.get('/api/sources?conversation_id=versions-a').json()["items"][0]["library_paper_id"]


def test_project_updates_bind_path_scope_cas_and_retry_receipt(client, db, tmp_path):
    root = tmp_path / "versioned-project"
    root.mkdir()
    path = root / "note.md"
    path.write_text("v1 local original", encoding="utf-8")
    (root / "other.md").write_text("other original", encoding="utf-8")
    project = workspace.register(db, str(root))
    conversation = db.get(Conversation, "versions-a")
    conversation.project_id, conversation.workspace_revision = project.id, 1
    db.commit()
    payload = {"conversation_id": "versions-a", "request_id": "capture-project-v1", "project_id": project.id,
               "workspace_revision": 1, "path": "note.md"}
    first = client.post("/api/sources/capture-project", json=payload).json()
    path.write_text("v2 local original", encoding="utf-8")
    update = {**payload, "request_id": "capture-project-v2", "source_id": first["source_id"], "expected_version": 1}
    second = client.post("/api/sources/capture-project", json=update)
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2 and second.json()["source_id"] == first["source_id"]
    assert client.post("/api/sources/capture-project", json=update).json()["replayed"]
    old = client.get(f'/api/sources/revisions/{first["revision_id"]}/file?conversation_id=versions-a')
    assert old.content == b"v1 local original"
    stale = client.post("/api/sources/capture-project", json={**update, "request_id": "capture-project-stale"})
    assert stale.status_code == 409
    changed_path = client.post("/api/sources/capture-project", json={**update, "request_id": "capture-other-path", "path": "other.md", "expected_version": 2})
    assert changed_path.status_code == 409
    foreign = client.post("/api/sources/capture-project", json={**update, "request_id": "capture-wrong-scope", "conversation_id": "versions-b"})
    assert foreign.status_code in (403, 409)
    assert client.post("/api/sources/capture-project", json={**update, "expected_version": None}).status_code == 422
    assert db.scalar(select(func.count()).select_from(SourceRevision)) == 2
    assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 2
    history = client.get(history_url(first)).json()
    assert history["current_version"] == 2
