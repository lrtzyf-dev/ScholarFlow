"""Pricing is an estimate until separately confirmed against a bill.

No secret, prompt, response text or exchange-rate guess belongs in this module.
The caller's Session is used throughout a ledger transaction.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
from uuid import uuid4
from sqlalchemy import select
from .pricing_models import PricingVersion, CallPriceAssessment, CallChargeConfirmation, BudgetChargeEvent
from .timeutil import utcnow


class PricingError(ValueError):
    pass


def _number(value):
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number < 0 or number > 10**9 or number.as_tuple().exponent < -12:
            raise ValueError()
        return number
    except (ValueError, InvalidOperation, TypeError):
        raise PricingError('费用与单价必须为有限非负十进制数') from None


def _text(value, label, maximum=200):
    from .call_ledger import _SENSITIVE
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value) or _SENSITIVE.search(value):
        raise PricingError(f'{label}必须为不含凭证的单行文字')
    return value.strip()


def normalize(form):
    from .call_ledger import UNITS, _endpoint, _model_id, _identifier
    components, used = [], set()
    for source in form.get('components') or []:
        if not isinstance(source, dict) or set(source) - {'unit', 'subtract_units', 'rate', 'per'}:
            raise PricingError('计费项格式无效')
        unit, subtract = source.get('unit'), source.get('subtract_units') or []
        if unit not in UNITS or unit == 'total_tokens' or unit in used or not isinstance(subtract, list) or any(u not in UNITS or u == unit for u in subtract) or len(set(subtract)) != len(subtract):
            raise PricingError('计费单位重复、无效或包含重复扣除项；total_tokens 是总计不能再次收费')
        rate, per = _number(source.get('rate')), _number(source.get('per', '1'))
        if not per:
            raise PricingError('计价基数必须大于零')
        components.append({'unit': unit, 'subtract_units': subtract, 'rate': str(rate), 'per': str(per)})
        used.add(unit)
    if not 1 <= len(components) <= 30:
        raise PricingError('价目需包含 1–30 个计费项')
    # Alias counters describe the same work. Never charge both.
    for aliases in ({'input_tokens', 'prompt_tokens'}, {'output_tokens', 'completion_tokens'}, {'pages', 'ocr_pages'}, {'images', 'ocr_images'},
                    {'tool_search', 'web_search_requests'}, {'tool_fetch', 'web_fetch_requests'},
                    {'cached_tokens', 'cache_read_input_tokens', 'prompt_cache_hit_tokens'}, {'reasoning_tokens', 'thinking_tokens'}):
        if len(used & aliases) > 1:
            raise PricingError('同一工作量的别名不能重复计费')
    token_children = {'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'cached_tokens'}
    for item in components:
        if item['unit'] in ('input_tokens', 'prompt_tokens') and (used & token_children) - set(item['subtract_units']):
            raise PricingError('输入总量与缓存分量同时计费时，必须从输入总量扣除已单独收费分量')
        if item['unit'] in ('output_tokens', 'completion_tokens') and (used & {'reasoning_tokens', 'thinking_tokens'}) - set(item['subtract_units']):
            raise PricingError('输出总量与推理分量不能重叠计费')
    informational = form.get('informational_units') or []
    if not isinstance(informational, list) or any(u not in UNITS for u in informational) or used & set(informational):
        raise PricingError('非计费统计单位无效或与收费单位重复')
    currency = form.get('currency')
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        raise PricingError('币种须为三位大写代码；不自动换算币种')
    service = _identifier(form.get('service'), '服务', maximum=60)
    return {'name': _text(form.get('name'), '价目名称', 160), 'endpoint': _endpoint(form.get('endpoint')),
        'service': 'chat' if service == 'chat_stream' else service, 'model_id': _model_id(form.get('model_id')),
        'profile_id': _identifier(form.get('profile_id'), '模型档案', maximum=64, optional=True),
        'currency': currency, 'components': components, 'informational_units': sorted(set(informational) | {'total_tokens'}),
        'complete_contract': int(form.get('complete_contract') is True),
        'source_reference': _text(form.get('source_reference'), '价目依据')}


def public_version(row):
    return {key: deepcopy(getattr(row, key)) for key in ('id', 'name', 'endpoint', 'service', 'model_id', 'profile_id',
        'currency', 'components', 'informational_units', 'complete_contract', 'source_reference', 'active')}


def create_version(db, form):
    value = normalize(form)
    row = PricingVersion(id=uuid4().hex, **value, active=1)
    for old in db.scalars(select(PricingVersion).where(PricingVersion.endpoint == row.endpoint,
            PricingVersion.service == row.service, PricingVersion.model_id == row.model_id,
            PricingVersion.profile_id == row.profile_id, PricingVersion.active == 1)):
        old.active = 0
    db.add(row); db.flush()
    return public_version(row)


def freeze(db, call, binding=None, *, select_current=True):
    if db.get(CallPriceAssessment, call.id):
        return
    binding = binding or {}
    profile = binding.get('service_profile_id') or binding.get('profile_id')
    candidates = list(db.scalars(select(PricingVersion).where(PricingVersion.endpoint == call.endpoint,
        PricingVersion.service == ('chat' if call.service == 'chat_stream' else call.service),
        PricingVersion.model_id == call.model_id, PricingVersion.active == 1).order_by(PricingVersion.created_at.desc(), PricingVersion.id)))
    selected = (next((v for v in candidates if profile and v.profile_id == profile), None) or next((v for v in candidates if v.profile_id is None), None)) if select_current else None
    db.add(CallPriceAssessment(call_id=call.id, tariff_id=selected.id if selected else None,
        tariff_snapshot=public_version(selected) if selected else None, currency=selected.currency if selected else None,
        status='awaiting_usage' if selected else 'missing_price', revision=1, unknown_units=[], details={}))
    db.flush()


def unknown_numeric_units(raw):
    from .call_ledger import UNITS, GROUPS
    result = set()
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        if key in GROUPS and isinstance(value, dict):
            result.update(unknown_numeric_units(value))
        elif key not in UNITS and type(value) in (int, float):
            result.add(key if isinstance(key, str) and re.fullmatch('[a-zA-Z_]{1,60}', key) else 'unrecognized_numeric_unit')
        elif isinstance(value, dict) and any(type(v) in (int, float, dict) for v in value.values()):
            result.add('unrecognized_nested_usage')
    return result


def observe_usage(db, call, raw):
    row = db.get(CallPriceAssessment, call.id)
    if row is None:
        freeze(db, call, select_current=False); row = db.get(CallPriceAssessment, call.id)
    unknown = sorted(set(row.unknown_units or []) | unknown_numeric_units(raw))
    if row.unknown_units != unknown:
        row.unknown_units, row.revision, row.updated_at = unknown, row.revision + 1, utcnow()


def assess(db, call):
    row = db.get(CallPriceAssessment, call.id)
    if row is None:
        freeze(db, call, select_current=False); row = db.get(CallPriceAssessment, call.id)
    if row.status == 'user_confirmed':
        return
    tariff = row.tariff_snapshot
    status, amount, details = 'missing_price', None, {}
    if call.synthetic:
        status = 'synthetic'
    elif tariff:
        counts = call.actual_usage or call.estimated_usage or {}
        amount, missing, covered, entries = Decimal('0'), [], set(), []
        for component in tariff['components']:
            unit = component['unit']; needed = {unit, *component['subtract_units']}
            covered.update(needed)
            if not needed.issubset(counts):
                missing.extend(sorted(needed - set(counts))); continue
            quantity = Decimal(str(counts[unit])) - sum((Decimal(str(counts[u])) for u in component['subtract_units']), Decimal('0'))
            if quantity < 0:
                missing.append('inconsistent:' + unit); continue
            charge = quantity * Decimal(component['rate']) / Decimal(component['per'])
            entries.append({'unit': unit, 'quantity': str(quantity), 'amount': str(charge)})
            amount += charge
        uncovered = sorted(unit for unit, count in counts.items() if count > 0 and unit not in covered | set(tariff['informational_units']))
        complete = bool(tariff['complete_contract'] and call.actual_usage and not missing and not uncovered and not row.unknown_units and call.state == 'completed' and amount <= 10**9)
        status = 'complete_estimate' if complete else 'partial_estimate' if entries else 'awaiting_usage'
        details = {'missing_units': sorted(set(missing)), 'unpriced_units': uncovered, 'components': entries,
            'usage_source': 'actual' if call.actual_usage else 'estimated' if call.estimated_usage else 'unknown',
            'provider_bill_confirmed': False}
        amount = str(amount) if entries else None
    values = (status, amount, details)
    if (row.status, row.amount, row.details) != values:
        row.status, row.amount, row.details = values
        row.revision += 1; row.updated_at = utcnow()


def public_assessment(db, call_id):
    row = db.get(CallPriceAssessment, call_id)
    if not row:
        return {'status': 'missing_price', 'amount': None, 'revision': 0, 'currency': None}
    return {'status': row.status, 'amount': row.amount, 'currency': row.currency, 'revision': row.revision,
        'tariff_id': row.tariff_id, 'tariff_name': (row.tariff_snapshot or {}).get('name'),
        'unknown_units': list(row.unknown_units or []), 'details': deepcopy(row.details),
        'automatic_settlement_eligible': row.status == 'complete_estimate', 'provider_bill_confirmed': False}


def budget_charge(db, call_id, currency):
    row = db.get(CallPriceAssessment, call_id)
    if row and row.currency == currency and row.amount is not None and row.status in ('complete_estimate', 'user_confirmed'):
        return {'amount': row.amount, 'source': 'estimated_tariff' if row.status == 'complete_estimate' else 'user_confirmed',
                'reference': row.tariff_id if row.status == 'complete_estimate' else row.details.get('reference')}
    return None


def confirm_call(db, call, *, expected_revision, amount, currency, reference):
    from .resource_models import ResourceReservation, ResourceBudget
    from .resource_budget import _money
    row = db.get(CallPriceAssessment, call.id)
    if row is None and expected_revision == 0:
        freeze(db, call, select_current=False)
        row = db.get(CallPriceAssessment, call.id)
    elif not row or row.revision != expected_revision:
        raise PricingError('费用记录已变化，请重新读取后核对')
    if call.synthetic or call.state == 'started':
        raise PricingError('合成调用或尚在运行的调用不能核对实际费用')
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        raise PricingError('币种格式无效')
    amount, reference = str(_number(amount)), _text(reference, '账单核对依据')
    reservation = db.scalar(select(ResourceReservation).where(ResourceReservation.call_id == call.id))
    if reservation:
        budget = db.get(ResourceBudget, reservation.budget_id)
        if currency != budget.currency:
            raise PricingError('账单与预算币种不同，不能隐式换算或释放预留')
        if reservation.state == 'in_flight':
            raise PricingError('请求仍在收尾，请稍后再核对')
    row.status, row.amount, row.currency = 'user_confirmed', amount, currency
    row.revision += 1; row.updated_at = utcnow(); row.details = {**row.details, 'reference': reference, 'provider_bill_confirmed': False}
    db.add(CallChargeConfirmation(id=uuid4().hex, call_id=call.id, revision=row.revision,
        amount=amount, currency=currency, reference=reference))
    if reservation:
        settle_reservation(db, reservation, amount_micros=_money(amount), source='user_confirmed', reference=reference)
    db.flush()
    return public_assessment(db, call.id)


def settle_reservation(db, reservation, *, amount_micros, source, reference=None):
    from .resource_models import ResourceBudget
    budget = db.get(ResourceBudget, reservation.budget_id)
    latest = db.scalar(select(BudgetChargeEvent).where(BudgetChargeEvent.reservation_id == reservation.id)
        .order_by(BudgetChargeEvent.revision.desc()).limit(1))
    if latest and (latest.amount_micros, latest.source, latest.reference) == (amount_micros, source, reference):
        return
    previous = reservation.settled_micros
    if reservation.state != 'settled':
        budget.reserved_micros -= reservation.reserved_micros
    budget.spent_micros += amount_micros - (previous or 0)
    reservation.settled_micros, reservation.state = amount_micros, 'settled'
    budget.revision += 1
    db.add(BudgetChargeEvent(id=uuid4().hex, reservation_id=reservation.id, call_id=reservation.call_id,
        revision=(latest.revision + 1 if latest else 1), amount_micros=amount_micros, previous_micros=previous,
        currency=budget.currency, source=source, reference=reference))
