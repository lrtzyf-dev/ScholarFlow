"""Real quota policy HTTP, authoritative routing and bounded threaded load."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from app.main import app
from app import account_quotas as quotas, request_resources as resources
from app.models import Conversation
from app.model_profiles import capture_run_config, public_runtime_config
from app.quota_models import QuotaGroup, QuotaMember

client = TestClient(app)
BASE = "/api/account-quotas"


@pytest.fixture(autouse=True)
def isolated_scheduler(monkeypatch):
    monkeypatch.setattr(resources, "scheduler", resources.FairRequestScheduler())


def state():
    response = client.get(BASE); assert response.status_code == 200, response.text
    return response.json()


def profile(name="One", key="synthetic-quota-secret-A", endpoint="https://quota.example.test/v1"):
    response = client.post("/api/model-profiles", json={"name": name, "base_url": endpoint,
        "model_id": "synthetic-model", "api_key": key})
    assert response.status_code == 200, response.text
    return response.json()


def group(name="Account", limit=2, endpoint="https://quota.example.test"):
    before = state()
    response = client.post(BASE + "/groups", json={"expected_revision": before["revision"],
        "name": name, "origin": endpoint, "concurrency": limit})
    assert response.status_code == 200, response.text
    return response.json()["groups"][-1]


def bind(selected, target, *, expected=None, config=None):
    before = state()
    account = next(a for a in before["accounts"] if a["id"] == "profile:" + selected["id"])
    return client.post(BASE + f'/groups/{target["id"]}/bind', json={
        "expected_revision": before["revision"] if expected is None else expected,
        "account_id": account["id"], "config_fingerprint": config or account["config_fingerprint"]})


def key(target):
    return resources.quota_key(provider="test", endpoint=target["origin"], quota_group_id=target["id"])


def update(target, limit, *, revision=None, status="active"):
    return client.put(BASE + f'/groups/{target["id"]}', json={
        "expected_revision": state()["revision"] if revision is None else revision,
        "name": target["name"], "concurrency": limit, "state": status})


def eventually(predicate, timeout=4):
    until = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < until
        time.sleep(0.005)


def test_http_groups_bind_explicit_keys_same_credential_profiles_share_without_secret_exposure(db):
    one = profile(); two = profile("Two", "synthetic-quota-secret-B"); same = profile("Same key")
    target = group()
    assert bind(one, target).status_code == 200
    assert bind(two, target).status_code == 200
    value = state()
    assert all(next(a for a in value["accounts"] if a["id"] == "profile:" + p["id"])["group_id"] == target["id"] for p in (one, two, same))
    assert db.scalar(select(func.count()).select_from(QuotaMember)) == 2
    assert value["default_concurrency"] == 2
    assert value["policy"] == "round_robin_sessions"
    assert "synthetic-quota-secret" not in json.dumps(value)
    assert "credential_fingerprint" not in json.dumps(value)


@pytest.mark.parametrize("change", [{"concurrency": True}, {"concurrency": 0}, {"concurrency": 65},
    {"name": " "}, {"origin": "https://quota.example.test/v1"},
    {"origin": "https://user:secret@quota.example.test"}, {"origin": "file:///tmp"},
    {"api_key": "synthetic-should-not-echo"}])
def test_http_invalid_policy_is_atomic_and_does_not_echo_accidental_keys(db, change):
    response = client.post(BASE + "/groups", json={"expected_revision": 0, "name": "Account",
        "origin": "https://quota.example.test", "concurrency": 2, **change})
    assert response.status_code == 422
    assert "synthetic-should-not-echo" not in response.text
    assert db.scalar(select(func.count()).select_from(QuotaGroup)) == 0
    assert state()["revision"] == 0


def test_cross_origin_stale_credential_and_retired_binding_are_rejected(db):
    one = profile(); target = group(endpoint="https://different.example.test")
    assert bind(one, target).status_code == 422
    same = group("Correct")
    original = next(a for a in state()["accounts"] if a["id"] == "profile:" + one["id"])
    changed = client.put('/api/model-profiles/' + one["id"], json={"expected_version": one["version"], "api_key": "synthetic-quota-secret-new"})
    assert changed.status_code == 200
    assert bind(one, same, config=original["config_fingerprint"]).status_code == 409
    assert update(same, 2, status="retired").status_code == 200
    assert bind(one, same).status_code == 409
    assert db.scalar(select(func.count()).select_from(QuotaMember)) == 0


def test_global_cas_across_two_windows_has_one_winner_and_preserves_limits():
    target = group(); revision = state()["revision"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda limit: update(target, limit, revision=revision).status_code, (4, 8)))
    assert sorted(results) == [200, 409]
    assert state()["revision"] == revision + 1
    assert state()["groups"][0]["concurrency"] in (4, 8)


def test_old_run_audit_binding_is_retained_but_resume_uses_current_authoritative_member(db):
    one = profile(); first = group("Old", 2); second = group("New", 4)
    assert bind(one, first).status_code == 200
    db.add(Conversation(id="quota-audit")); db.commit()
    client.put('/api/conversation-options/quota-audit', json={"expected_revision": 0, "model_profile_id": one["id"]})
    runtime = capture_run_config(db, "quota-audit"); db.commit(); frozen = deepcopy(runtime)
    assert runtime["quota_binding"]["initial_group_id"] == first["id"]
    assert "credential_fingerprint" not in json.dumps(public_runtime_config(runtime))
    assert bind(one, second).status_code == 200
    fp = quotas.credential_hash("synthetic-quota-secret-A")
    with resources.request_admission(provider="test", endpoint=one["base_url"], credential_fingerprint=fp,
            runtime_config=runtime, service="chat"):
        assert resources.scheduler.groups[key(second)].active
        assert key(first) not in resources.scheduler.groups
    assert runtime == frozen
    assert quotas.resolve_request(db, one["base_url"], fp)["group_id"] == second["id"]


@pytest.mark.parametrize("busy", ["source", "target", "queued", "ungrouped"])
def test_explicit_migration_requires_both_current_and_target_queues_idle(busy):
    one = profile(); first = group("First", 1); second = group("Second", 1)
    if busy != "ungrouped":
        assert bind(one, first).status_code == 200
    busy_key = key(second if busy == "target" else first) if busy != "ungrouped" else resources.quota_key(
        provider="test", endpoint=one["base_url"], credential_fingerprint=quotas.credential_hash("synthetic-quota-secret-A"))
    held = resources.scheduler.acquire(busy_key, session="held")
    cancellation = threading.Event()
    def wait():
        return resources.scheduler.acquire(busy_key, session="waiting", cancelled=cancellation.is_set)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(wait) if busy == "queued" else None
        if waiting:
            eventually(lambda: len(resources.scheduler.snapshot()["items"]) == 2)
        revision = state()["revision"]
        rejected = bind(one, second)
        assert rejected.status_code == 409 and state()["revision"] == revision
        cancellation.set()
        if waiting:
            with pytest.raises(resources.ResourceRequestCancelled):
                waiting.result(timeout=4)
        resources.scheduler.release(busy_key, held)
    assert bind(one, second).status_code == 200


def test_late_old_resolution_cannot_join_old_group_after_explicit_migration(db):
    one = profile(); first = group("First"); second = group("Second")
    assert bind(one, first).status_code == 200
    fp = quotas.credential_hash("synthetic-quota-secret-A")
    old = quotas.resolve_request(db, one["base_url"], fp); db.rollback()
    token = resources.scheduler.apply_policy(old["policy"])
    assert bind(one, second).status_code == 200
    with pytest.raises(resources.QuotaPolicyChanged):
        resources.scheduler.apply_policy(old["policy"])
    with pytest.raises(resources.QuotaPolicyChanged):
        resources.scheduler.acquire(key(first), session="late", policy_token=token)
    assert not resources.scheduler.snapshot()["items"]


def test_current_chat_group_is_never_lent_to_other_embedding_or_service_credentials():
    one = profile(); target = group(limit=1); assert bind(one, target).status_code == 200
    binding = {"conversation_id": "same-parent", "quota_group_id": target["id"],
        "quota_binding": {"origin": target["origin"], "initial_group_id": target["id"],
                          "credential_fingerprint": quotas.credential_hash("synthetic-quota-secret-A")}}
    with resources.request_admission(provider="test", endpoint=one["base_url"], credential_fingerprint=quotas.credential_hash("synthetic-quota-secret-A"), runtime_config=binding):
        with resources.request_admission(provider="test", endpoint=one["base_url"], credential_fingerprint=quotas.credential_hash("synthetic-separate-embedding"), runtime_config=binding, service="embedding"):
            assert len(resources.scheduler.snapshot()["items"]) == 2
        with resources.request_admission(provider="test", endpoint="https://independent.example.test/search", credential_fingerprint=quotas.credential_hash("synthetic-quota-secret-A"), runtime_config=binding, service="independent_search"):
            assert len(resources.scheduler.snapshot()["items"]) == 2


@pytest.mark.parametrize("count", [2, 4, 8, 16])
def test_mixed_foreground_background_children_share_explicit_account_without_starvation(count):
    first = profile(); second = profile("Other credential", "synthetic-quota-secret-B")
    target = group(limit=2); assert bind(first, target).status_code == bind(second, target).status_code == 200
    barrier = threading.Barrier(count); mutex = threading.Lock(); maximum = 0; active = 0; done = []
    def work(index):
        nonlocal maximum, active
        barrier.wait(timeout=5)
        fp = quotas.credential_hash("synthetic-quota-secret-A" if index % 2 else "synthetic-quota-secret-B")
        binding = {"conversation_id": f"session-{index}", "run_id": f"{'background' if index % 2 else 'foreground'}-{index}", "parent_run_id": f"synthetic-parent-{index}"}
        if index % 3 == 0:
            binding["subtask_id"] = f"synthetic-child-{index}"
        with resources.request_admission(provider="test", endpoint=first["base_url"], credential_fingerprint=fp,
                runtime_config=binding, service="embedding" if index % 3 == 0 else "chat"):
            with mutex:
                active += 1; maximum = max(maximum, active); done.append(index)
            time.sleep(0.01)
            with mutex:
                active -= 1
    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(work, range(count)))
    assert sorted(done) == list(range(count)) and maximum <= 2
    assert not resources.scheduler.snapshot()["items"]


def test_shrinking_limit_preserves_active_requests_and_older_revision_cannot_restore_old_limit():
    target = group(limit=3)
    held = [resources.scheduler.acquire(key(target), session=f"s{i}") for i in range(3)]
    assert update(target, 1).status_code == 200
    assert len(resources.scheduler.groups[key(target)].active) == 3
    current = state()["revision"]
    assert resources.scheduler.configure(key(target), 8, policy_revision=current-1) is False
    assert resources.scheduler.groups[key(target)].limit == 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(resources.scheduler.acquire, key(target), session="later")
        eventually(lambda: len(resources.scheduler.snapshot()["items"]) == 4)
        for ticket in held[:2]:
            resources.scheduler.release(key(target), ticket)
        assert not waiting.done()
        resources.scheduler.release(key(target), held[2])
        ticket = waiting.result(timeout=4); resources.scheduler.release(key(target), ticket)


def test_retired_group_is_not_deleted_or_bypassed_by_old_or_new_requests():
    one = profile(); target = group(limit=1); assert bind(one, target).status_code == 200
    assert update(target, 1, status="retired").status_code == 200
    with resources.request_admission(provider="test", endpoint=one["base_url"], credential_fingerprint=quotas.credential_hash("synthetic-quota-secret-A")):
        assert len(resources.scheduler.groups[key(target)].active) == 1
    assert state()["groups"][0]["state"] == "retired"


def test_account_group_policy_survives_fresh_process_without_plaintext_credentials():
    one = profile(); target = group(limit=4); assert bind(one, target).status_code == 200
    code = "from app.db import init_db,SessionLocal;init_db();from app.account_quotas import resolve_request;from app.request_resources import request_admission,scheduler;import json;d=SessionLocal();r=resolve_request(d,'https://quota.example.test/v1','" + quotas.credential_hash("synthetic-quota-secret-A") + "');d.close()\nwith request_admission(provider='test',endpoint='https://quota.example.test/v1',credential_fingerprint='" + quotas.credential_hash("synthetic-quota-secret-A") + "'):\n r['effective_limit']=next(iter(scheduler.groups.values())).limit\n print(json.dumps(r))"
    process = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, timeout=30, env=os.environ.copy())
    assert process.returncode == 0, process.stderr
    resolved = json.loads(process.stdout)
    assert resolved["group_id"] == target["id"] and resolved["policy"]["groups"][0]["concurrency"] == 4
    assert resolved["effective_limit"] == 4
    assert "synthetic-quota-secret-A" not in process.stdout


def test_account_policy_and_hash_members_survive_credential_excluded_backup_restore(db, tmp_path):
    from contextlib import closing
    import sqlite3
    from app import data_backup
    from app.config import DATA_DIR
    one = profile(); target = group(limit=8); assert bind(one, target).status_code == 200
    exported = data_backup.create_backup(DATA_DIR)
    restored = tmp_path / "restored"
    preview = data_backup.prepare_restore(restored, exported["path"])
    data_backup.schedule_restore(restored, preview["id"])
    assert data_backup.apply_pending_restore(restored)["status"] == "restored"
    with closing(sqlite3.connect(restored / "scholarflow.db")) as database:
        assert database.execute("SELECT concurrency FROM account_quota_groups WHERE id=?", (target["id"],)).fetchone() == (8,)
        assert database.execute("SELECT credential_fingerprint FROM account_quota_members").fetchone() == (quotas.credential_hash("synthetic-quota-secret-A"),)
        assert database.execute("SELECT COUNT(*) FROM model_credentials").fetchone() == (0,)
    assert b"synthetic-quota-secret-A" not in (restored / "scholarflow.db").read_bytes()


def test_configured_group_cancellation_creates_no_dispatch_and_429_cooldown_is_shared(monkeypatch):
    import httpx
    one = profile(); target = group(limit=1); assert bind(one, target).status_code == 200
    fp = quotas.credential_hash("synthetic-quota-secret-A")
    with pytest.raises(resources.ResourceRequestCancelled):
        with resources.request_admission(provider="test", endpoint=one["base_url"], credential_fingerprint=fp,
                cancelled=lambda: True):
            pytest.fail("cancelled request entered")
    monkeypatch.setattr(resources.random, "uniform", lambda *a: 0)
    response = httpx.Response(429, headers={"Retry-After": "0.05"}, request=httpx.Request("GET", one["base_url"]))
    with pytest.raises(httpx.HTTPStatusError):
        with resources.request_admission(provider="test", endpoint=one["base_url"], credential_fingerprint=fp):
            raise httpx.HTTPStatusError("synthetic throttle", response=response, request=response.request)
    began = time.monotonic()
    with resources.request_admission(provider="test", endpoint=one["base_url"], credential_fingerprint=fp):
        assert time.monotonic() - began >= 0.045
    assert not resources.scheduler.snapshot()["items"]


def test_real_thread_late_resolution_reloads_after_migration_before_any_dispatch(monkeypatch):
    one = profile(); first = group("First"); second = group("Second")
    assert bind(one, first).status_code == 200
    read_old, release = threading.Event(), threading.Event()
    original = quotas.resolve_request
    observed = []
    def controlled(db, endpoint, fp):
        resolved = original(db, endpoint, fp)
        observed.append(resolved["group_id"])
        if len(observed) == 1:
            read_old.set(); assert release.wait(timeout=5)
        return resolved
    monkeypatch.setattr(quotas, "resolve_request", controlled)
    def late():
        with resources.request_admission(provider="test", endpoint=one["base_url"],
                credential_fingerprint=quotas.credential_hash("synthetic-quota-secret-A")):
            return list(resources.scheduler.groups)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(late)
        try:
            assert read_old.wait(timeout=5)
            assert bind(one, second).status_code == 200
        finally:
            release.set()
        used = worker.result(timeout=5)
    assert observed == [first["id"], second["id"]]
    assert key(second) in used and key(first) not in used
    assert not resources.scheduler.snapshot()["items"]


def test_queued_cancelled_configured_request_never_enters_and_leaves_no_ticket():
    one = profile(); target = group(limit=1); assert bind(one, target).status_code == 200
    held = resources.scheduler.acquire(key(target), session="held")
    cancel, entered = threading.Event(), threading.Event()
    def wait():
        with resources.request_admission(provider="test", endpoint=one["base_url"],
                credential_fingerprint=quotas.credential_hash("synthetic-quota-secret-A"),
                runtime_config={"conversation_id": "queued-only"}, cancelled=cancel.is_set):
            entered.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(wait)
        eventually(lambda: len(resources.scheduler.snapshot()["items"]) == 2)
        cancel.set()
        with pytest.raises(resources.ResourceRequestCancelled):
            worker.result(timeout=5)
    assert not entered.is_set()
    resources.scheduler.release(key(target), held)
    assert not resources.scheduler.snapshot()["items"]


def test_shrink_snapshot_cannot_reapply_previous_higher_limit(db):
    one = profile(); target = group(limit=8); assert bind(one, target).status_code == 200
    old = quotas.resolve_request(db, one["base_url"], quotas.credential_hash("synthetic-quota-secret-A")); db.rollback()
    assert update(target, 1).status_code == 200
    with pytest.raises(resources.QuotaPolicyChanged):
        resources.scheduler.apply_policy(old["policy"])
    assert resources.scheduler.limits[key(target)] == 1


def test_explicit_migration_cannot_bypass_shared_429_cooldown():
    one = profile(); first = group("First"); second = group("Second")
    assert bind(one, first).status_code == 200
    held = resources.scheduler.acquire(key(first), session="throttled")
    resources.scheduler.release(key(first), held, cooldown_seconds=10)
    before = state()
    assert before["groups"][0]["cooldown_seconds"] >= 1
    rejected = bind(one, second)
    assert rejected.status_code == 409 and "限流冷却" in rejected.text
    assert state()["revision"] == before["revision"]
    with resources.scheduler.condition:
        resources.scheduler.groups[key(first)].cooldown_until = time.monotonic() - 1
    assert bind(one, second).status_code == 200
