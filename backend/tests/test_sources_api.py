"""Real local API source lifecycle; no model, real files, or provider calls."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.main import app
from app.models import Conversation, DeletedConversation, Paper
from app.source_models import Source, SourceCaptureReceipt, SourceLocator, SourceRevision, SourceSnapshot
from app.timeutil import utcnow


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    db.add_all([Conversation(id="source-a", title="Source A"), Conversation(id="source-b", title="Source B")])
    db.commit()
    with TestClient(app) as value:
        yield value


def upload(client, raw=b"first line\nsecond line\nthird line", *, filename="资料.txt", cid="source-a",
           request="upload-request-1", **extra):
    return client.post("/api/sources/upload", files={"file": (filename, raw, "text/html")},
                       data={"conversation_id": cid, "request_id": request, **extra})


def url(revision, suffix="", cid="source-a"):
    return f"/api/sources/revisions/{revision}{suffix}?conversation_id={cid}"


def test_upload_does_not_import_library_and_original_text_are_fixed_and_untrusted(client, db):
    response = upload(client)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["version"] == 1 and item["parse_status"] == "text_available"
    assert item["available_views"] == ["download", "text", "document"] and not item["library_import_performed"]
    assert item["media_type"] == "text/plain", "browser MIME is untrusted"
    assert item["authorizes_actions"] is False and item["model_understanding_verified"] is False
    assert "raw_bytes" not in item and "text" not in item
    revision = item["revision_id"]
    download = client.get(url(revision, "/file"))
    assert download.content == b"first line\nsecond line\nthird line"
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"] == "attachment; filename*=UTF-8''" + quote("资料.txt", safe="")
    assert download.headers["cache-control"] == "no-store"
    page = client.get(url(revision, "/text") + "&line_start=2&line_limit=1").json()
    assert page["text"] == "second line\n" and page["next_line"] == 3 and page["line_start"] == 2
    assert db.scalar(select(func.count()).select_from(Paper)) == 0
    assert client.get("/api/sources?conversation_id=source-a").json()["items"][0]["revision_id"] == revision


def test_upload_idempotency_conflict_and_concurrent_identical_retries(client, db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(client), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    items = [r.json() for r in responses]
    assert items[0]["revision_id"] == items[1]["revision_id"]
    assert sorted(item["replayed"] for item in items) == [False, True]
    assert upload(client, raw=b"changed").status_code == 409
    for model in (Source, SourceRevision, SourceSnapshot, SourceCaptureReceipt):
        assert db.scalar(select(func.count()).select_from(model)) == 1


def test_old_revision_locator_and_original_survive_update_and_replay_ignores_new_head(client):
    first = upload(client).json()
    locator = client.post(url(first["revision_id"], "/locators").split("?")[0], json={
        "conversation_id": "source-a", "kind": "text_lines", "line_start": 2, "line_end": 2}).json()
    second = upload(client, raw=b"new text", request="upload-request-2",
                    source_id=first["source_id"], expected_version=1).json()
    assert second["version"] == 2 and second["revision_id"] != first["revision_id"]
    assert client.get(url(first["revision_id"], "/text")).json()["text"].startswith("first line")
    old = client.get(url(first["revision_id"], "/locators/" + locator["locator_id"]))
    assert old.status_code == 200 and old.json()["quote"] == "second line\n"
    assert client.get(url(second["revision_id"], "/locators/" + locator["locator_id"])).status_code == 404
    replay = upload(client).json()
    assert replay["replayed"] and replay["revision_id"] == first["revision_id"]
    assert replay["current_version"] == 2
    stale = upload(client, request="upload-request-3", source_id=first["source_id"], expected_version=1)
    assert stale.status_code == 409


def test_real_pdf_locator_comes_from_original_bytes_and_corrupt_office_is_honestly_original_only(client, sample_pdf):
    item = upload(client, raw=sample_pdf.read_bytes(), filename="article.pdf").json()
    assert item["parse_status"] == "text_available" and item["available_views"] == ["download", "text", "pdf_page", "document"]
    assert "Attention" in client.get(url(item["revision_id"], "/text")).json()["text"]
    response = client.post(url(item["revision_id"], "/locators").split("?")[0], json={
        "conversation_id": "source-a", "kind": "pdf_page", "page_index": 0})
    assert response.status_code == 200, response.text
    locator = response.json()
    assert locator["location"]["geometry_verified"] is True and locator["location"]["page_number"] == 1
    assert "Attention" in locator["quote"]
    office = upload(client, raw=b"synthetic office original", filename="article.docx", request="office-upload-1").json()
    assert office["parse_status"] == "original_only" and office["available_views"] == ["download"]
    assert client.get(url(office["revision_id"], "/text")).status_code == 409


def test_all_reads_and_writes_require_existing_non_deleted_conversation_scope(client, db):
    item = upload(client).json()
    revision = item["revision_id"]
    for suffix in ("", "/file", "/text"):
        assert client.get(url(revision, suffix, "source-b")).status_code == 404
        assert client.get(url(revision, suffix, "unknown")).status_code == 404
    assert upload(client, cid="unknown").status_code == 404
    assert db.get(Conversation, "unknown") is None
    db.get(Conversation, "source-a").archived_at = utcnow()
    db.commit()
    assert client.get(url(revision, "/file")).status_code == 200
    assert upload(client).status_code == 409, "archive authorization is checked before idempotent replay"
    assert client.post(url(revision, "/locators").split("?")[0], json={
        "conversation_id": "source-a", "kind": "text_lines", "line_start": 1, "line_end": 1}).status_code == 409
    db.add(DeletedConversation(id="source-a"))
    db.commit()
    assert upload(client).status_code == 410
    for suffix in ("", "/file", "/text"):
        assert client.get(url(revision, suffix)).status_code == 410
    assert client.get("/api/sources?conversation_id=source-a").status_code == 410


@pytest.mark.parametrize("name,raw,status", [
    ("../secret.txt", b"text", 422), ("foo.html", b"<script>alert(1)</script>", 415),
    ("empty.txt", b"", 422), ("binary.txt", b"text\x00raw", 422), ("bad.txt", b"\xff\xfe", 422),
])
def test_filename_is_never_a_path_and_invalid_text_or_content_is_rejected(client, db, name, raw, status):
    assert upload(client, raw=raw, filename=name).status_code == status
    assert db.scalar(select(func.count()).select_from(Source)) == 0


@pytest.mark.parametrize("filename", ["C:\\private.txt", "line\nname.txt"])
def test_multipart_normalized_filename_is_inert_and_never_opened(client, filename):
    # Starlette strips an old Windows browser's path prefix; HTTPX percent
    # escapes a filename newline. Both are inert display names at this point.
    result = upload(client, raw=b"only bytes supplied by user", filename=filename)
    assert result.status_code == 200
    item = result.json()
    assert "\\" not in item["title"] and "\n" not in item["title"]
    original = client.get(url(item["revision_id"], "/file"))
    assert original.content == b"only bytes supplied by user"
    assert len(original.headers.get_list("Content-Disposition")) == 1


@pytest.mark.parametrize("filename", ["C:\\private.txt", "line\nname.txt", "../secret.txt"])
def test_unprocessed_filename_path_or_control_is_rejected_before_read(filename):
    from fastapi import HTTPException, UploadFile
    from app.api.sources import _upload_content
    class NoRead(BytesIO):
        def read(self, *args, **kwargs):
            pytest.fail("invalid filename must not read or open anything")
    with pytest.raises(HTTPException) as caught:
        _upload_content(UploadFile(file=NoRead(b"data"), filename=filename))
    assert caught.value.status_code == 422


def test_upload_limit_and_client_forged_locator_are_rejected(client, monkeypatch):
    from app.api import sources
    monkeypatch.setattr(sources, "MAX_UPLOAD_BYTES", 4)
    assert upload(client, raw=b"12345").status_code == 413
    item = upload(client, raw=b"1234").json()
    base = url(item["revision_id"], "/locators").split("?")[0]
    for extra in ({"quote": "fabricated"}, {"geometry_verified": True}, {"page_index": 1}):
        assert client.post(base, json={"conversation_id": "source-a", "kind": "text_lines",
                                       "line_start": 1, "line_end": 1, **extra}).status_code == 422


def test_locator_duplicates_are_serialized_and_bad_old_snapshot_never_falls_back(client, db):
    item = upload(client).json()
    base = url(item["revision_id"], "/locators").split("?")[0]
    request = {"conversation_id": "source-a", "kind": "text_lines", "line_start": 1, "line_end": 2}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(base, json=request), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json()["locator_id"] == responses[1].json()["locator_id"]
    assert db.scalar(select(func.count()).select_from(SourceLocator)) == 1
    db.execute(text("UPDATE source_snapshots SET raw_bytes = :value WHERE id = :id"),
               {"value": b"corrupt", "id": item["snapshot_id"]})
    db.commit()
    for suffix in ("", "/file", "/text", "/locators/" + responses[0].json()["locator_id"]):
        assert client.get(url(item["revision_id"], suffix)).status_code == 409


def test_paginated_list_does_not_read_blob_or_raw_text(client, monkeypatch):
    from app.api import sources
    first = upload(client).json()
    upload(client, raw=b"two", request="upload-request-2")
    monkeypatch.setattr(sources.store, "read_revision", lambda *args, **kwargs: pytest.fail("list loaded full blob"))
    page = client.get("/api/sources?conversation_id=source-a&limit=1").json()
    assert len(page["items"]) == 1 and page["next_offset"] == 1
    next_page = client.get("/api/sources?conversation_id=source-a&limit=1&offset=1").json()
    assert next_page["next_offset"] is None and next_page["items"][0]["revision_id"] == first["revision_id"]
    assert "text" not in next_page["items"][0] and "raw_bytes" not in next_page["items"][0]


def test_same_bytes_in_different_conversations_never_share_snapshot(client):
    first = upload(client).json()
    other = upload(client, cid="source-b").json()
    assert first["snapshot_id"] != other["snapshot_id"] and first["source_id"] != other["source_id"]


def test_replay_rejects_receipt_pointer_retargeted_to_same_raw_but_other_capture(client, db):
    from app import source_store as store
    first = upload(client).json()
    scope = store.SourceScope("conversation", "source-a")
    other = store.capture_source(db, scope=scope, authorization=store.SourceAuthorization(scope, "another-capture", "upload"),
        kind="upload", title=first["title"], raw_bytes=b"first line\nsecond line\nthird line",
        text="different parsed text", media_type=first["media_type"], text_origin="parsed_text", parser="different-parser")
    db.execute(text("UPDATE source_capture_receipts SET revision_id = :id WHERE request_id = 'upload-request-1'"),
               {"id": other["revision_id"]})
    db.commit()
    result = upload(client)
    assert result.status_code == 409 and "回执" in result.json()["detail"]


def test_unbound_legacy_receipt_is_not_silently_trusted(client, db):
    upload(client)
    db.execute(text("UPDATE source_capture_receipts SET revision_fingerprint = NULL"))
    db.commit()
    assert upload(client).status_code == 409


def test_pdf_preparation_releases_database_and_rechecks_archive_before_save(client, db, sample_pdf, monkeypatch):
    from app.api import sources
    from app.db import SessionLocal
    item = upload(client, raw=sample_pdf.read_bytes(), filename="article.pdf").json()
    original = sources.store.derive_pdf_page
    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(sources.store, "derive_pdf_page", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.post, url(item["revision_id"], "/locators").split("?")[0], json={
            "conversation_id": "source-a", "kind": "pdf_page", "page_index": 0})
        try:
            assert entered.wait(10)
            def archive():
                with SessionLocal.begin() as writer:
                    writer.execute(text("BEGIN IMMEDIATE"))
                    writer.get(Conversation, "source-a").archived_at = utcnow()
            # Another conversation's checkpoint writes share this same SQLite
            # writer. A pending PDF parse must not occupy that global slot.
            pool.submit(archive).result(timeout=3)
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 409
    assert db.scalar(select(func.count()).select_from(SourceLocator)) == 0


def test_pdf_boolean_coordinate_does_not_coerce_to_real_geometry(client, sample_pdf):
    item = upload(client, raw=sample_pdf.read_bytes(), filename="article.pdf").json()
    result = client.post(url(item["revision_id"], "/locators").split("?")[0], json={
        "conversation_id": "source-a", "kind": "pdf_page", "page_index": 0, "bbox": [False, True, 100, 100]})
    assert result.status_code == 422
