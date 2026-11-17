"""One admission boundary for each real HTTP attempt, including streaming.

The desktop backend has one scheduler process. Quotas are shared across model
profiles by origin and credential fingerprint, not model names. Persistent
budgets serialize across processes; this scheduler is deliberately process-local.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .runtime_context import current_runtime_config


class ResourceRequestCancelled(RuntimeError):
    pass


class QuotaPolicyChanged(RuntimeError):
    """A late resolver must obtain current membership before joining a queue."""
    pass


def raise_if_resource_boundary(exc):
    """Fallbacks may degrade quality, never override a user's resource limit."""
    from .resource_budget import ResourceBudgetExceeded
    from .agent.active_context import ContextCapacityExceeded
    from .skill_store import SkillsChanged
    from .memory_governance import MemoryScopeChanged
    from .account_quotas import QuotaPolicyError
    if isinstance(exc, (ResourceBudgetExceeded, ContextCapacityExceeded, ResourceRequestCancelled, SkillsChanged, MemoryScopeChanged, QuotaPolicyError)):
        raise exc


def quota_key(*, provider, endpoint, credential_fingerprint=None, quota_group_id=None):
    from .account_quotas import origin
    actual_origin = origin(endpoint)
    identity = [actual_origin, "explicit", quota_group_id] if quota_group_id else [actual_origin, "credential", credential_fingerprint or "anonymous"]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


@dataclass
class _Ticket:
    session: str
    run_id: str | None
    service: str
    id: str = field(default_factory=lambda: uuid4().hex)
    queued_at: float = field(default_factory=time.monotonic)


class _Queue:
    def __init__(self, limit):
        self.limit = limit
        self.sessions = deque()
        self.waiting = {}
        self.active = {}
        self.cooldown_until = 0.0


class FairRequestScheduler:
    def __init__(self, default_limit=2):
        self.default_limit = default_limit
        self.condition = threading.Condition()
        self.groups = {}
        self.limits = {}
        self.limit_revisions = {}
        self.policy_store_id, self.policy_revision = None, -1

    def configure(self, group, limit, *, policy_revision=None):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("供应商并发数须在 1–64 之间")
        with self.condition:
            if policy_revision is not None and policy_revision < self.limit_revisions.get(group, -1):
                return False
            if policy_revision is not None:
                self.limit_revisions[group] = policy_revision
            self.limits[group] = limit
            if group in self.groups:
                self.groups[group].limit = limit
            self.condition.notify_all()
            return True

    def apply_policy(self, snapshot):
        from .account_quotas import QuotaPolicyError
        with self.condition:
            incoming = snapshot["store_id"]
            if self.policy_store_id not in (None, incoming, "unconfigured"):
                if incoming == "unconfigured":
                    raise QuotaPolicyChanged("配额策略已变化，请重新解析")
                raise QuotaPolicyError("账号配额存储身份发生变化，请重启后核对；本次未派发", 409)
            if incoming == self.policy_store_id and snapshot["revision"] < self.policy_revision:
                raise QuotaPolicyChanged("较旧的配额策略不能覆盖新策略")
            for group in snapshot["groups"]:
                key = quota_key(provider="", endpoint=group["origin"], quota_group_id=group["id"])
                self.configure(key, group["concurrency"], policy_revision=snapshot["revision"])
            self.policy_store_id, self.policy_revision = incoming, snapshot["revision"]
            self.condition.notify_all()
            return (self.policy_store_id, self.policy_revision)

    def _remove(self, group, ticket):
        queue = self.groups[group]
        waiting = queue.waiting.get(ticket.session)
        if waiting and ticket in waiting:
            waiting.remove(ticket)
        if waiting is not None and not waiting:
            del queue.waiting[ticket.session]
            queue.sessions.remove(ticket.session)
        if not queue.waiting and not queue.active and queue.cooldown_until <= time.monotonic():
            self.groups.pop(group, None)

    def acquire(self, group, *, session, run_id=None, service="request", cancelled=lambda: False, policy_token=None):
        ticket = _Ticket(session, run_id, service)
        with self.condition:
            if policy_token is not None and policy_token != (self.policy_store_id, self.policy_revision):
                raise QuotaPolicyChanged("配额路由在排队前发生变化")
            queue = self.groups.setdefault(group, _Queue(self.limits.get(group, self.default_limit)))
            if session not in queue.waiting:
                queue.waiting[session] = deque()
                queue.sessions.append(session)
            queue.waiting[session].append(ticket)
            try:
                while True:
                    if cancelled():
                        raise ResourceRequestCancelled("任务已取消，排队请求未派发")
                    if policy_token is not None and policy_token != (self.policy_store_id, self.policy_revision):
                        raise QuotaPolicyChanged("配额路由在等待期间发生变化")
                    active_sessions = {value.session for value in queue.active.values()}
                    eligible = next((s for s in queue.sessions if s not in active_sessions), None)
                    if (len(queue.active) < queue.limit and eligible == session
                            and queue.waiting[session][0] is ticket and queue.cooldown_until <= time.monotonic()):
                        queue.active[ticket.id] = ticket
                        self._remove(group, ticket)
                        # Round-robin: a session with more pending work goes last.
                        if session in queue.sessions:
                            queue.sessions.remove(session)
                            queue.sessions.append(session)
                        self.condition.notify_all()
                        return ticket
                    self.condition.wait(timeout=0.1)
            except BaseException:
                self._remove(group, ticket)
                self.condition.notify_all()
                raise

    def release(self, group, ticket, *, cooldown_seconds=0):
        with self.condition:
            queue = self.groups.get(group)
            if queue is None:
                return
            queue.active.pop(ticket.id, None)
            queue.cooldown_until = max(queue.cooldown_until, time.monotonic() + max(0, cooldown_seconds))
            self._remove(group, ticket)
            self.condition.notify_all()

    def snapshot(self, run_id=None):
        now = time.monotonic()
        with self.condition:
            items = []
            for queue in self.groups.values():
                for state, tickets in (("active", list(queue.active.values())),
                                       ("queued", [t for s in queue.sessions for t in queue.waiting[s]])):
                    for ticket in tickets:
                        if run_id is None or ticket.run_id == run_id:
                            items.append({"id": ticket.id, "run_id": ticket.run_id, "service": ticket.service,
                                "state": state, "wait_seconds": max(0, now - ticket.queued_at) if state == "queued" else None,
                                "reason": "provider_retry_after" if queue.cooldown_until > now else "shared_provider_quota" if state == "queued" else None})
            return {"items": items, "scope": "single_backend_process", "policy": "round_robin_sessions"}


scheduler = FairRequestScheduler()


def _retry_after(exc):
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 429 and getattr(exc, "status_code", None) != 429:
        return 0
    value = response.headers.get("Retry-After", "") if response is not None else getattr(exc, "retry_after_seconds", "")
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 1.0
    if not (delay >= 0 and delay < float("inf")):
        delay = 1.0
    return delay + random.uniform(0, 0.25)


class RequestAdmission:
    def __init__(self, reservation_id, timing_identity=None):
        self.reservation_id = reservation_id
        self.timing_identity = timing_identity
        self.call_id = None
        self.known_amount = None

    def bind_call(self, call_id):
        if self.call_id is not None:
            raise ValueError("请求只能绑定一个调用尝试")
        from .run_timing import request_boundary
        request_boundary(self.timing_identity, "bind", call_id)
        if self.reservation_id:
            from .resource_budget import bind_call
            bind_call(self.reservation_id, call_id)
        self.call_id = call_id


@contextmanager
def request_admission(*, provider, endpoint, credential_fingerprint=None, service="request",
                      runtime_config=None, call_id=None, run_id=None, cancelled=None):
    from . import cancel_store
    from . import resource_budget
    binding = current_runtime_config() or runtime_config or {}
    run_id = run_id or binding.get("run_id")
    cancel = cancelled or (lambda: cancel_store.is_cancelled(run_id))
    session = binding.get("conversation_id") or binding.get("parent_run_id") or run_id or uuid4().hex
    from .db import SessionLocal
    from .account_quotas import resolve_request
    from .run_timing import begin_request, request_boundary
    timing_identity = begin_request(binding, run_id, service)
    try:
        while True:
            if cancel():
                raise ResourceRequestCancelled("任务已取消，排队请求未派发")
            with SessionLocal() as quota_db:
                resolved = resolve_request(quota_db, endpoint, credential_fingerprint)
            group = quota_key(provider=provider, endpoint=endpoint, credential_fingerprint=credential_fingerprint,
                              quota_group_id=resolved["group_id"])
            try:
                token = scheduler.apply_policy(resolved["policy"])
                ticket = scheduler.acquire(group, session=session, run_id=run_id, service=service,
                                           cancelled=cancel, policy_token=token)
                break
            except QuotaPolicyChanged:
                continue
    except BaseException:
        request_boundary(timing_identity, "cancelled")
        raise
    started = time.monotonic()
    admission = RequestAdmission(None, timing_identity)
    cooldown = 0
    failure = None
    try:
        request_boundary(timing_identity, "acquired")
        if cancel():
            raise ResourceRequestCancelled("任务已取消，请求未派发")
        if binding.get("resource_budget_id"):
            admission.reservation_id = resource_budget.reserve(binding["resource_budget_id"], run_id=run_id)
        if call_id:
            admission.bind_call(call_id)
        yield admission
    except BaseException as exc:
        failure = exc
        cooldown = _retry_after(exc)
        raise
    finally:
        try:
            if admission.reservation_id:
                charge = None
                if admission.call_id and admission.known_amount is None:
                    from .db import SessionLocal
                    from .resource_models import ResourceBudget, ResourceReservation
                    from .pricing import budget_charge
                    with SessionLocal() as charge_db:
                        reservation = charge_db.get(ResourceReservation, admission.reservation_id)
                        budget = charge_db.get(ResourceBudget, reservation.budget_id)
                        charge = budget_charge(charge_db, admission.call_id, budget.currency)
                # A complete tariff can replace a reservation only as an
                # estimate. Missing dimensions/currency mismatches stay unknown.
                resource_budget.finish(admission.reservation_id, active_seconds=time.monotonic() - started,
                    known_amount=admission.known_amount if admission.known_amount is not None else charge['amount'] if charge else None,
                    amount_source=charge['source'] if charge else 'externally_confirmed', reference=charge['reference'] if charge else None,
                    confirmed_no_dispatch=admission.call_id is None)
        except BaseException as settlement_error:
            if failure is not None:
                failure.add_note(f"预算预留仍待核对：{type(settlement_error).__name__}")
            else:
                raise
        finally:
            try:
                request_boundary(timing_identity, "error" if failure else "closed")
            finally:
                scheduler.release(group, ticket, cooldown_seconds=cooldown)
