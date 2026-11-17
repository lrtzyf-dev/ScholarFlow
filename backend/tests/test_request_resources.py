from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import threading
import time

import httpx
import pytest

from app import cancel_store, resource_budget as budget
from app import request_resources as resources
from app.runtime_context import bind_runtime_config


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_account_quota_ignores_route_and_model_profile_but_not_credentials():
    key = lambda endpoint, credential: resources.quota_key(provider="test", endpoint=endpoint,
                                                          credential_fingerprint=credential)
    assert key("https://api.test/v1/chat", "same") == key("https://api.test/embeddings", "same")
    assert key("https://api.test/chat", "one") != key("https://api.test/chat", "two")
    assert key("http://api.test/chat", "same") != key("https://api.test/chat", "same")


@pytest.mark.parametrize("count", [2, 4, 8, 16])
def test_shared_quota_load_has_no_lost_duplicate_or_starved_requests(count):
    scheduler = resources.FairRequestScheduler(default_limit=2)
    barrier = threading.Barrier(count)
    mutex, observed, active = threading.Lock(), [], set()
    maximum = 0
    def work(index):
        nonlocal maximum
        barrier.wait(timeout=3)
        ticket = scheduler.acquire("account", session=f"session-{index % 4}", run_id=str(index))
        try:
            with mutex:
                assert ticket.session not in active
                active.add(ticket.session)
                maximum = max(maximum, len(active))
                observed.append(index)
            time.sleep(0.01)
        finally:
            with mutex:
                active.remove(ticket.session)
            scheduler.release("account", ticket)
    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(work, range(count)))
    assert sorted(observed) == list(range(count)) and maximum <= 2
    assert scheduler.snapshot()["items"] == []


def test_session_rotation_and_queued_cancellation():
    scheduler = resources.FairRequestScheduler(default_limit=1)
    first = scheduler.acquire("group", session="A")
    cancelled = threading.Event()
    order = []
    def work(session, cancellation=lambda: False):
        ticket = scheduler.acquire("group", session=session, cancelled=cancellation)
        order.append(session)
        scheduler.release("group", ticket)
    with ThreadPoolExecutor(max_workers=3) as pool:
        a = pool.submit(work, "A")
        eventually(lambda: len(scheduler.snapshot()["items"]) == 2)
        b = pool.submit(work, "B")
        c = pool.submit(work, "C", cancelled.is_set)
        eventually(lambda: len(scheduler.snapshot()["items"]) == 4)
        cancelled.set()
        with pytest.raises(resources.ResourceRequestCancelled):
            c.result(timeout=3)
        scheduler.release("group", first)
        a.result(timeout=3)
        b.result(timeout=3)
    assert order == ["A", "B"]  # Already enqueued A precedes B; no same-session overtaking.
    assert not scheduler.snapshot()["items"]


def test_http_429_and_independent_tools_cooldown_without_holding_slot(monkeypatch):
    monkeypatch.setattr(resources.random, "uniform", lambda a, b: 0)
    response = httpx.Response(429, headers={"Retry-After": "0.05"}, request=httpx.Request("GET", "https://test.test"))
    error = httpx.HTTPStatusError("rate limit", request=response.request, response=response)
    assert resources._retry_after(error) == 0.05
    class IndependentToolError(Exception):
        status_code = 429
        retry_after_seconds = 0.07
    assert resources._retry_after(IndependentToolError()) == 0.07
    scheduler = resources.FairRequestScheduler()
    one = scheduler.acquire("group", session="one")
    scheduler.release("group", one, cooldown_seconds=0.05)
    start = time.monotonic()
    two = scheduler.acquire("group", session="two")
    assert time.monotonic() - start >= 0.045
    scheduler.release("group", two)


def test_budget_reservations_are_atomic_across_parent_and_children():
    identity = budget.create_budget(owner_run_id="parent", amount_limit="1", request_estimate="0.3", price_version="user-estimate-v1")
    def attempt(index):
        try:
            return budget.reserve(identity, run_id=f"child-{index}")
        except budget.ResourceBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=16) as pool:
        reservations = list(pool.map(attempt, range(16)))
    assert len([r for r in reservations if r]) == 3
    snapshot = budget.budget_snapshot(identity)
    assert Decimal(snapshot["reserved_amount"]) == Decimal("0.9")
    assert Decimal(snapshot["remaining_amount"]) == Decimal("0.1")


def test_unknown_charges_survive_error_and_reconcile_exactly_once():
    identity = budget.create_budget(owner_run_id="parent", amount_limit="1", request_estimate="0.4", price_version="estimate-v1")
    reservation = budget.reserve(identity, run_id="child")
    budget.bind_call(reservation, "call-1")
    budget.finish(reservation, active_seconds=1.1)
    snapshot = budget.budget_snapshot(identity)
    assert snapshot["pending_reconciliation"] == 1 and snapshot["reserved_amount"] == "0.4"
    assert snapshot["spent_amount"] == "0" and snapshot["active_seconds"] == 1.1
    budget.finish(reservation, known_amount="0.2", reconcile=True)
    budget.finish(reservation, known_amount="0.2", reconcile=True)
    snapshot = budget.budget_snapshot(identity)
    assert snapshot["spent_amount"] == "0.2" and snapshot["reserved_amount"] == "0"
    assert snapshot["active_seconds"] == 1.1
    with pytest.raises(budget.ResourceBudgetError):
        budget.finish(reservation, known_amount="0", reconcile=True)


def test_unlimited_default_and_explicit_time_budget_dont_become_step_limit(monkeypatch):
    monkeypatch.setattr(resources, "scheduler", resources.FairRequestScheduler())
    identity = budget.create_budget(owner_run_id="parent", active_seconds_limit=0.1)
    for _ in range(30):
        with resources.request_admission(provider="test", endpoint="https://test.test/chat"):
            pass
    reservation = budget.reserve(identity)
    budget.bind_call(reservation, "slow-call")
    budget.finish(reservation, active_seconds=0.2)
    with pytest.raises(budget.ResourceBudgetExceeded):
        budget.reserve(identity)
    snapshot = budget.budget_snapshot(identity)
    budget.increase_budget(identity, active_seconds_limit=2, expected_revision=snapshot["revision"])
    assert budget.reserve(identity)


def test_pre_dispatch_failure_releases_but_dispatched_failure_stays_unknown(monkeypatch):
    monkeypatch.setattr(resources, "scheduler", resources.FairRequestScheduler())
    identity = budget.create_budget(owner_run_id="parent", amount_limit="1", request_estimate="0.4", price_version="estimate-v1")
    with bind_runtime_config({"run_id": "parent", "resource_budget_id": identity}):
        with pytest.raises(ValueError):
            with resources.request_admission(provider="test", endpoint="https://test.test/chat"):
                raise ValueError("before dispatch")
        assert budget.budget_snapshot(identity)["reserved_amount"] == "0"
        with pytest.raises(ValueError):
            with resources.request_admission(provider="test", endpoint="https://test.test/chat") as admission:
                admission.bind_call("actual-attempt")
                raise ValueError("after dispatch")
        assert budget.budget_snapshot(identity)["reserved_amount"] == "0.4"
    assert resources.scheduler.snapshot()["items"] == []


def test_cancelled_admission_creates_no_reservation_and_no_active_slot(monkeypatch):
    monkeypatch.setattr(resources, "scheduler", resources.FairRequestScheduler())
    identity = budget.create_budget(owner_run_id="parent", amount_limit=1, request_estimate=".1", price_version="estimate")
    cancel_store.request_cancel("parent")
    try:
        with pytest.raises(resources.ResourceRequestCancelled):
            with resources.request_admission(provider="test", endpoint="https://test.test",
                    runtime_config={"run_id": "parent", "resource_budget_id": identity}):
                pytest.fail("cancelled request dispatched")
    finally:
        cancel_store.clear("parent")
    assert not budget.budget_snapshot(identity)["reservations"]
    assert resources.scheduler.snapshot()["items"] == []
