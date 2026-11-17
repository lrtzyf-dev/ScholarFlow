"""Exclusive startup recovery, with abrupt process exits and no provider calls."""
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import select

from app import call_ledger as ledger, resource_budget as budget, runtime_owner
from app.config import DATA_DIR
from app.db import SessionLocal
from app.ledger_models import CallRecord
from app.pricing_models import BudgetChargeEvent
from app.resource_models import ResourceBudget, ResourceReservation, ResourceStartupRecovery
from app.runtime_models import RuntimeRun, RunAttempt


@pytest.fixture
def startup_owner(monkeypatch):
    instance = runtime_owner.RuntimeOwner(DATA_DIR)
    instance.begin_service()
    monkeypatch.setattr(runtime_owner, "owner", instance)
    try:
        yield instance
    finally:
        instance.finish_shutdown(idle=True)


def reservation(*, run="orphan-run", limit="5", estimate="0.4"):
    identity = budget.create_budget(owner_run_id=run, amount_limit=limit,
                                   request_estimate=estimate, price_version="test-estimate")
    return identity, budget.reserve(identity, run_id=run)


def call(run="orphan-run", *, state="started"):
    identity = ledger.begin_call(provider="synthetic.test", endpoint="https://synthetic.test/chat",
                                 service="chat", run_id=run, synthetic=True)
    if state != "started":
        ledger.finish_call(identity, state=state, usage={"prompt_tokens": 5})
    return identity


def row(identity):
    with SessionLocal() as db:
        item = db.get(ResourceReservation, identity)
        receipt = db.get(ResourceStartupRecovery, identity)
        return item.state, item.settled_micros, receipt.action if receipt else None, receipt.reasons if receipt else None


def test_unbound_reservation_releases_admission_hold_without_claiming_zero_charge(startup_owner):
    bid, rid = reservation()
    before = budget.budget_snapshot(bid)
    assert budget.recover_startup_reservations() == {
        "released": 1, "retained_for_reconciliation": 0, "calls_marked_unknown": 0}
    result = budget.budget_snapshot(bid)
    assert result["reserved_amount"] == "0" and result["remaining_amount"] == "5"
    assert result["spent_amount"] == "0" and result["active_seconds"] == 0
    assert result["revision"] == before["revision"] + 1
    assert row(rid) == ("released", None, "released_unbound", ["unbound_before_dispatch"])
    receipt = result["reservations"][0]["startup_recovery"]
    assert receipt["released_amount"] == "0.4" and receipt["supplier_charge_known"] is False


def test_call_created_before_binding_becomes_unknown_even_when_hold_released(startup_owner):
    bid, rid = reservation()
    cid = call()
    result = budget.recover_startup_reservations()
    assert result == {"released": 1, "retained_for_reconciliation": 0, "calls_marked_unknown": 1}
    saved = ledger.get_call(cid)
    assert saved["state"] == "unknown" and saved["usage_source"] == "unknown"
    assert saved["amount"] is None and saved["finished_at"] is None and saved["duration_seconds"] is None
    assert row(rid)[0] == "released" and budget.budget_snapshot(bid)["reserved_amount"] == "0"


@pytest.mark.parametrize("state", ["started", "completed", "failed", "unknown", "cancelled", "interrupted"])
def test_bound_attempt_retains_hold_and_existing_usage_or_terminal_facts(startup_owner, state):
    bid, rid = reservation()
    cid = call(state=state)
    budget.bind_call(rid, cid)
    before = ledger.get_call(cid)
    result = budget.recover_startup_reservations()
    assert result["released"] == 0 and result["retained_for_reconciliation"] == 1
    assert row(rid)[0] == "pending_reconciliation"
    assert budget.budget_snapshot(bid)["reserved_amount"] == "0.4"
    assert budget.budget_snapshot(bid)["pending_reconciliation"] == 1
    saved = ledger.get_call(cid)
    assert saved["state"] == ("unknown" if state == "started" else state)
    assert saved["actual_usage"] == before["actual_usage"] and saved["amount"] == before["amount"]
    assert saved["finished_at"] == before["finished_at"]


def test_bound_missing_call_retains_and_records_inconsistency(startup_owner):
    bid, rid = reservation()
    budget.bind_call(rid, "missing-call")
    budget.recover_startup_reservations()
    assert "missing_bound_call" in row(rid)[3]
    assert budget.budget_snapshot(bid)["reserved_amount"] == "0.4"


@pytest.mark.parametrize("state", ["in_flight", "pending_reconciliation", "unexpected"])
def test_unbound_contradictory_state_never_assumed_undispatched(startup_owner, state):
    bid, rid = reservation()
    with SessionLocal.begin() as db:
        db.get(ResourceReservation, rid).state = state
    budget.recover_startup_reservations()
    assert row(rid)[0] == "pending_reconciliation" and "unbound_non_reserved_state" in row(rid)[3]
    assert budget.budget_snapshot(bid)["reserved_amount"] == "0.4"


@pytest.mark.parametrize("amount", [-1, 0, 1_200_000])
def test_inconsistent_budget_counter_preserved_without_subtraction_or_repair(startup_owner, amount):
    bid, rid = reservation()
    with SessionLocal.begin() as db:
        db.get(ResourceBudget, bid).reserved_micros = amount
    budget.recover_startup_reservations()
    assert row(rid)[0] == "pending_reconciliation" and "budget_counter_mismatch" in row(rid)[3]
    with SessionLocal() as db:
        assert db.get(ResourceBudget, bid).reserved_micros == amount
    with pytest.raises(budget.ResourceBudgetExceeded, match="矛盾记录"):
        budget.reserve(bid, run_id="orphan-run")


@pytest.mark.parametrize("contradiction", ["missing_budget", "settled_amount", "charge_event", "call_run_mismatch"])
def test_contradictory_ownership_or_charge_facts_fail_closed(startup_owner, contradiction):
    bid, rid = reservation()
    with SessionLocal.begin() as db:
        saved = db.get(ResourceReservation, rid)
        if contradiction == "missing_budget":
            db.delete(db.get(ResourceBudget, bid))
        elif contradiction == "settled_amount":
            saved.settled_micros = 0
        elif contradiction == "charge_event":
            db.add(BudgetChargeEvent(id="previous-charge", reservation_id=rid, revision=1,
                                    amount_micros=0, currency="USD", source="user_confirmed"))
    if contradiction == "call_run_mismatch":
        budget.bind_call(rid, call("different-run"))
    budget.recover_startup_reservations()
    assert row(rid)[0] == "pending_reconciliation"
    expected = "inconsistent_charge_facts" if contradiction in ("settled_amount", "charge_event") else contradiction
    assert expected in row(rid)[3]


def test_recovery_idempotent_across_repeated_calls_and_successive_owners(startup_owner, monkeypatch):
    bid, rid = reservation()
    other = budget.reserve(bid, run_id="orphan-run")
    cid = call()
    budget.bind_call(other, cid)
    first = budget.recover_startup_reservations()
    before, saved_call = budget.budget_snapshot(bid), ledger.get_call(cid)
    assert first == {"released": 1, "retained_for_reconciliation": 1, "calls_marked_unknown": 1}
    assert budget.recover_startup_reservations() == {
        "released": 0, "retained_for_reconciliation": 0, "calls_marked_unknown": 0}
    startup_owner.finish_shutdown(idle=True)
    successor = runtime_owner.RuntimeOwner(DATA_DIR)
    successor.begin_service()
    monkeypatch.setattr(runtime_owner, "owner", successor)
    try:
        budget.recover_startup_reservations()
    finally:
        successor.finish_shutdown(idle=True)
    assert budget.budget_snapshot(bid) == before and ledger.get_call(cid) == saved_call
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ResourceStartupRecovery)))) == 2


def test_recovery_is_atomic_when_call_reconciliation_fails(startup_owner, monkeypatch):
    bid, rid = reservation()
    before = budget.budget_snapshot(bid)
    def fail(db):
        raise RuntimeError("synthetic transaction fault")
    monkeypatch.setattr(ledger, "_recover_started_calls", fail)
    with pytest.raises(RuntimeError, match="transaction fault"):
        budget.recover_startup_reservations()
    assert budget.budget_snapshot(bid) == before and row(rid)[2] is None


def test_normal_bound_unknown_keeps_hold_and_can_be_reconciled_without_duplicate_cost(startup_owner):
    bid, rid = reservation()
    budget.bind_call(rid, call())
    budget.recover_startup_reservations()
    another = budget.reserve(bid, run_id="orphan-run")
    assert budget.budget_snapshot(bid)["reserved_amount"] == "0.8"
    budget.finish(rid, known_amount="0.25", reconcile=True)
    budget.finish(rid, known_amount="0.25", reconcile=True)
    snapshot = budget.budget_snapshot(bid)
    assert snapshot["spent_amount"] == "0.25" and snapshot["reserved_amount"] == "0.4"
    assert row(another)[0] == "reserved"


def test_binding_racing_recovery_cannot_revive_a_released_pre_dispatch_hold(startup_owner, monkeypatch):
    _, rid = reservation()
    attempting, outcomes = threading.Event(), []
    original = ledger._recover_started_calls
    def bind():
        attempting.set()
        try:
            budget.bind_call(rid, "new-call")
        except budget.ResourceBudgetError as exc:
            outcomes.append(str(exc))
        else:
            outcomes.append("unsafe binding succeeded")
    worker = threading.Thread(target=bind)
    def observe(db):
        worker.start()
        assert attempting.wait(3)
        return original(db)
    monkeypatch.setattr(ledger, "_recover_started_calls", observe)
    try:
        budget.recover_startup_reservations()
    finally:
        worker.join(timeout=5)
    assert len(outcomes) == 1 and "不可重复绑定" in outcomes[0]
    assert row(rid)[0] == "released"


def test_old_dates_and_run_status_do_not_authorize_runtime_recovery(startup_owner):
    bid, rid = reservation()
    with SessionLocal.begin() as db:
        db.get(ResourceReservation, rid).created_at = datetime(1999, 1, 1)
        db.add(RuntimeRun(id="orphan-run", kind="foreground", current_attempt_id="attempt", status="running"))
        db.add(RunAttempt(id="attempt", run_id="orphan-run", number=1, writer_token="still-current", status="running"))
    startup_owner.ready()
    with pytest.raises(runtime_owner.RuntimeInUse, match="启动阶段"):
        budget.recover_startup_reservations()
    assert row(rid)[0] == "reserved" and budget.budget_snapshot(bid)["reserved_amount"] == "0.4"


@pytest.mark.parametrize("stage", ["unowned", "desktop_reserved", "ready", "stopping", "shutdown"])
def test_recovery_rejects_every_nonstartup_ownership_phase(monkeypatch, stage):
    bid, rid = reservation()
    instance = runtime_owner.RuntimeOwner(DATA_DIR)
    monkeypatch.setattr(runtime_owner, "owner", instance)
    try:
        if stage == "desktop_reserved":
            assert instance.reserve_desktop()
        elif stage not in ("unowned",):
            instance.begin_service()
            if stage == "ready": instance.ready()
            elif stage == "stopping": instance.stopping()
            elif stage == "shutdown": instance.finish_shutdown(idle=False)
        with pytest.raises(runtime_owner.RuntimeInUse, match="启动阶段"):
            budget.recover_startup_reservations()
        assert row(rid)[0] == "reserved" and budget.budget_snapshot(bid)["reserved_amount"] == "0.4"
    finally:
        instance.finish_shutdown(idle=True)


def test_owner_of_another_directory_cannot_recover_this_database(monkeypatch, tmp_path):
    bid, rid = reservation()
    instance = runtime_owner.RuntimeOwner(tmp_path / "different-data")
    instance.begin_service()
    monkeypatch.setattr(runtime_owner, "owner", instance)
    try:
        with pytest.raises(budget.ResourceBudgetError, match="目录不一致"):
            budget.recover_startup_reservations()
        assert row(rid)[0] == "reserved"
    finally:
        instance.finish_shutdown(idle=True)


def test_contender_cannot_take_lock_or_trigger_recovery(startup_owner):
    bid, rid = reservation()
    contender = runtime_owner.RuntimeOwner(DATA_DIR)
    with pytest.raises(runtime_owner.RuntimeInUse):
        contender.begin_service()
    with pytest.raises(runtime_owner.RuntimeInUse):
        with contender.startup_recovery_guard():
            pytest.fail("contender obtained recovery ownership")
    assert row(rid)[0] == "reserved"


def test_startup_guard_prevents_ready_publication_until_recovery_commits(startup_owner, monkeypatch):
    bid, rid = reservation()
    entered, attempted_ready, published = threading.Event(), threading.Event(), threading.Event()
    original = ledger._recover_started_calls
    def observe(db):
        entered.set()
        assert attempted_ready.wait(3)
        assert not published.wait(0.05)
        return original(db)
    monkeypatch.setattr(ledger, "_recover_started_calls", observe)
    def ready():
        assert entered.wait(3)
        attempted_ready.set()
        startup_owner.ready()
        published.set()
    worker = threading.Thread(target=ready)
    worker.start()
    try:
        budget.recover_startup_reservations()
    finally:
        worker.join(timeout=3)
    assert published.is_set() and row(rid)[0] == "released"


def test_startup_hook_recovers_before_ready_without_replaying_provider_calls():
    from fastapi.testclient import TestClient
    from app.main import app
    bid, rid = reservation()
    cid = call()
    budget.bind_call(rid, cid)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert row(rid)[0] == "pending_reconciliation"
        assert ledger.get_call(cid)["state"] == "unknown"
        with pytest.raises(runtime_owner.RuntimeInUse):
            budget.recover_startup_reservations()
    assert budget.budget_snapshot(bid)["reserved_amount"] == "0.4"


def test_recovery_receipts_follow_conversation_cleanup_without_touching_other_budget(startup_owner):
    from app import data_cleanup
    from app.models import Conversation
    with SessionLocal.begin() as db:
        db.add_all([Conversation(id="remove-budget"), Conversation(id="keep-budget")])
        db.add_all([RuntimeRun(id="remove-run", kind="foreground", conversation_id="remove-budget"),
                    RuntimeRun(id="keep-run", kind="foreground", conversation_id="keep-budget")])
    _, removed = reservation(run="remove-run")
    kept_budget, kept = reservation(run="keep-run")
    budget.recover_startup_reservations()
    with SessionLocal.begin() as db:
        assert data_cleanup.preview(db, "remove-budget")["records"]["resource_startup_recoveries"] == 1
        data_cleanup.remove(db, "remove-budget")
    with SessionLocal() as db:
        assert db.get(ResourceStartupRecovery, removed) is None
        assert db.get(ResourceReservation, removed) is None
        assert db.get(ResourceStartupRecovery, kept) is not None
        assert db.get(ResourceBudget, kept_budget) is not None


@pytest.mark.parametrize("phase", ["after_reserve", "after_call", "after_bind", "after_dispatch", "before_settlement"])
def test_real_process_crash_points_preserve_or_release_only_proven_local_holds(tmp_path, phase):
    probe = Path(__file__).with_name("resource_orphan_probe.py")
    environment = dict(os.environ, DATA_DIR=str(tmp_path / "data"), LLM_API_KEY="", EMBEDDING_API_KEY="",
                       REDIS_URL="", LLM_DEMO_MODE="1", DESKTOP_NOTIFICATIONS="false")
    def run(mode):
        return subprocess.run([sys.executable, "-X", "utf8", str(probe), mode], env=environment,
                              capture_output=True, text=True, encoding="utf-8", timeout=35)
    crashed = run(phase)
    assert crashed.returncode == 91, crashed.stdout + crashed.stderr
    recovered = run("recover")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    result = json.loads(recovered.stdout.strip().splitlines()[-1])
    unbound = phase in ("after_reserve", "after_call")
    assert result["reservation_state"] == ("released" if unbound else "pending_reconciliation")
    assert Decimal(result["reserved_amount"]) == (Decimal(0) if unbound else Decimal("0.4"))
    assert result["spent_amount"] == "0" and result["active_seconds"] == 0
    assert result["dispatches"] == (1 if phase in ("after_dispatch", "before_settlement") else 0)
    assert result["call_state"] == (None if phase == "after_reserve" else "completed" if phase == "before_settlement" else "unknown")
    assert result["call_amount"] is None
    assert result["second_recovery"] == {"released": 0, "retained_for_reconciliation": 0, "calls_marked_unknown": 0}


def test_live_process_lock_rejection_preserves_hold_until_owner_actually_exits(tmp_path):
    probe = Path(__file__).with_name("resource_orphan_probe.py")
    data = tmp_path / "data"
    environment = dict(os.environ, DATA_DIR=str(data), LLM_API_KEY="", EMBEDDING_API_KEY="",
                       REDIS_URL="", LLM_DEMO_MODE="1", DESKTOP_NOTIFICATIONS="false")
    log_path = tmp_path / "owner.log"
    with log_path.open("w", encoding="utf-8") as output:
        holder = subprocess.Popen([sys.executable, "-X", "utf8", str(probe), "hold"], env=environment,
                                  stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while not (data / "holder-started").exists():
                assert holder.poll() is None, log_path.read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "lock holder did not start"
                time.sleep(0.03)
            rejected = subprocess.run([sys.executable, "-X", "utf8", str(probe), "recover"], env=environment,
                                      capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert rejected.returncode != 0 and "RuntimeInUse" in rejected.stderr
            import sqlite3
            with sqlite3.connect(data / "scholarflow.db") as connection:
                assert connection.execute("SELECT state FROM resource_reservations").fetchone() == ("reserved",)
                assert connection.execute("SELECT COUNT(*) FROM resource_startup_recoveries").fetchone() == (0,)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
    recovered = subprocess.run([sys.executable, "-X", "utf8", str(probe), "recover"], env=environment,
                               capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert json.loads(recovered.stdout.strip().splitlines()[-1])["reservation_state"] == "released"

