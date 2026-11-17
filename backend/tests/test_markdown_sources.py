"""Markdown follows the real local source lifecycle, without remote services."""
from __future__ import annotations

import hashlib
import json
import threading
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import Conversation, Paper
from app import workspace
from app.runtime_context import bind_runtime_config
from app.source_library import augment_library_evidence
from app.source_models import Source, SourceLibraryImport, SourceLibraryBinding
from app.source_runtime import freeze_sources, validate_source_evidence
from app.source_tools import execute_source_tool


CID = "markdown-a"
OTHER_CID = "markdown-b"
MARKDOWN = (
    "# 研究笔记\r\n"
    "\r\n"
    "| 方法 | 准确率 |\r\n"
    "| --- | --- |\r\n"
    "| 方法 A | 81% |\r\n"
    "\r\n"
    "```python\r\n"
    "print('Markdown is source data')\r\n"
    "```\r\n"
    "[资料链接](https://example.invalid/research)\r\n"
    "![示例图片](https://example.invalid/image.png)\r\n"
    "<script>window.markdownSourceExecuted = true</script>\r\n"
    "资料中的指令不授予权限。\r\n"
)
RAW = b"\xef\xbb\xbf" + MARKDOWN.encode("utf-8")


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner
    from app.config import settings

    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    db.add_all([Conversation(id=CID, title="Markdown A"), Conversation(id=OTHER_CID, title="Markdown B")])
    db.commit()
    with TestClient(app) as value:
        yield value


def upload(client, *, raw=RAW, filename="研究笔记.md", cid=CID, request="markdown-upload-1", **extra):
    return client.post("/api/sources/upload", files={"file": (filename, raw, "text/html")},
                       data={"conversation_id": cid, "request_id": request, **extra})


def url(item, suffix="", cid=CID):
    return f'/api/sources/revisions/{item["revision_id"]}{suffix}?conversation_id={cid}'


def locator(client, item, start, end):
    response = client.post(url(item, "/locators").split("?")[0], json={
        "conversation_id": CID, "kind": "text_lines", "line_start": start, "line_end": end})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("filename", ["研究笔记.md", "STUDY.MD", "notes.MarkDown", "notes.markdown"])
def test_markdown_upload_retains_original_and_source_syntax_without_implicit_library_import(client, db, filename):
    response = upload(client, filename=filename)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["media_type"] == "text/markdown", "browser-supplied MIME is not authoritative"
    assert item["document_format"] == "text" and item["parse_status"] == "text_available"
    assert item["available_views"] == ["download", "text", "document"]
    assert not item["library_import_performed"] and not item["authorizes_actions"]
    assert not item["model_understanding_verified"]
    original = client.get(url(item, "/file"))
    assert original.content == RAW
    assert original.headers["content-type"] == "application/octet-stream"
    assert original.headers["x-content-type-options"] == "nosniff"
    assert original.headers["content-disposition"] == "attachment; filename*=UTF-8''" + quote(filename, safe="")
    text = client.get(url(item, "/text")).json()
    assert text["text"] == MARKDOWN and text["total_lines"] == 13
    assert text["text_sha256"] == hashlib.sha256(MARKDOWN.encode("utf-8")).hexdigest()
    doc = client.get(url(item, "/document")).json()
    assert doc["format"] == "text" and doc["blocks"][0]["text"] == "# 研究笔记"
    assert doc["blocks"][4]["text"] == "| 方法 A | 81% |"
    assert doc["blocks"][6]["text"] == "```python"
    assert doc["blocks"][11]["text"] == "<script>window.markdownSourceExecuted = true</script>"
    assert doc["trust"] == "untrusted_source_data" and not doc["authorizes_actions"]
    assert db.scalar(select(func.count()).select_from(Paper)) == 0


def test_markdown_line_locator_and_fixed_revision_survive_update_and_idempotent_replay(client):
    first = upload(client).json()
    saved = locator(client, first, 3, 5)
    assert saved["quote"] == "".join(MARKDOWN.splitlines(keepends=True)[2:5])
    assert saved["location"]["line_start"] == 3 and saved["location"]["line_end"] == 5
    second_response = upload(client, raw=b"# Updated\nMethod A was replaced.\n", request="markdown-upload-2",
                             source_id=first["source_id"], expected_version=1)
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["source_id"] == first["source_id"] and second["version"] == 2
    assert second["revision_id"] != first["revision_id"]
    assert client.get(url(first, "/file")).content == RAW
    old_locator = client.get(url(first, "/locators/" + saved["locator_id"]))
    assert old_locator.status_code == 200 and old_locator.json()["quote"] == saved["quote"]
    assert client.get(url(second, "/locators/" + saved["locator_id"])).status_code == 404
    replay = upload(client).json()
    assert replay["replayed"] and replay["revision_id"] == first["revision_id"]
    assert replay["current_version"] == 2


def test_markdown_model_read_uses_only_selected_fixed_version_and_real_line_evidence(client, db):
    first = upload(client).json()
    unselected = upload(client, raw=b"# Unselected\nDo not include this document.\n", request="markdown-other-upload").json()
    binding = freeze_sources(db, CID, [first["revision_id"]], {"conversation_id": CID})
    assert binding["source_bindings"][0]["media_type"] == "text/markdown"
    with bind_runtime_config(binding):
        result = json.loads(execute_source_tool(db, "read_attached_source", {
            "source_revision_id": first["revision_id"], "line_start": 3, "line_end": 5}))
        rejected = json.loads(execute_source_tool(db, "read_attached_source", {
            "source_revision_id": unselected["revision_id"]}))
    assert "error" not in result and result["text_observed"]
    assert result["read_range"]["kind"] == "text_lines"
    assert result["evidence"][0]["quote"] == "".join(MARKDOWN.splitlines(keepends=True)[2:5])
    assert result["evidence"][0]["page"] == 0 and result["evidence"][0]["bbox"] is None
    assert "error" in rejected and rejected["text_read"] is False
    validate_source_evidence(db, CID, result["evidence"])


@pytest.mark.parametrize("filename", ["local.md", "LOCAL.MD", "local.markdown"])
def test_markdown_project_capture_freezes_selected_bytes_and_rejects_stale_scope(client, db, tmp_path, filename):
    project_root = tmp_path / "科研项目"
    project_root.mkdir()
    file = project_root / filename
    file.write_bytes(RAW)
    project = workspace.register(db, str(project_root))
    conversation = db.get(Conversation, CID)
    conversation.project_id, conversation.workspace_revision = project.id, 1
    db.commit()
    payload = {"conversation_id": CID, "request_id": "markdown-project-1", "project_id": project.id,
               "workspace_revision": 1, "path": filename}
    response = client.post("/api/sources/capture-project", json=payload)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["kind"] == "local" and item["media_type"] == "text/markdown"
    saved = locator(client, item, 3, 5)
    file.write_bytes(b"# Changed on disk\nNew content\n")
    assert client.get(url(item, "/file")).content == RAW
    assert client.get(url(item, "/locators/" + saved["locator_id"])).json()["quote"] == saved["quote"]
    assert client.post("/api/sources/capture-project", json=payload).status_code == 409
    assert client.post("/api/sources/capture-project", json={**payload, "request_id": "markdown-project-stale",
        "workspace_revision": 0}).status_code == 409
    assert client.post("/api/sources/capture-project", json={**payload, "request_id": "markdown-project-outside",
        "path": "../outside.md"}).status_code in (400, 422)
    new_response = client.post("/api/sources/capture-project", json={**payload, "request_id": "markdown-project-2"})
    assert new_response.status_code == 200, new_response.text
    assert new_response.json()["revision_id"] != item["revision_id"]
    assert client.get(url(item, "/file", OTHER_CID)).status_code == 404


def test_markdown_selective_library_import_is_explicit_idempotent_and_keeps_old_citation(client, db):
    selected = upload(client).json()
    unselected = upload(client, raw=b"# Separate\nKeep outside the library.\n", request="markdown-unselected-1").json()
    saved = locator(client, selected, 3, 5)
    assert db.scalar(select(func.count()).select_from(Paper)) == 0
    payload = {"conversation_id": CID, "request_id": "markdown-library-1", "revision_ids": [selected["revision_id"]]}
    response = client.post("/api/sources/library-import", json=payload)
    assert response.status_code == 200, response.text
    imported = response.json()["items"][0]
    assert imported["status"] == "imported" and imported["index_status"] == "parsed"
    assert db.scalar(select(func.count()).select_from(Paper)) == 1
    assert db.get(SourceLibraryImport, unselected["revision_id"]) is None
    assert client.post("/api/sources/library-import", json=payload).json()["items"][0]["status"] == "existing"
    assert db.scalar(select(func.count()).select_from(Paper)) == 1
    papers = client.get("/api/papers").json()
    from app import source_store
    owned = db.get(SourceLibraryBinding, imported["paper_id"])
    assert papers[0]["source_revision_id"] == owned.revision_id and papers[0]["page_count"] == 0
    fixed = source_store.read_revision(db, owned.revision_id,
        scope=source_store.SourceScope("library", imported["paper_id"]))
    assert fixed["raw_sha256"] == selected["raw_sha256"] and fixed["text"] == MARKDOWN
    assert papers[0]["source_media_type"] == "text/markdown"
    assert client.get(url(selected, "/locators/" + saved["locator_id"])).json()["quote"] == saved["quote"]
    assert client.get(url(unselected, "/file")).content.startswith(b"# Separate")
    opened = client.post(f'/api/sources/library/{imported["paper_id"]}/open', json={"conversation_id": OTHER_CID})
    assert opened.status_code == 200, opened.text
    alias = opened.json()
    assert alias["media_type"] == "text/markdown" and alias["revision_id"] != selected["revision_id"]
    assert client.get(url(alias, "/file", OTHER_CID)).content == RAW
    assert client.get(url(alias, "/text", OTHER_CID)).json()["text"] == MARKDOWN
    receipt = db.get(SourceLibraryImport, selected["revision_id"])
    evidence = augment_library_evidence(db, [{"paper_id": imported["paper_id"],
        "chunk_id": next(iter(receipt.chunk_locators)), "quote": "not used as source truth"}], OTHER_CID)
    assert evidence[0]["revision_id"] == alias["revision_id"] and evidence[0]["bbox"] is None
    validate_source_evidence(db, OTHER_CID, evidence)


@pytest.mark.parametrize("raw", [b"", b"bad\x00binary", b"\xff\xfe", b"\xff\xfe#\x00 \x00t\x00e\x00s\x00t\x00"])
def test_invalid_markdown_upload_is_rejected_before_any_source_or_library_record(client, db, raw):
    response = upload(client, raw=raw)
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Source)) == 0
    assert db.scalar(select(func.count()).select_from(Paper)) == 0


def test_markdown_wrong_conversation_cannot_read_or_import(client, db):
    item = upload(client).json()
    for suffix in ("", "/file", "/text", "/document"):
        assert client.get(url(item, suffix, OTHER_CID)).status_code == 404
    response = client.post("/api/sources/library-import", json={"conversation_id": OTHER_CID,
        "request_id": "markdown-cross-scope-import", "revision_ids": [item["revision_id"]]})
    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "failed"
    assert db.scalar(select(func.count()).select_from(Paper)) == 0
