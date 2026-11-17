"""Short, serialized budget transactions shared by a run and its children.

Money is stored as integer millionths of the specified currency. Reservations
are admission estimates, not a promise about charges at a remote supplier.
Unknown charges retain their reservation until explicitly reconciled.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import math
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import select, text
from .db import SessionLocal
from .resource_models import ResourceBudget, ResourceReservation, ResourceStartupRecovery
from .timeutil import utcnow


class ResourceBudgetError(ValueError):
    pass


class ResourceBudgetExceeded(ResourceBudgetError):
    """Recoverable admission boundary; raising this must not discard work."""


def _money(value):
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number < 0 or number > 10**9:
            raise ValueError()
        return int((number * 10**6).to_integral_value(rounding=ROUND_CEILING))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ResourceBudgetError("预算金额须为有限非负数") from exc


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\w./:@+-]{1,160}", value) or value.lower().startswith("sk-"):
        raise ResourceBudgetError("资源标识无效")
    return value


@contextmanager
def _write():
    # BEGIN IMMEDIATE takes the writer lock before the read/modify/write, also
    # across processes. Never accept a caller Session that may contain writes.
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def create_budget(*, owner_run_id, currency="USD", amount_limit=None,
                  request_estimate=None, price_version=None, active_seconds_limit=None):
    _id(owner_run_id)
    if not re.fullmatch(r"[A-Z]{3}", currency or ""):
        raise ResourceBudgetError("预算币种无效")
    if price_version is not None:
        _id(price_version)
    if request_estimate is not None and not price_version:
        raise ResourceBudgetError("请求费用预留必须注明估算来源版本")
    if active_seconds_limit is not None and (type(active_seconds_limit) not in (int, float)
            or not math.isfinite(active_seconds_limit) or not 0 < active_seconds_limit <= 10**9):
        raise ResourceBudgetError("时间预算须为有限正数")
    with _write() as db:
        row = ResourceBudget(id=uuid4().hex, owner_run_id=owner_run_id, currency=currency,
            limit_micros=_money(amount_limit) if amount_limit is not None else None,
            request_estimate_micros=_money(request_estimate) if request_estimate is not None else None,
            request_price_version=price_version,
            seconds_limit_ms=math.ceil(active_seconds_limit * 1000) if active_seconds_limit is not None else None)
        db.add(row)
        db.flush()
        result = row.id
    return result


def budget_snapshot(budget_id):
    with SessionLocal() as db:
        row = db.get(ResourceBudget, budget_id)
        if row is None:
            raise ResourceBudgetError("预算不存在")
        reservations = list(db.scalars(select(ResourceReservation).where(ResourceReservation.budget_id == budget_id)))
        recoveries = {r.reservation_id: r for r in db.scalars(select(ResourceStartupRecovery).where(
            ResourceStartupRecovery.budget_id == budget_id))}
        from .pricing_models import BudgetChargeEvent
        charges = list(db.scalars(select(BudgetChargeEvent).where(BudgetChargeEvent.reservation_id.in_([r.id for r in reservations])))) if reservations else []
        latest = {}
        for charge in charges:
            if charge.reservation_id not in latest or latest[charge.reservation_id].revision < charge.revision:
                latest[charge.reservation_id] = charge
        estimated = sum(c.amount_micros for c in latest.values() if c.source == 'estimated_tariff')
        confirmed = row.spent_micros - estimated
        money = lambda value: str(Decimal(value) / 10**6) if value is not None else None
        return {"id": row.id, "owner_run_id": row.owner_run_id, "currency": row.currency,
            "amount_limit": money(row.limit_micros), "spent_amount": money(row.spent_micros),
            "reserved_amount": money(row.reserved_micros), "revision": row.revision,
            'estimated_spent_amount': money(estimated), 'confirmed_spent_amount': money(confirmed),
            "remaining_amount": money(max(0, row.limit_micros - row.spent_micros - row.reserved_micros)) if row.limit_micros is not None else None,
            "active_seconds_limit": row.seconds_limit_ms / 1000 if row.seconds_limit_ms is not None else None,
            "active_seconds": row.active_ms / 1000, "time_scope": "sum_of_provider_request_seconds",
            "excludes_queue_and_user_wait": True, "remote_hard_cap": False,
            "pending_reconciliation": sum(r.state in ("in_flight", "pending_reconciliation") for r in reservations),
            "reservations": [{"id": r.id, "call_id": r.call_id, "run_id": r.run_id, "state": r.state,
                "startup_recovery": {"action": recoveries[r.id].action,
                    "reasons": recoveries[r.id].reasons,
                    "recovered_at": recoveries[r.id].recovered_at.isoformat(),
                    "released_amount": money(recoveries[r.id].released_micros),
                    "supplier_charge_known": False} if r.id in recoveries else None,
                'settlement_source': latest[r.id].source if r.id in latest else None,
                "reserved_amount": money(r.reserved_micros), "settled_amount": money(r.settled_micros)} for r in reservations]}


def recover_startup_reservations():
    """Reconcile old local reservations only under exclusive startup ownership.

    Every HTTP outlet binds its durable call before dispatch. An unbound
    ``reserved`` row therefore proves no dispatch through that boundary. Any
    binding, charge, contradictory state or inconsistent counter preserves the
    hold for reconciliation. No remote operation or retry happens here.
    """
    from .runtime_owner import owner
    from .ledger_models import CallRecord
    from .pricing_models import BudgetChargeEvent
    from .call_ledger import _recover_started_calls

    with owner.startup_recovery_guard() as instance_id, _write() as db:
        database = db.get_bind().url.database
        if not database or Path(database).resolve().parent != owner.directory.resolve():
            raise ResourceBudgetError("恢复所有者与数据库目录不一致，拒绝调整资源预留")
        rows = list(db.scalars(select(ResourceReservation).where(
            ResourceReservation.state.not_in(("released", "settled")))))
        budgets = {row.id: row for row in db.scalars(select(ResourceBudget))}
        outstanding = {}
        for row in rows:
            outstanding[row.budget_id] = outstanding.get(row.budget_id, 0) + row.reserved_micros
        charged = set(db.scalars(select(BudgetChargeEvent.reservation_id)))
        recovered = set(db.scalars(select(ResourceStartupRecovery.reservation_id)))
        counts = {"released": 0, "retained_for_reconciliation": 0, "calls_marked_unknown": 0}
        for row in rows:
            if row.id in recovered:
                continue
            budget = budgets.get(row.budget_id)
            call = db.get(CallRecord, row.call_id) if row.call_id else None
            reasons = []
            if budget is None:
                reasons.append("missing_budget")
            elif budget.reserved_micros != outstanding[row.budget_id] or budget.reserved_micros < 0:
                reasons.append("budget_counter_mismatch")
            if row.reserved_micros < 0 or row.settled_micros is not None or row.id in charged:
                reasons.append("inconsistent_charge_facts")
            if row.call_id:
                reasons.append("bound_call_outcome_or_charge_unconfirmed")
                if call is None:
                    reasons.append("missing_bound_call")
                elif call.run_id != row.run_id:
                    reasons.append("call_run_mismatch")
            elif row.state != "reserved":
                reasons.append("unbound_non_reserved_state")

            previous_state = row.state
            before = budget.reserved_micros if budget else None
            released = 0
            if not reasons:
                reasons.append("unbound_before_dispatch")
                row.state = "released"
                row.finished_at = utcnow()
                released = row.reserved_micros
                budget.reserved_micros -= released
                outstanding[row.budget_id] -= released
                action = "released_unbound"
                counts["released"] += 1
            else:
                row.state = "pending_reconciliation"
                action = "retained_for_reconciliation"
                counts["retained_for_reconciliation"] += 1
            if budget:
                budget.revision += 1
            db.add(ResourceStartupRecovery(reservation_id=row.id, instance_id=instance_id,
                budget_id=row.budget_id, run_id=row.run_id, call_id=row.call_id,
                action=action, reasons=reasons, previous_state=previous_state,
                previous_call_state=call.state if call else None, released_micros=released,
                previous_budget_reserved_micros=before,
                resulting_budget_reserved_micros=budget.reserved_micros if budget else None))
        counts["calls_marked_unknown"] = _recover_started_calls(db)
        return counts


def increase_budget(budget_id, *, amount_limit=None, active_seconds_limit=None, expected_revision):
    """Explicit user change; lowering below spent + reserved is not supported."""
    with _write() as db:
        row = db.get(ResourceBudget, budget_id)
        if row is None or row.revision != expected_revision:
            raise ResourceBudgetError("预算已变化，请刷新后重试")
        if amount_limit is not None:
            limit = _money(amount_limit)
            if limit < row.spent_micros + row.reserved_micros:
                raise ResourceBudgetError("预算不能低于已用及待核对预留费用")
            row.limit_micros = limit
        if active_seconds_limit is not None:
            if type(active_seconds_limit) not in (int, float) or not math.isfinite(active_seconds_limit) or not 0 < active_seconds_limit <= 10**9:
                raise ResourceBudgetError("时间预算无效")
            row.seconds_limit_ms = math.ceil(active_seconds_limit * 1000)
        row.revision += 1
    return budget_snapshot(budget_id)


def reserve(budget_id, *, run_id=None):
    with _write() as db:
        row = db.get(ResourceBudget, budget_id)
        if row is None:
            raise ResourceBudgetError("任务预算不存在，不能绕过预算派发请求")
        unresolved = db.execute(select(ResourceStartupRecovery.reasons).join(
            ResourceReservation, ResourceReservation.id == ResourceStartupRecovery.reservation_id).where(
                ResourceStartupRecovery.budget_id == budget_id,
                ResourceReservation.state == "pending_reconciliation")).scalars()
        contradictions = {"budget_counter_mismatch", "inconsistent_charge_facts", "missing_bound_call",
                          "call_run_mismatch", "unbound_non_reserved_state"}
        if any(contradictions.intersection(reasons) for reasons in unresolved):
            raise ResourceBudgetExceeded("预算恢复发现待核对的矛盾记录；核对完成前已保留成果并停止派发新请求")
        if row.seconds_limit_ms is not None and row.active_ms >= row.seconds_limit_ms:
            raise ResourceBudgetExceeded("已到所设请求执行时间预算；已保存工作，可增加预算后继续")
        amount = row.request_estimate_micros
        if row.limit_micros is not None:
            if amount is None or amount <= 0:
                raise ResourceBudgetExceeded("此费用预算缺少有效的单次请求估算，需配置估算后继续")
            if row.spent_micros + row.reserved_micros + amount > row.limit_micros:
                raise ResourceBudgetExceeded("可用预算不足以派发下一请求；待核对费用仍保留，可调整预算后继续")
        amount = amount or 0
        reservation = ResourceReservation(id=uuid4().hex, budget_id=row.id, run_id=run_id,
            reserved_micros=amount, price_version=row.request_price_version)
        db.add(reservation)
        row.reserved_micros += amount
        row.revision += 1
        db.flush()
        return reservation.id


def bind_call(reservation_id, call_id):
    with _write() as db:
        row = db.get(ResourceReservation, reservation_id)
        if row is None or row.state != "reserved":
            raise ResourceBudgetError("请求预留不可重复绑定")
        row.call_id = _id(call_id)
        row.state = "in_flight"


def finish(reservation_id, *, active_seconds=0, known_amount=None, confirmed_no_dispatch=False,
           reconcile=False, amount_source='externally_confirmed', reference=None):
    """Complete tariff estimates stay labelled estimates; partial costs never settle."""
    if amount_source not in ('estimated_tariff', 'user_confirmed', 'externally_confirmed'):
        raise ResourceBudgetError('费用结算来源无效')
    if type(active_seconds) not in (int, float) or not math.isfinite(active_seconds) or active_seconds < 0:
        raise ResourceBudgetError("请求计时无效")
    with _write() as db:
        row = db.get(ResourceReservation, reservation_id)
        if row is None:
            raise ResourceBudgetError("预留记录不存在")
        if row.state in ("released", "settled"):
            if known_amount is not None and row.settled_micros != _money(known_amount):
                raise ResourceBudgetError("已结算金额不可重复覆盖")
            return
        if row.state == "pending_reconciliation" and not reconcile:
            return
        budget = db.get(ResourceBudget, row.budget_id)
        measured = math.ceil(active_seconds * 1000)
        budget.active_ms += max(0, measured - row.active_ms)
        row.active_ms = max(row.active_ms, measured)
        if confirmed_no_dispatch:
            if row.call_id or row.state != "reserved":
                raise ResourceBudgetError("已派发请求不能按未派发释放费用")
            row.state = "released"
            budget.reserved_micros -= row.reserved_micros
        elif known_amount is not None:
            from .pricing import settle_reservation
            settle_reservation(db, row, amount_micros=_money(known_amount), source=amount_source, reference=reference)
        else:
            row.state = "pending_reconciliation"
        row.finished_at = utcnow()
        if known_amount is None:
            budget.revision += 1
