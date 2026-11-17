import sqlite3
import threading
import time
import io
import json
import zipfile
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import backup_service as service, data_backup as core, maintenance, run_store
from app.main import app

client = TestClient(app)


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    with closing(sqlite3.connect(root / "scholarflow.db")) as db:
        db.execute("CREATE TABLE example(value TEXT)")
        db.execute("INSERT INTO example VALUES ('live')")
        db.commit()
    (root / "notes.txt").write_text("application data", encoding="utf-8")
    monkeypatch.setattr(service, "DATA_DIR", root)
    maintenance.set_restore_pending(False)
    yield root
    service.wait_shutdown()
    maintenance.set_restore_pending(False)


def finish(response):
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = client.get("/api/backups/jobs/" + job_id).json()
        if value["status"] not in ("running", "queued"):
            return value
        time.sleep(.02)
    pytest.fail("Backup operation did not finish")


def test_backup_download_preview_confirm_cancel(data):
    exported = finish(client.post("/api/backups/create"))
    assert exported["status"] == "succeeded", exported
    result = exported["result"]
    response = client.get(result["download_url"])
    assert response.status_code == 200
    preview = finish(client.post("/api/backups/restore/prepare", files={"file": ("my.sfbak", response.content)}))
    assert preview["status"] == "succeeded", preview
    restore_id = preview["result"]["id"]
    assert client.post("/api/backups/restore/confirm", json={"restore_id": restore_id}).status_code == 422
    scheduled = finish(client.post("/api/backups/restore/confirm", json={"restore_id": restore_id, "confirm_replace": True}))
    assert scheduled["status"] == "succeeded", scheduled
    assert client.get("/api/settings").status_code == 423
    assert client.get("/api/backups").json()["pending_restore"]["id"] == restore_id
    assert (data / "notes.txt").read_text() == "application data"
    assert finish(client.post("/api/backups/restore/cancel"))["status"] == "succeeded"
    assert client.get("/api/settings").status_code == 200


def test_http_export_has_no_credential_inclusion_switch(data):
    secret = "test-key-898e6fe8ba97fdd7"
    (data / "config.json").write_text(json.dumps({"llm_api_key": secret,
        "embedding_api_key": secret, "protected_credentials": {"llm_api_key": "dpapi:" + secret}}), encoding="utf-8")
    exported = finish(client.post("/api/backups/create?include_credentials=true",
        json={"include_credentials": True, "_private_rollback": True}))
    assert exported["status"] == "succeeded", exported
    assert exported["result"]["credential_portability"] == "credentials_excluded_reconfigure"
    assert client.get("/api/backups").json()["credential_portability"] == "credentials_excluded_reconfigure"
    response = client.get(exported["result"]["download_url"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert all(secret.encode() not in archive.read(name) for name in archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["credential_portability"] == "credentials_excluded_reconfigure"
    assert secret in (data / "config.json").read_text()


def test_running_task_refuses_backup_without_cancellation(data, monkeypatch):
    monkeypatch.setattr(run_store, "active_runs", lambda: {"active": "conversation"})
    result = finish(client.post("/api/backups/create"))
    assert result["status"] == "failed" and "任务没有被取消" in result["error"]
    assert (data / "notes.txt").exists()


def test_external_worker_cannot_claim_consistent_backup(data, monkeypatch):
    monkeypatch.setattr(service.settings, "redis_url", "redis://127.0.0.1:6379")
    result = finish(client.post("/api/backups/create"))
    assert result["status"] == "failed" and "外部任务队列" in result["error"]


def test_parallel_job_refused_and_stale_job_is_interrupted(data, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = core.create_backup
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(6)
        return real(*args, **kwargs)
    monkeypatch.setattr(core, "create_backup", held)
    response = client.post("/api/backups/create")
    assert entered.wait(3)
    try:
        assert client.post("/api/backups/create").status_code == 409
        assert client.post("/api/conversations", json={"title": "blocked"}).status_code == 423
    finally:
        release.set()
    assert finish(response)["status"] == "succeeded"
    record = {"id": "a" * 32, "status": "running"}
    service._save(record)
    assert service.job(record["id"])["status"] == "interrupted"


def test_bad_archive_and_untrusted_origin(data):
    result = finish(client.post("/api/backups/restore/prepare", files={"file": ("invalid.sfbak", b"broken")}))
    assert result["status"] == "failed"
    assert client.post("/api/backups/create", headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert (data / "notes.txt").exists()


def test_maintenance_drains_existing_operation_and_blocks_new():
    started, ended, release = threading.Event(), threading.Event(), threading.Event()
    def operation():
        with maintenance.activity():
            started.set()
            assert release.wait(3)
        ended.set()
    thread = threading.Thread(target=operation)
    thread.start()
    assert started.wait(2)
    with pytest.raises(maintenance.MaintenanceBusy):
        with maintenance.exclusive(timeout=.05):
            pass
    release.set()
    thread.join(3)
    assert ended.is_set()
    with maintenance.exclusive():
        with pytest.raises(maintenance.MaintenanceBusy):
            maintenance.enter()


def test_scheduler_respects_restore_freeze(monkeypatch):
    from app.tracking.scheduler import TrackingScheduler
    scheduler = TrackingScheduler()
    calls = []
    monkeypatch.setattr(scheduler, "_tick_admitted", lambda: calls.append("ran"))
    maintenance.set_restore_pending(True)
    try:
        assert scheduler.tick() == 0
        assert calls == []
    finally:
        maintenance.set_restore_pending(False)
