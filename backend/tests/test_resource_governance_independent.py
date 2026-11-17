"""Root-authored probes independent of the account/timing author's fixtures."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from app import account_quotas as quotas, request_resources as resources, run_lifecycle, run_timing
from app.db import SessionLocal
from app.models import Conversation
from app.quota_models import QuotaMember
from app.runtime_models import RuntimeRun


def test_group_shrink_keeps_inflight_and_stale_resolution_cannot_restore_capacity(db, monkeypatch):
    scheduler = resources.FairRequestScheduler()
    monkeypatch.setattr(resources, "scheduler", scheduler)
    first = quotas.change(db, expected_revision=0, operation="create",
        fields={"name": "Independent account", "origin": "https://independent.example.test", "concurrency": 2})
    group = first["groups"][0]
    for key in ("synthetic-one", "synthetic-two"):
        db.add(QuotaMember(origin=group["origin"], credential_fingerprint=quotas.credential_hash(key), group_id=group["id"]))
    db.commit()
    old = quotas.resolve_request(db, group["origin"] + "/v1", quotas.credential_hash("synthetic-one")); db.rollback()
    token = scheduler.apply_policy(old["policy"])
    quota_key = resources.quota_key(provider="independent", endpoint=group["origin"], quota_group_id=group["id"])
    one, two = [scheduler.acquire(quota_key, session="held-" + key, policy_token=token) for key in ("1", "2")]
    smaller = quotas.change(db, expected_revision=first["revision"], operation="update", group_id=group["id"],
        fields={"name": group["name"], "state": "active", "concurrency": 1})
    assert len(scheduler.groups[quota_key].active) == 2
    with pytest.raises(resources.QuotaPolicyChanged): scheduler.apply_policy(old["policy"])
    db.rollback()
    current = quotas.resolve_request(db, group["origin"], quotas.credential_hash("synthetic-two")); db.rollback()
    token = scheduler.apply_policy(current["policy"])
    entered = threading.Event()
    def pending():
        held = scheduler.acquire(quota_key, session="new-session", policy_token=token)
        entered.set(); scheduler.release(quota_key, held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pending)
        end = time.monotonic() + 4
        while len(scheduler.snapshot()["items"]) < 3:
            assert time.monotonic() < end; time.sleep(.005)
        assert not entered.is_set()
        scheduler.release(quota_key, one); assert not entered.wait(.03)
        scheduler.release(quota_key, two); future.result(timeout=4)
    assert entered.is_set() and smaller["revision"] == first["revision"] + 1
    assert not scheduler.snapshot()["items"]


def test_confirmed_time_excludes_crash_tail_and_clock_jump_never_becomes_offline(db, monkeypatch):
    clock = [datetime(2026, 11, 16), 100.]
    monkeypatch.setattr(run_timing, "time", SimpleNamespace(monotonic=lambda: clock[1]))
    monkeypatch.setattr(run_timing, "utcnow", lambda: clock[0])
    monkeypatch.setattr(run_lifecycle, "utcnow", lambda: clock[0])
    db.add(Conversation(id="independent-time")); db.commit()
    attempt, _ = run_lifecycle.admit_attempt(db, "independent-timing", "independent-time", "Check actual durations")
    db.commit()
    def advance(seconds): clock[0] += timedelta(seconds=seconds); clock[1] += seconds
    advance(2)
    run_lifecycle.save_checkpoint(db, attempt.run_id, {"attempt_id": attempt.id, "writer_token": attempt.writer_token, "phase": "model"}); db.commit()
    identity = run_timing.begin_request({"attempt_id": attempt.id}, attempt.run_id, "independent")
    advance(3); run_timing.request_boundary(identity, "acquired")
    advance(4); run_timing.request_boundary(identity, "closed"); run_timing.tick()
    advance(9)
    with SessionLocal() as reader:
        result = run_timing.snapshot(reader, reader.get(RuntimeRun, attempt.run_id))
        assert result["request_execution_sum_seconds"] == 4
        assert result["confirmed_task_wall_seconds"] == 9 and result["offline_or_unknown_seconds"] == 9
    clock[0] += timedelta(hours=2)
    with SessionLocal() as reader:
        result = run_timing.snapshot(reader, reader.get(RuntimeRun, attempt.run_id))
        assert result["request_execution_sum_seconds"] == 4
        assert not result["wall_time_basis_reliable"]
        assert result["offline_or_unknown_seconds"] is result["confirmed_task_wall_seconds"] is None
