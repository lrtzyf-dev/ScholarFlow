"""Isolated duration goldens, actual queue cancellation and process recovery."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app import run_timing as timing, run_lifecycle as lifecycle, request_resources as resources
from app.db import SessionLocal
from app.models import Conversation
from app.runtime_models import RuntimeRun, RunAttempt
from app.timing_models import TimingSpan
from app.runtime_context import bind_runtime_config
from app.main import app


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1)
        self.mono = 100.
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=lambda: clock.mono))
    monkeypatch.setattr(timing, "utcnow", lambda: clock.now)
    monkeypatch.setattr(lifecycle, "utcnow", lambda: clock.now)
    return clock


def admitted(db, rid="timed", cid="timed-conversation"):
    if db.get(Conversation, cid) is None:
        db.add(Conversation(id=cid, title="计时验收")); db.commit()
    attempt, created = lifecycle.admit_attempt(db, rid, cid, "完成验收")
    db.commit()
    return attempt


def active(db, attempt):
    lifecycle.save_checkpoint(db, attempt.run_id,
        {"attempt_id": attempt.id, "writer_token": attempt.writer_token, "phase": "model"})
    db.commit()


def summary(db, rid="timed"):
    db.expire_all()
    return timing.snapshot(db, db.get(RuntimeRun, rid))


def test_monotonic_phase_goldens_separate_queue_active_approval_and_user_wait(db, clock):
    attempt = admitted(db)
    clock.advance(2); active(db, attempt)
    clock.advance(3)
    timing.phase(db, attempt.run_id, attempt.id, attempt.writer_token, "approval_wait"); db.commit()
    clock.advance(4)
    timing.phase(db, attempt.run_id, attempt.id, attempt.writer_token, "run_active"); db.commit()
    clock.advance(1)
    lifecycle.finish_attempt(db, attempt.run_id, attempt.id, attempt.writer_token, "paused"); db.commit()
    clock.advance(5); timing.tick()
    result = summary(db)
    assert result["queue_wall_seconds"] == 2
    assert result["active_wall_seconds"] == 4
    assert result["approval_wait_seconds"] == 4
    assert result["user_wait_seconds"] == 5
    assert result["confirmed_task_wall_seconds"] == result["elapsed_wall_seconds"] == 15
    assert result["offline_or_unknown_seconds"] == 0


def test_read_only_snapshot_never_invents_unconfirmed_running_tail(db, clock):
    attempt = admitted(db); active(db, attempt)
    clock.advance(6); timing.tick()
    clock.advance(9)
    first = summary(db)
    assert first["active_wall_seconds"] == 6 and first["offline_or_unknown_seconds"] == 9
    spans = [(r.id, r.elapsed_ms, r.confirmed_at) for r in db.scalars(select(TimingSpan))]
    second = summary(db)
    assert first == second
    assert spans == [(r.id, r.elapsed_ms, r.confirmed_at) for r in db.scalars(select(TimingSpan))]


def test_checkpoint_timing_shares_transaction_rollback(db, clock):
    attempt = admitted(db); clock.advance(4)
    lifecycle.save_checkpoint(db, attempt.run_id, {"attempt_id": attempt.id,
        "writer_token": attempt.writer_token, "phase": "paused"})
    db.rollback()
    assert db.get(RuntimeRun, attempt.run_id).status == "queued"
    rows = list(db.scalars(select(TimingSpan)))
    assert len(rows) == 1 and rows[0].state == "open" and rows[0].elapsed_ms == 0


def test_duplicate_checkpoint_and_finish_do_not_double_count(db, clock):
    attempt = admitted(db); active(db, attempt); clock.advance(2)
    active(db, attempt); active(db, attempt)
    lifecycle.finish_attempt(db, attempt.run_id, attempt.id, attempt.writer_token, "done"); db.commit()
    clock.advance(10)
    lifecycle.finish_attempt(db, attempt.run_id, attempt.id, attempt.writer_token, "done"); db.commit()
    assert summary(db)["active_wall_seconds"] == 2
    assert summary(db)["elapsed_wall_seconds"] == 2
    assert db.scalar(select(func.count()).select_from(TimingSpan)) == 2


def replacement(db, old):
    new = RunAttempt(id=uuid4().hex, run_id=old.run_id, number=old.number + 1,
        writer_token=uuid4().hex, status="running")
    db.add(new); db.flush()
    db.get(RuntimeRun, old.run_id).current_attempt_id = new.id; db.flush()
    timing.phase(db, old.run_id, new.id, new.writer_token, "run_active"); db.commit()
    return new


def test_stale_writer_cannot_close_or_confirm_replacement_phase(db, clock):
    old = admitted(db); active(db, old); clock.advance(2); timing.tick()
    clock.advance(20); new = replacement(db, old)
    before = summary(db)
    with pytest.raises(lifecycle.StaleAttempt):
        timing.phase(db, old.run_id, old.id, old.writer_token, None)
    db.rollback()
    assert summary(db) == before
    assert db.scalar(select(TimingSpan).where(TimingSpan.attempt_id == new.id)).state == "open"
    assert sum(r.elapsed_ms for r in db.scalars(select(TimingSpan).where(TimingSpan.attempt_id == old.id))) == 2000


@pytest.mark.parametrize("token", ["", "wrong-writer-token"])
def test_current_attempt_id_does_not_authorize_wrong_writer_token(db, clock, token):
    attempt = admitted(db); active(db, attempt)
    with pytest.raises(lifecycle.StaleAttempt):
        timing.phase(db, attempt.run_id, attempt.id, token, None)
    db.rollback()
    assert summary(db)["open_segments"] == 1


def test_stale_request_and_wrong_process_identity_are_fenced(db, clock):
    old = admitted(db); active(db, old)
    identity = timing.begin_request({"attempt_id": old.id}, old.run_id, "chat")
    new = replacement(db, old)
    for identity_ in [identity, {**identity, "attempt_id": new.id, "writer_token": new.writer_token, "process_id": "old-process"}]:
        with pytest.raises(lifecycle.StaleAttempt):
            timing.request_boundary(identity_, "closed")
    with pytest.raises(lifecycle.StaleAttempt):
        timing.begin_request({"attempt_id": old.id}, old.run_id, "chat")


def test_unmanaged_probe_never_borrows_current_attempt(db, clock):
    admitted(db)
    assert timing.begin_request({}, "timed", "probe") is None
    assert timing.begin_request({"attempt_id": "missing"}, "unknown", "probe") is None
    with pytest.raises(lifecycle.StaleAttempt):
        timing.begin_request({"attempt_id": "missing"}, "timed", "probe")


def test_cancelled_queue_and_terminal_attempt_cannot_open_execution_later(db, clock):
    attempt = admitted(db); active(db, attempt)
    identity = timing.begin_request({"attempt_id": attempt.id}, attempt.run_id, "chat")
    timing.request_boundary(identity, "cancelled")
    with pytest.raises(ValueError, match="已取消"):
        timing.request_boundary(identity, "acquired")
    lifecycle.finish_attempt(db, attempt.run_id, attempt.id, attempt.writer_token, "done"); db.commit()
    with pytest.raises(lifecycle.StaleAttempt):
        timing.request_boundary(identity, "acquired")
    assert summary(db)["request_windows"] == 0


def test_execution_window_pair_and_bound_call_deduplicate(db, clock):
    attempt = admitted(db); active(db, attempt)
    identity = timing.begin_request({"attempt_id": attempt.id}, attempt.run_id, "chat")
    clock.advance(3); timing.request_boundary(identity, "acquired")
    timing.request_boundary(identity, "acquired")
    timing.request_boundary(identity, "bind", "one-call")
    clock.advance(4); timing.request_boundary(identity, "closed")
    clock.advance(10); timing.request_boundary(identity, "closed")
    result = summary(db)
    assert result["request_execution_sum_seconds"] == 4 and result["queue_wall_seconds"] == 3
    assert result["request_windows"] == 1 and result["queue_only_requests"] == 0
    assert len(list(db.scalars(select(TimingSpan).where(TimingSpan.dispatch_id == identity["dispatch_id"])))) == 2


def test_parallel_child_request_sum_is_distinct_from_wall_union(db, clock):
    attempt = admitted(db); active(db, attempt)
    one = timing.begin_request({"attempt_id": attempt.id}, attempt.run_id, "chat")
    timing.request_boundary(one, "acquired"); timing.request_boundary(one, "bind", "one")
    clock.advance(1)
    two = timing.begin_request({"attempt_id": attempt.id, "parent_run_id": attempt.run_id}, attempt.run_id, "child")
    timing.request_boundary(two, "acquired"); timing.request_boundary(two, "bind", "two")
    clock.advance(3); timing.request_boundary(one, "closed")
    clock.advance(1); timing.request_boundary(two, "closed"); timing.tick()
    result = summary(db)
    assert result["request_execution_sum_seconds"] == 8
    assert result["request_execution_wall_seconds"] == result["confirmed_task_wall_seconds"] == 5
    assert result["request_sum_additive_to_wall"] is False


def test_duplicate_call_id_facts_are_not_added_twice(db, clock):
    attempt = admitted(db); active(db, attempt)
    for duration in (2, 3):
        identity = timing.begin_request({"attempt_id": attempt.id}, attempt.run_id, "chat")
        timing.request_boundary(identity, "acquired"); timing.request_boundary(identity, "bind", "same-call")
        clock.advance(duration); timing.request_boundary(identity, "closed")
    assert summary(db)["request_execution_sum_seconds"] == 3
    assert summary(db)["request_windows"] == 2
    assert summary(db)["deduplicated_request_windows"] == 1
    assert summary(db)["duplicate_call_windows"] == 1


@pytest.mark.parametrize("jump", [-3600, 3600])
def test_clock_jump_preserves_monotonic_request_but_does_not_claim_offline(db, clock, jump):
    attempt = admitted(db); active(db, attempt)
    identity = timing.begin_request({"attempt_id": attempt.id}, attempt.run_id, "chat")
    timing.request_boundary(identity, "acquired")
    clock.advance(4); clock.now += timedelta(seconds=jump)
    timing.request_boundary(identity, "closed")
    result = summary(db)
    assert result["request_execution_sum_seconds"] == 4 and result["clock_adjustment_detected"]
    assert result["elapsed_wall_seconds"] is None and result["offline_or_unknown_seconds"] is None
    assert result["wall_time_basis_reliable"] is False


def test_read_only_snapshot_detects_clock_jump_before_next_heartbeat(db, clock):
    attempt = admitted(db); active(db, attempt)
    clock.advance(4); clock.now += timedelta(hours=1)
    result = summary(db)
    assert result["offline_or_unknown_seconds"] is None
    assert result["clock_adjustment_detected"] is True


def test_approval_wait_wraps_actual_wait_and_restores_active_phase(db, clock, monkeypatch):
    from app import approval_store
    attempt = admitted(db); active(db, attempt)
    approval = approval_store.create("write", {}, "guarded", run_id=attempt.run_id)
    def waiting(p, timeout):
        clock.advance(7)
        return p
    monkeypatch.setattr(approval_store, "_wait", waiting)
    try:
        with bind_runtime_config({"run_id": attempt.run_id, "attempt_id": attempt.id}):
            assert approval_store.wait(approval.id) is approval
        assert summary(db)["approval_wait_seconds"] == 7
        assert db.scalar(select(TimingSpan).where(TimingSpan.kind == "run_active", TimingSpan.state == "open"))
    finally:
        approval_store.discard(approval.id)


def test_old_approval_finally_never_reopens_new_attempt(db, clock):
    old = admitted(db); active(db, old)
    with bind_runtime_config({"run_id": old.run_id, "attempt_id": old.id}):
        with timing.approval_wait(old.run_id):
            clock.advance(1); replacement(db, old)
    assert sum(r.state == "open" for r in db.scalars(select(TimingSpan))) == 1


def test_actual_queued_cancellation_has_no_execution_window_or_call(db, monkeypatch):
    attempt = admitted(db); active(db, attempt)
    monkeypatch.setattr(resources, "scheduler", resources.FairRequestScheduler(default_limit=1))
    group = resources.quota_key(provider="test", endpoint="https://timing.test", credential_fingerprint="test")
    blocker = resources.scheduler.acquire(group, session="blocker")
    cancel = threading.Event()
    def work():
        with pytest.raises(resources.ResourceRequestCancelled):
            with resources.request_admission(provider="test", endpoint="https://timing.test", credential_fingerprint="test",
                    runtime_config={"run_id": attempt.run_id, "attempt_id": attempt.id}, cancelled=cancel.is_set):
                pytest.fail("Cancelled request entered its window")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(work)
            deadline = time.monotonic() + 5
            while len(resources.scheduler.snapshot()["items"]) < 2:
                assert time.monotonic() < deadline; time.sleep(.01)
            cancel.set(); future.result(timeout=5)
    finally:
        resources.scheduler.release(group, blocker)
    result = summary(db)
    assert result["queue_only_requests"] == 1 and result["request_windows"] == 0
    assert not resources.scheduler.snapshot()["items"]
    assert all(r.call_id is None for r in db.scalars(select(TimingSpan)))


def test_request_failure_closes_duration_and_releases_slot(db, monkeypatch):
    attempt = admitted(db); active(db, attempt)
    monkeypatch.setattr(resources, "scheduler", resources.FairRequestScheduler())
    with pytest.raises(ValueError, match="synthetic failure"):
        with resources.request_admission(provider="test", endpoint="https://timing.test",
                runtime_config={"run_id": attempt.run_id, "attempt_id": attempt.id}) as admission:
            admission.bind_call("synthetic-timing-call")
            raise ValueError("synthetic failure")
    result = summary(db)
    assert result["request_windows"] == 1 and not resources.scheduler.snapshot()["items"]
    assert db.scalar(select(TimingSpan).where(TimingSpan.kind == "request_execution_window")).state == "error"


def test_timer_confirm_covers_quiet_background_and_paused_user_wait(db, clock):
    attempt = admitted(db); active(db, attempt)
    identity = timing.begin_request({"attempt_id": attempt.id}, attempt.run_id, "background")
    timing.request_boundary(identity, "acquired")
    clock.advance(12); timing.tick()
    assert summary(db)["request_execution_sum_seconds"] == 12
    timing.request_boundary(identity, "closed")
    lifecycle.finish_attempt(db, attempt.run_id, attempt.id, attempt.writer_token, "paused"); db.commit()
    clock.advance(30); timing.tick()
    assert summary(db)["user_wait_seconds"] == 30


def test_startup_recovery_needs_exclusive_owner_and_never_extends_old_span(db, clock, tmp_path, monkeypatch):
    from app import runtime_owner
    attempt = admitted(db); active(db, attempt)
    clock.advance(5); timing.tick()
    clock.advance(300)
    owner = runtime_owner.RuntimeOwner(tmp_path)
    monkeypatch.setattr(runtime_owner, "owner", owner)
    with pytest.raises(runtime_owner.RuntimeInUse):
        timing.recover_startup()
    owner.begin_service()
    try:
        timing.recover_startup()
        result = summary(db)
        assert result["active_wall_seconds"] == 5 and result["offline_or_unknown_seconds"] == 300
        assert result["open_segments"] == 0
    finally:
        owner.finish_shutdown(idle=True)


def test_graceful_stop_confirms_until_stop_then_excludes_later_gap(db, clock):
    attempt = admitted(db); active(db, attempt)
    clock.advance(10); timing.stop()
    clock.advance(60); timing.tick()
    result = summary(db)
    assert result["active_wall_seconds"] == 10 and result["offline_or_unknown_seconds"] == 60


def test_legacy_unrecorded_task_remains_unavailable_instead_of_inferred(db):
    row = RuntimeRun(id="legacy-timing", kind="foreground", conversation_id="legacy", status="ended")
    db.add(row); db.commit()
    assert timing.snapshot(db, row)["available"] is False


def test_conversation_deletion_removes_all_timing_facts_without_affecting_other_runs(db, clock):
    from app import data_cleanup
    one = admitted(db); active(db, one)
    other = admitted(db, "other-run", "other-conversation"); active(db, other)
    lifecycle.finish_attempt(db, one.run_id, one.id, one.writer_token, "done"); db.commit()
    before = data_cleanup.preview(db, "timed-conversation")
    assert before["records"]["run_timing_spans"] == 2
    removed_run_id = one.run_id
    data_cleanup.remove(db, "timed-conversation"); db.commit()
    assert db.scalar(select(func.count()).select_from(TimingSpan).where(TimingSpan.run_id == removed_run_id)) == 0
    assert db.scalar(select(func.count()).select_from(TimingSpan).where(TimingSpan.run_id == other.run_id)) == 2


def test_conversation_timing_api_scopes_runs_and_hides_internal_tokens(db, clock):
    one = admitted(db); active(db, one)
    admitted(db, "other-run", "other-conversation")
    response = TestClient(app).get("/api/conversation-resources/timed-conversation")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [r["run_id"] for r in payload["timings"]] == ["timed"]
    assert one.writer_token not in response.text
    assert "started_monotonic" not in response.text and timing.PROCESS_ID not in response.text


def test_actual_crash_and_new_process_recovery_preserve_confirmed_only(db):
    # Two genuinely fresh interpreters use an isolated database. os._exit mimics
    # a hard termination, so no graceful cleanup can manufacture a close time.
    directory = str(__import__("pathlib").Path(os.environ["DATA_DIR"]) / "timing-crash-probe")
    common = "from app.db import init_db,SessionLocal;init_db();from app import run_timing as t;from app.runtime_models import RuntimeRun;from app.timing_models import TimingSpan;from sqlalchemy import select;import json,time,os\n"
    create = common + "from app.models import Conversation\nfrom app.run_lifecycle import admit_attempt,save_checkpoint\nd=SessionLocal();d.add(Conversation(id='c',title='probe'));d.commit();a,_=admit_attempt(d,'r','c','probe');d.commit();save_checkpoint(d,'r',{'attempt_id':a.id,'writer_token':a.writer_token,'phase':'model'});d.commit();time.sleep(.06);t.tick();s=t.snapshot(d,d.get(RuntimeRun,'r'));print(json.dumps(s),flush=True);os._exit(0)"
    env = {**os.environ, "DATA_DIR": directory, "LLM_API_KEY": "", "EMBEDDING_API_KEY": ""}
    first = subprocess.run([sys.executable, "-c", create], env=env, capture_output=True, text=True, check=True)
    before = json.loads(first.stdout.strip().splitlines()[-1])
    time.sleep(.08)
    recover = common + "from app.runtime_owner import owner\nowner.begin_service();t.recover_startup();d=SessionLocal();s=t.snapshot(d,d.get(RuntimeRun,'r'));print(json.dumps(s));d.close();owner.finish_shutdown(idle=True)"
    second = subprocess.run([sys.executable, "-c", recover], env=env, capture_output=True, text=True, check=True)
    after = json.loads(second.stdout.strip().splitlines()[-1])
    assert before["active_wall_seconds"] >= .05
    assert after["active_wall_seconds"] == before["active_wall_seconds"]
    assert after["offline_or_unknown_seconds"] > before["offline_or_unknown_seconds"] + .07
    assert after["open_segments"] == 0
