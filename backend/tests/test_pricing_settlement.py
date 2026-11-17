from copy import deepcopy
from decimal import Decimal
import pytest
from sqlalchemy import select
from app import call_ledger as ledger, pricing, resource_budget
from app.db import SessionLocal
from app.pricing_models import PricingVersion, CallPriceAssessment, CallChargeConfirmation, BudgetChargeEvent
from app.ledger_models import CallRecord
from app.request_resources import request_admission


FORM = {'name': 'Test tariff 1', 'endpoint': 'https://pricing.test/v1/chat/completions', 'service': 'chat',
    'model_id': 'Exact-Model', 'currency': 'USD', 'complete_contract': True, 'source_reference': 'synthetic-price-fixture',
    'components': [{'unit': 'prompt_tokens', 'rate': '2', 'per': '1000'}, {'unit': 'completion_tokens', 'rate': '4', 'per': '1000'}]}


def tariff(**changes):
    with SessionLocal.begin() as db:
        return pricing.create_version(db, {**deepcopy(FORM), **changes})


def begin(**changes):
    return ledger.begin_call(provider='pricing.test', service='chat', endpoint=FORM['endpoint'], model_id='Exact-Model', **changes)


def test_price_freezes_at_start_and_distinguishes_estimate_from_invoice():
    first = tariff()
    cid = begin()
    tariff(name='second version', components=[{'unit': 'prompt_tokens', 'rate': '50', 'per': '1000'}])
    result = ledger.finish_call(cid, usage={'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150})
    assert result['pricing']['tariff_id'] == first['id']
    assert result['pricing']['status'] == 'complete_estimate'
    assert Decimal(result['pricing']['amount']) == Decimal('.4')
    assert result['pricing']['provider_bill_confirmed'] is False
    assert result['amount'] is None  # The older partial-price field is not relabelled actual.


@pytest.mark.parametrize('usage,expected', [
    ({'prompt_tokens': 100}, 'partial_estimate'),
    ({'prompt_tokens': 100, 'completion_tokens': 50, 'tool_search': 1}, 'partial_estimate'),
    ({'prompt_tokens': 100, 'completion_tokens': 50, 'new_charge_units': 3}, 'partial_estimate'),
    ({'prompt_tokens': 100, 'completion_tokens': 50, 'new_billing': {'units': 3}}, 'partial_estimate'),
    ({'prompt_tokens': 0, 'completion_tokens': 0}, 'complete_estimate'),
])
def test_missing_unpriced_unknown_dimensions_and_real_zero(usage, expected):
    tariff()
    result = ledger.finish_call(begin(), usage=usage)
    assert result['pricing']['status'] == expected


def test_synthetic_estimated_usage_and_failed_requests_never_automatic_total():
    tariff()
    usage = {'prompt_tokens': 100, 'completion_tokens': 50}
    fake = ledger.finish_call(begin(synthetic=True), usage=usage)
    estimated = ledger.finish_call(begin(), usage=usage, usage_source='estimated')
    failed = ledger.finish_call(begin(), usage=usage, state='failed')
    assert fake['pricing']['status'] == 'synthetic'
    assert all(result['pricing']['status'] != 'complete_estimate' for result in (fake, estimated, failed))


def test_complete_estimate_settles_and_user_reconciliation_adjusts_without_double_count():
    tariff()
    bid = resource_budget.create_budget(owner_run_id='pricing-run', amount_limit='3', request_estimate='1', price_version='test')
    with request_admission(provider='pricing.test', endpoint=FORM['endpoint'], runtime_config={'resource_budget_id': bid}) as admission:
        cid = begin(run_id='pricing-run')
        admission.bind_call(cid)
        result = ledger.finish_call(cid, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    snapshot = resource_budget.budget_snapshot(bid)
    assert snapshot['reserved_amount'] == '0' and snapshot['estimated_spent_amount'] == '0.4'
    assert snapshot['confirmed_spent_amount'] == '0'
    with SessionLocal.begin() as db:
        assessment = pricing.confirm_call(db, db.get(CallRecord, cid), expected_revision=result['pricing']['revision'], amount='.3', currency='USD', reference='invoice-line-1')
    snapshot = resource_budget.budget_snapshot(bid)
    assert snapshot['spent_amount'] == '0.3'
    assert snapshot['confirmed_spent_amount'] == '0.3' and snapshot['estimated_spent_amount'] == '0'
    with SessionLocal.begin() as db:
        with pytest.raises(pricing.PricingError):
            pricing.confirm_call(db, db.get(CallRecord, cid), expected_revision=result['pricing']['revision'], amount='.3', currency='USD', reference='duplicate')
        assert len(list(db.scalars(select(BudgetChargeEvent)))) == 2
        assert len(list(db.scalars(select(CallChargeConfirmation)))) == 1
    assert assessment['status'] == 'user_confirmed'


def test_unpriced_usage_and_currency_mismatch_hold_reservation():
    tariff(currency='CNY')
    bid = resource_budget.create_budget(owner_run_id='pricing-run', amount_limit='3', request_estimate='1', price_version='test')
    with request_admission(provider='pricing.test', endpoint=FORM['endpoint'], runtime_config={'resource_budget_id': bid}) as admission:
        cid = begin(run_id='pricing-run'); admission.bind_call(cid)
        result = ledger.finish_call(cid, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    assert resource_budget.budget_snapshot(bid)['reserved_amount'] == '1'
    with SessionLocal.begin() as db:
        with pytest.raises(pricing.PricingError):
            pricing.confirm_call(db, db.get(CallRecord, cid), expected_revision=result['pricing']['revision'], amount='.3', currency='CNY', reference='invoice')
    assert resource_budget.budget_snapshot(bid)['reserved_amount'] == '1'


def test_reject_double_charge_alias_and_cache_overlap():
    for components in [
        [{'unit': 'prompt_tokens', 'rate': '1'}, {'unit': 'input_tokens', 'rate': '1'}],
        [{'unit': 'prompt_tokens', 'rate': '1'}, {'unit': 'prompt_cache_hit_tokens', 'rate': '1'}],
        [{'unit': 'total_tokens', 'rate': '1'}],
    ]:
        with pytest.raises(pricing.PricingError):
            tariff(components=components)


def test_cache_subtraction_and_version_deactivation():
    tariff(components=[{'unit': 'prompt_tokens', 'subtract_units': ['prompt_cache_hit_tokens'], 'rate': '2', 'per': '1000'},
        {'unit': 'prompt_cache_hit_tokens', 'rate': '1', 'per': '1000'}, {'unit': 'completion_tokens', 'rate': '4', 'per': '1000'}])
    result = ledger.finish_call(begin(), usage={'prompt_tokens': 100, 'prompt_cache_hit_tokens': 40, 'completion_tokens': 50})
    assert result['pricing']['status'] == 'complete_estimate' and Decimal(result['pricing']['amount']) == Decimal('.36')
    with SessionLocal.begin() as db:
        for row in db.scalars(select(PricingVersion)): row.active = 0
    assert ledger.finish_call(begin(), usage={'prompt_tokens': 100})['pricing']['status'] == 'missing_price'


def test_adding_price_later_does_not_reprice_old_unknown_call():
    cid = begin()
    tariff()
    assert ledger.finish_call(cid, usage={'prompt_tokens': 100, 'completion_tokens': 50})['pricing']['status'] == 'missing_price'
