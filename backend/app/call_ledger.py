"""Durable, sanitized call attempts and usage receipts.

With no ``db`` argument each operation commits its own short transaction, so
the attempt exists before an external request starts. An explicit Session is
flushed but never committed here. No prompt, key, request/response body, raw
error or arbitrary metadata is accepted. This module does not reserve budgets.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from sqlalchemy import select, update, and_, or_, tuple_

from .db import SessionLocal
from .ledger_models import CallRecord, CallUsageReceipt
from .timeutil import utcnow


class CallLedgerError(ValueError):
    pass


class CallLedgerConflict(CallLedgerError):
    pass


STATES = {"started", "completed", "failed", "cancelled", "interrupted", "unknown"}
TERMINAL = STATES - {"started"}
UNITS = {
    "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    "cached_tokens", "reasoning_tokens", "thinking_tokens", "web_search_requests", "web_fetch_requests",
    "tool_search", "tool_fetch", "tool_image_search", "ocr_pages", "ocr_images", "pages", "images",
    "audio_seconds", "input_audio_seconds", "output_audio_seconds", "characters", "requests",
}
GROUPS = {"server_tool_use", "prompt_tokens_details", "completion_tokens_details", "output_tokens_details", "input_tokens_details"}
_SAFE_ID = re.compile(r"^[\w./:@+-]+$", re.UNICODE)
_SENSITIVE = re.compile(r"(?i)(?:^|[^\w])(?:sk-|bearer\s|api[_-]?key[=:]|access[_-]?token[=:])")


@contextmanager
def _session(db):
    if db is not None:
        yield db
        db.flush()
    else:
        with SessionLocal.begin() as owned:
            yield owned


def _identifier(value, label, *, maximum=160, optional=False):
    if value is None and optional:
        return None
    if (not isinstance(value, str) or not value or len(value) > maximum
            or not _SAFE_ID.fullmatch(value) or _SENSITIVE.search(value)):
        raise CallLedgerError(f"{label}必须是脱敏的稳定标识符")
    return value


def _model_id(value):
    """Model names are provider-defined; preserve their exact case/punctuation."""
    if value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or "\u2028" in value or "\u2029" in value or _SENSITIVE.search(value)):
        raise CallLedgerError("模型名称必须是脱敏的单行标识符")
    return value


def _endpoint(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise CallLedgerError("接口地址无效")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError()
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname + (f":{parsed.port}" if parsed.port else "")
        # No userinfo, query, fragment or credential-looking route segment.
        path = unquote(parsed.path)
        path = re.sub(r"(?i)(?:sk-|api[_-]?key[=:]|access[_-]?token[=:])[^/]*", "[redacted]", path)
        path = re.sub(r"(?i)/(?:api[_-]?key|access[_-]?token|token)/[^/]+", "/[redacted]", path)
        if any(ord(c) < 32 for c in path):
            raise ValueError()
        result = urlunsplit((parsed.scheme, netloc, quote(path, safe="/[]:@!$&'()*+,;=-._~"), "", ""))
        if len(result) > 2048:
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise CallLedgerError("接口地址无效") from exc


def _usage(value):
    """Strict allowlist keeps provider counts and discards all textual data."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CallLedgerError("用量必须是对象")
    result = {}
    for key, count in value.items():
        if key in GROUPS and isinstance(count, dict):
            nested = _usage(count)
            for unit, number in nested.items():
                if unit in result and result[unit] != number:
                    raise CallLedgerError("同一用量单位出现冲突值")
                result[unit] = number
        elif key in UNITS:
            if (type(count) not in (int, float) or count < 0
                    or (type(count) is float and not math.isfinite(count))
                    or count > 10**18):
                raise CallLedgerError("用量计数必须是有限非负数")
            if key not in ("audio_seconds", "input_audio_seconds", "output_audio_seconds") and type(count) is not int:
                raise CallLedgerError("该用量单位必须为整数")
            if key in result and result[key] != count:
                raise CallLedgerError("同一用量单位出现冲突值")
            result[key] = count
    return result


def _record(db, call_id):
    record = db.get(CallRecord, call_id)
    if record is None:
        raise CallLedgerError("调用记录不存在")
    return record


def _snapshot(record, db=None):
    source = "actual" if record.actual_usage else "estimated" if record.estimated_usage else "unknown"
    usage = record.actual_usage if source == "actual" else record.estimated_usage if source == "estimated" else {}
    duration = None
    if record.finished_at:
        duration = max(0.0, (record.finished_at - record.created_at).total_seconds())
    from .pricing import public_assessment
    from .resource_models import SubtaskCallLink
    child = db.get(SubtaskCallLink, record.id) if db is not None else None
    return {"pricing": public_assessment(db, record.id) if db is not None else None,
            "subtask": {"id": child.subtask_id, "attempt": child.attempt} if child else None,
            "id": record.id, "call_id": record.id, "request_id": record.request_id,
            "run_id": record.run_id, "attempt_id": record.attempt_id, "parent_call_id": record.parent_call_id,
            "retry_of_call_id": record.retry_of_call_id, "retry_index": record.retry_index,
            "provider": record.provider, "service": record.service, "endpoint": record.endpoint,
            "model_id": record.model_id, "synthetic": bool(record.synthetic), "state": record.state,
            "revision": record.revision, "usage": deepcopy(usage), "usage_source": source,
            "actual_usage": deepcopy(record.actual_usage), "estimated_usage": deepcopy(record.estimated_usage),
            "provider_request_id": record.provider_request_id, "error_kind": record.error_kind,
            "price_version": record.price_version, "currency": record.currency,
            "amount": record.amount, "amount_source": record.amount_source,
            "priced_units": list((record.unit_prices or {}).keys()),
            "amount_scope": "priced_units_only" if record.amount is not None else "unknown",
            "created_at": record.created_at.isoformat(), "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            "duration_seconds": duration}


def _change(db, record, **values):
    revision = record.revision
    result = db.execute(update(CallRecord).where(CallRecord.id == record.id, CallRecord.revision == revision)
                        .values(**values, revision=revision + 1, updated_at=utcnow())
                        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise CallLedgerConflict("调用记录已更新，请按同一调用编号重试记账")
    db.expire(record)
    db.flush()


def begin_call(*, provider: str, service: str, endpoint: str, model_id: str | None = None,
               run_id: str | None = None, attempt_id: str | None = None, parent_call_id: str | None = None,
               request_id: str | None = None, retry_of_call_id: str | None = None,
               synthetic: bool = False, call_id: str | None = None, db=None) -> str:
    from .runtime_context import current_runtime_config
    binding = current_runtime_config() or {}
    run_id = run_id if run_id is not None else binding.get("run_id")
    attempt_id = attempt_id if attempt_id is not None else binding.get("attempt_id") if binding.get("run_id") == run_id else None
    provider = _identifier(provider, "供应商", maximum=100)
    service = _identifier(service, "服务", maximum=60)
    endpoint = _endpoint(endpoint)
    model_id = _model_id(model_id)
    run_id = _identifier(run_id, "运行", maximum=96, optional=True)
    attempt_id = _identifier(attempt_id, "尝试", maximum=96, optional=True)
    parent_call_id = _identifier(parent_call_id, "父调用", maximum=64, optional=True)
    retry_of_call_id = _identifier(retry_of_call_id, "重试来源", maximum=64, optional=True)
    call_id = _identifier(call_id or uuid.uuid4().hex, "调用", maximum=64)
    if type(synthetic) is not bool:
        raise CallLedgerError("合成调用标记必须为布尔值")
    with _session(db) as session:
        existing = session.get(CallRecord, call_id)
        child = None
        if binding.get('subtask_id') and binding.get('run_id') == run_id:
            from .resource_models import PersistentSubtask, SubtaskCallLink
            from .runtime_models import RuntimeRun
            child = session.get(PersistentSubtask, binding['subtask_id'])
            logical = session.get(RuntimeRun, run_id)
            if (not child or child.parent_run_id != run_id or child.attempt != binding.get('subtask_attempt')
                    or child.writer_token != binding.get('subtask_writer_token') or child.state != 'running'
                    or (child.checkpoint or {}).get('parent_attempt_id') != attempt_id
                    or logical and logical.current_attempt_id != attempt_id):
                raise CallLedgerConflict('子任务尝试已变化，不能以旧执行者派发请求')
        if call_id in (parent_call_id, retry_of_call_id):
            raise CallLedgerError("调用不能以自身为父调用或重试来源")
        retry = _record(session, retry_of_call_id) if retry_of_call_id else None
        if retry:
            from .resource_models import SubtaskCallLink
            prior_child = session.get(SubtaskCallLink, retry.id)
            if (prior_child.subtask_id if prior_child else None, prior_child.attempt if prior_child else None) != (child.id if child else None, child.attempt if child else None):
                raise CallLedgerConflict('重试调用必须归属同一子任务尝试')
        if parent_call_id:
            parent = _record(session, parent_call_id)
            if parent.run_id != run_id or parent.attempt_id != attempt_id or bool(parent.synthetic) != synthetic:
                raise CallLedgerError("父调用必须归属相同运行尝试及合成类型")
        if retry:
            if retry.run_id != run_id or retry.attempt_id != attempt_id or bool(retry.synthetic) != synthetic:
                raise CallLedgerError("重试必须归属原运行尝试及相同合成类型")
            if request_id is not None and request_id != retry.request_id:
                raise CallLedgerError("重试不能更改逻辑请求编号")
            request_id = retry.request_id
        request_id = _identifier(request_id or (existing.request_id if existing else uuid.uuid4().hex), "逻辑请求")
        facts = {"request_id": request_id, "run_id": run_id, "attempt_id": attempt_id,
                 "parent_call_id": parent_call_id, "retry_of_call_id": retry_of_call_id,
                 "provider": provider, "service": service, "endpoint": endpoint, "model_id": model_id,
                 "synthetic": int(synthetic)}
        if existing:
            # Idempotent admission is allowed only for the exact same call.
            if any(getattr(existing, key) != value for key, value in facts.items()):
                raise CallLedgerConflict("调用编号已绑定不同请求")
            from .resource_models import SubtaskCallLink
            link = session.get(SubtaskCallLink, call_id)
            if (link.subtask_id if link else None, link.attempt if link else None) != (child.id if child else None, child.attempt if child else None):
                raise CallLedgerConflict('调用编号已绑定其他子任务尝试')
            return call_id
        session.add(CallRecord(id=call_id, **facts, retry_index=retry.retry_index + 1 if retry else 0,
                               state="started", revision=1, actual_usage={}, estimated_usage={}, amount_source="unknown"))
        if child:
            session.add(SubtaskCallLink(call_id=call_id, subtask_id=child.id, attempt=child.attempt))
        session.flush()
        from .pricing import freeze
        freeze(session, _record(session, call_id), binding)
    return call_id


def get_call(call_id: str, *, db=None) -> dict:
    with _session(db) as session:
        return _snapshot(_record(session, call_id), session)


def list_calls(*, run_id: str | None = None, attempt_id: str | None = None,
               limit=50, offset=0, include_synthetic=False, db=None) -> dict:
    """Page sanitized facts only; no request bodies, credentials or error text."""
    run_id = _identifier(run_id, "运行", maximum=96, optional=True)
    attempt_id = _identifier(attempt_id, "尝试", maximum=96, optional=True)
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 10**6:
        raise CallLedgerError("调用分页范围无效")
    if type(include_synthetic) is not bool:
        raise CallLedgerError("合成调用筛选必须为布尔值")
    with _session(db) as session:
        query = select(CallRecord)
        query = _research_call_scope(session, query, run_id, attempt_id)
        if not include_synthetic:
            query = query.where(CallRecord.synthetic == 0)
        rows = list(session.scalars(query.order_by(CallRecord.created_at.desc(), CallRecord.id.desc())
                                   .offset(offset).limit(limit + 1)))
        more = len(rows) > limit
        return {"items": [_snapshot(record, session) for record in rows[:limit]], "limit": limit, "offset": offset,
                "has_more": more, "next_offset": offset + limit if more else None}


def record_usage(call_id: str, usage: dict | None, *, source="actual", cumulative=True,
                 event_id: str | None = None, db=None) -> dict:
    if source not in ("actual", "estimated", "unknown") or type(cumulative) is not bool:
        raise CallLedgerError("用量来源或累计模式无效")
    units = _usage(usage)
    if source == "unknown" and units:
        raise CallLedgerError("未知用量不能同时提供确定计数")
    if not cumulative and not event_id:
        raise CallLedgerError("增量用量必须带稳定事件编号以防重复累计")
    digest = hashlib.sha256(json.dumps([source, cumulative, units], sort_keys=True).encode()).hexdigest()
    event_id = _identifier(event_id or f"cumulative:{digest}", "用量事件")
    with _session(db) as session:
        record = _record(session, call_id)
        from .pricing import observe_usage
        observe_usage(session, record, usage)
        if not units:
            return _snapshot(record, session)
        if record.synthetic:
            source = "estimated"
        existing = session.scalar(select(CallUsageReceipt).where(
            CallUsageReceipt.call_id == call_id, CallUsageReceipt.event_id == event_id))
        mode = "cumulative" if cumulative else "incremental"
        if existing:
            if existing.units != units or existing.source != source or existing.mode != mode:
                raise CallLedgerConflict("同一用量事件不能改写已记录事实")
            return _snapshot(record, session)
        previous_modes = session.scalars(select(CallUsageReceipt).where(
            CallUsageReceipt.call_id == call_id, CallUsageReceipt.source == source)).all()
        if any(receipt.mode != mode and set(receipt.units) & set(units) for receipt in previous_modes):
            raise CallLedgerError("同一用量单位不能混用累计与增量模式")
        column = "actual_usage" if source == "actual" else "estimated_usage"
        merged = deepcopy(getattr(record, column))
        for unit, number in units.items():
            merged[unit] = max(merged.get(unit, 0), number) if cumulative else merged.get(unit, 0) + number
        _change(session, record, **{column: merged}, amount=None, amount_source="unknown")
        session.add(CallUsageReceipt(id=uuid.uuid4().hex, call_id=call_id, event_id=event_id,
                                     source=source, mode=mode, units=units))
        session.flush()
        return _snapshot(record, session)


def _price_data(price_version, unit_prices, currency):
    if price_version is None and unit_prices is None and currency is None:
        return None
    version = _identifier(price_version, "价格版本")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise CallLedgerError("费用币种无效")
    if not isinstance(unit_prices, dict) or not unit_prices or set(unit_prices) - UNITS:
        raise CallLedgerError("必须提供明确的计费单位及单价")
    prices = {}
    for unit, value in unit_prices.items():
        if type(value) not in (str, int, float, Decimal):
            raise CallLedgerError("单价无效")
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise CallLedgerError("单价无效") from exc
        if not number.is_finite() or number < 0:
            raise CallLedgerError("单价必须为有限非负数")
        prices[unit] = str(number)
    return version, prices, currency


def finish_call(call_id: str, *, state="completed", usage: dict | None = None, usage_source="actual",
                provider_request_id: str | None = None, error_kind: str | None = None,
                price_version=None, unit_prices=None, currency=None, reconcile=False, db=None) -> dict:
    if state not in TERMINAL:
        raise CallLedgerError("调用结束状态无效")
    provider_request_id = _identifier(provider_request_id, "供应商请求", maximum=200, optional=True)
    error_kind = _identifier(error_kind, "错误分类", maximum=100, optional=True)
    prices = _price_data(price_version, unit_prices, currency)
    with _session(db) as session:
        record = _record(session, call_id)
        if record.state in TERMINAL and record.state != state and not (record.state == "unknown" and reconcile is True):
            raise CallLedgerConflict("调用终态不可被重复结束改写")
        if provider_request_id and record.provider_request_id and record.provider_request_id != provider_request_id:
            raise CallLedgerConflict("供应商请求编号不能覆盖")
        if usage is not None:
            record_usage(call_id, usage, source=usage_source, db=session)
        record = _record(session, call_id)
        values = {}
        if record.state != state:
            values.update(state=state, finished_at=record.finished_at or utcnow())
        if provider_request_id and record.provider_request_id is None:
            values["provider_request_id"] = provider_request_id
        if error_kind and record.error_kind is None:
            values["error_kind"] = error_kind
        if prices:
            version, units, money = prices
            if record.price_version and (record.price_version != version or record.unit_prices != units or record.currency != money):
                raise CallLedgerConflict("已记录价格版本不能被覆盖")
            values.update(price_version=version, unit_prices=units, currency=money)
        effective_prices = prices or ((record.price_version, record.unit_prices, record.currency) if record.price_version else None)
        if effective_prices and not record.synthetic:
            _, rates, _ = effective_prices
            counts = record.actual_usage or record.estimated_usage
            if counts and all(unit in counts for unit in rates):
                amount = sum((Decimal(str(counts[unit])) * Decimal(rate) for unit, rate in rates.items()), Decimal("0"))
                # These are only the explicitly priced components. Without a
                # complete provider tariff we cannot claim a total charge.
                values.update(amount=str(amount), amount_source="estimated_partial")
        # Repeated completion with identical facts changes no revision or count.
        values = {key: value for key, value in values.items() if getattr(record, key) != value}
        if values:
            _change(session, record, **values)
        from .pricing import assess
        assess(session, record)
        session.flush()
        return _snapshot(record, session)


def _recover_started_calls(db):
    """Startup-only caller must own the data-directory lock and transaction.

    A durable started receipt is not proof that bytes reached the supplier.
    Keep its usage and pricing facts, mark the outcome unknown, and leave the
    finish time unset: startup time cannot establish the request's duration.
    This also covers a call committed just before reservation binding crashed.
    """
    rows = list(db.scalars(select(CallRecord).where(CallRecord.state == "started")))
    for row in rows:
        _change(db, row, state="unknown", error_kind=row.error_kind or "StartupInterrupted")
    return len(rows)


def _research_call_scope(session, query, run_id, attempt_id):
    if run_id is not None:
        from .research_children import descendant_call_scope
        scopes = descendant_call_scope(session, run_id, attempt_id)
        whole = [rid for rid, aid in scopes if aid is None]
        attempts = [(rid, aid) for rid, aid in scopes if aid is not None]
        conditions = ([CallRecord.run_id.in_(whole)] if whole else [])
        if attempts:
            conditions.append(tuple_(CallRecord.run_id, CallRecord.attempt_id).in_(attempts))
        return query.where(or_(*conditions))
    return query.where(CallRecord.attempt_id == attempt_id) if attempt_id is not None else query


def summarize_calls(*, run_id: str | None = None, attempt_id: str | None = None,
                    include_synthetic=False, db=None) -> dict:
    with _session(db) as session:
        query = select(CallRecord)
        query = _research_call_scope(session, query, run_id, attempt_id)
        if not include_synthetic:
            query = query.where(CallRecord.synthetic == 0)
        rows = list(session.scalars(query))
        actual, estimated, synthetic_usage, amounts = {}, {}, {}, {}
        for row in rows:
            if row.synthetic:
                for unit, number in row.estimated_usage.items():
                    synthetic_usage[unit] = synthetic_usage.get(unit, 0) + number
                continue
            for destination, source in ((actual, row.actual_usage), (estimated, row.estimated_usage if not row.actual_usage else {})):
                for unit, number in source.items():
                    destination[unit] = destination.get(unit, 0) + number
            if row.amount is not None and row.currency:
                amounts[row.currency] = amounts.get(row.currency, Decimal("0")) + Decimal(row.amount)
        real = [r for r in rows if not r.synthetic]
        unknown = sum(not r.actual_usage and not r.estimated_usage for r in real)
        missing_amount = sum(r.amount is None for r in real)
        from .pricing import public_assessment
        pricing_totals = {'complete_estimates': {}, 'partial_estimates': {}, 'user_confirmed': {}, 'unknown_calls': 0}
        for row in real:
            assessment = public_assessment(session, row.id)
            group = {'complete_estimate': 'complete_estimates', 'partial_estimate': 'partial_estimates', 'user_confirmed': 'user_confirmed'}.get(assessment['status'])
            if group and assessment['amount'] is not None and assessment['currency']:
                totals = pricing_totals[group]; currency = assessment['currency']
                totals[currency] = str(Decimal(totals.get(currency, '0')) + Decimal(assessment['amount']))
            else:
                pricing_totals['unknown_calls'] += 1
        return {"call_count": len(rows), "actual_usage": actual, "estimated_usage": estimated,
                'pricing': pricing_totals,
                "unknown_usage_calls": unknown, "amounts": {key: str(value) for key, value in amounts.items()},
                "unknown_amount_calls": missing_amount, "amount_complete": False,
                "priced_amount_calls": len(real) - missing_amount, "amounts_scope": "priced_units_only",
                "real_call_count": len(real), "synthetic_call_count": len(rows) - len(real),
                "synthetic_usage": synthetic_usage,
                "includes_synthetic": bool(include_synthetic)}


class CallTracker:
    def __init__(self, call_id):
        self.call_id = self.id = call_id

    def usage(self, usage, **kwargs):
        return record_usage(self.call_id, usage, **kwargs)

    def finish(self, **kwargs):
        return finish_call(self.call_id, **kwargs)

    def snapshot(self):
        return get_call(self.call_id)


@contextmanager
def tracked_call(**kwargs):
    """Persist before I/O; exception class only, never its sensitive message."""
    if kwargs.get("db") is not None:
        raise CallLedgerError("跨网络上下文使用自管理事务，不能持有调用方数据库事务")
    tracker = CallTracker(begin_call(**kwargs))
    try:
        yield tracker
    except BaseException as exc:
        try:
            if tracker.snapshot()["state"] == "started":
                kind = type(exc).__name__
                state = "cancelled" if kind in ("CancelledError", "GeneratorExit", "KeyboardInterrupt") else "failed"
                finish_call(tracker.call_id, state=state, error_kind=kind)
        except Exception as ledger_error:
            # Preserve the external exception if final ledger I/O also fails.
            # A durable started row remains available for reconciliation.
            exc.add_note(f"调用账本待对账：{type(ledger_error).__name__}")
        raise
    else:
        if tracker.snapshot()["state"] == "started":
            tracker.finish()
