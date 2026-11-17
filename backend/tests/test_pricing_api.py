import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import Conversation, DeletedConversation
from app.runtime_models import RuntimeRun
from app import call_ledger
from app.db import SessionLocal
from app.runtime_context import bind_runtime_config


def setup_call(db):
    db.add_all([Conversation(id='price-a', title='A'), Conversation(id='price-b', title='B'), RuntimeRun(id='price-run', kind='foreground', conversation_id='price-a')]); db.commit()
    identity = call_ledger.begin_call(provider='price.test', service='chat', endpoint='https://price.test/chat', model_id='Exact-Model', run_id='price-run')
    result = call_ledger.finish_call(identity)
    return identity, result['pricing']['revision']


def test_only_own_conversation_can_confirm_and_stale_revision_rejected(db):
    call, rev = setup_call(db)
    client = TestClient(app)
    body = {'expected_revision': rev, 'amount': '0', 'currency': 'USD', 'reference': 'zero-charge-invoice'}
    wrong = f'/api/pricing/conversations/price-b/calls/{call}'
    assert client.get(wrong).status_code == 404
    assert client.post(wrong + '/confirm', json=body).status_code == 404
    target = f'/api/pricing/conversations/price-a/calls/{call}'
    response = client.post(target + '/confirm', json=body)
    assert response.status_code == 200 and response.json()['status'] == 'user_confirmed'
    assert client.post(target + '/confirm', json=body).status_code == 409
    assert len(client.get(target).json()['confirmations']) == 1
    assert len(client.get('/api/pricing/conversations/price-a/calls').json()['items']) == 1
    assert client.get('/api/pricing/conversations/price-b/calls').json()['items'] == []


def test_deleted_conversation_cannot_read_or_mutate_charge(db):
    call, rev = setup_call(db)
    db.add(DeletedConversation(id='price-a')); db.commit()
    client = TestClient(app)
    assert client.get('/api/pricing/conversations/price-a/calls').status_code == 410
    assert client.post(f'/api/pricing/conversations/price-a/calls/{call}/confirm', json={
        'expected_revision': rev, 'amount': '1', 'currency': 'USD', 'reference': 'invoice'}).status_code == 410


def test_model_case_and_profile_scope_are_exact(db):
    from app import pricing
    form = {'name': 'Profile only', 'endpoint': 'https://price.test/chat', 'service': 'chat', 'model_id': 'Exact-Model', 'profile_id': 'profile-a',
        'currency': 'USD', 'components': [{'unit': 'input_tokens', 'rate': '1', 'per': '1000'}], 'complete_contract': True, 'source_reference': 'test'}
    pricing.create_version(db, form); db.commit()
    def begin(profile, model):
        with bind_runtime_config({'profile_id': profile}):
            cid = call_ledger.begin_call(provider='price.test', service='chat_stream', endpoint='https://price.test/chat', model_id=model)
        return call_ledger.finish_call(cid, usage={'input_tokens': 5})['pricing']['status']
    assert begin('profile-a', 'Exact-Model') == 'complete_estimate'
    assert begin('profile-b', 'Exact-Model') == 'missing_price'
    assert begin('profile-a', 'exact-model') == 'missing_price'


def test_price_api_rejects_secrets_and_negative_or_nonfinite_rate():
    client = TestClient(app)
    form = {'name': 'Fixture', 'endpoint': 'https://price.test/chat', 'service': 'chat', 'model_id': 'M',
        'currency': 'USD', 'components': [{'unit': 'input_tokens', 'rate': 'NaN'}], 'complete_contract': True, 'source_reference': 'test'}
    assert client.post('/api/pricing/versions', json=form).status_code == 422
    form['components'][0]['rate'] = '-1'
    assert client.post('/api/pricing/versions', json=form).status_code == 422
    form['components'][0]['rate'] = '1'
    form['source_reference'] = 'test-key-c326b69a91bd0909'
    assert client.post('/api/pricing/versions', json=form).status_code == 422
