"""Isolated catalog, protected credentials and conversation-local selections."""
import json
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import model_profiles as store
from app.api import model_profiles as api
from app.config import settings
from app.credential_protection import CredentialProtectionError, protect, unprotect
from app.main import app
from app.models import Conversation, ConversationOptions, DeletedConversation, ModelCredential, ModelProfile
from app.timeutil import utcnow

client = TestClient(app)
SYNTHETIC_KEY = "Synthetic-CaSe-Sensitive-Credential-123"


@pytest.fixture(autouse=True)
def configured_legacy(monkeypatch):
    monkeypatch.setattr(settings, "llm_demo_mode", False)
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_model", "legacy-Model")
    monkeypatch.setattr(settings, "llm_base_url", "https://legacy.invalid/v1")
    monkeypatch.setattr(settings, "reasoning_effort", "")
    monkeypatch.setattr(settings, "approval_mode", "auto")


def create(**fields):
    response = client.post('/api/model-profiles', json={
        'name': 'Provider A', 'base_url': 'https://Example.invalid/CasePath/v1',
        'model_id': 'Model-CaSe', 'api_key': SYNTHETIC_KEY, **fields,
    })
    assert response.status_code == 200, response.text
    assert SYNTHETIC_KEY not in response.text
    return response.json()


def select(cid, *, revision=0, **fields):
    response = client.put(f'/api/conversation-options/{cid}', json={
        'expected_revision': revision, **fields,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_dpapi_roundtrip_and_tamper(monkeypatch):
    if sys.platform != 'win32':
        pytest.skip('Windows DPAPI integration')
    protected = protect(SYNTHETIC_KEY)
    assert protected.startswith('dpapi:') and SYNTHETIC_KEY not in protected
    assert unprotect(protected) == SYNTHETIC_KEY
    with pytest.raises(CredentialProtectionError):
        unprotect('dpapi:corrupt')
    import app.credential_protection as protection
    monkeypatch.setattr(protection.sys, 'platform', 'unsupported-platform')
    with pytest.raises(CredentialProtectionError):
        protect(SYNTHETIC_KEY)


def test_catalog_case_and_public_fields_do_not_reveal_credentials(db):
    first = create()
    second = create(name='Provider B', model_id='model-case', base_url='https://Other.invalid/v1')
    assert first['id'] != second['id']
    assert first['base_url'] == 'https://example.invalid/CasePath/v1'
    assert first['model_id'] == 'Model-CaSe' and second['model_id'] == 'model-case'
    assert first['api_key_set'] is True
    assert set(first['capabilities'].values()) == {'unknown'}
    listing = client.get('/api/model-profiles')
    assert listing.status_code == 200
    assert listing.json()['default_profile_id'] == store.LEGACY_PROFILE_ID
    assert len(listing.json()['profiles']) == 4
    serialized = listing.text
    assert SYNTHETIC_KEY not in serialized and 'credential_ref' not in serialized
    credentials = db.query(ModelCredential).all()
    assert len(credentials) == 2
    assert all(SYNTHETIC_KEY not in credential.protected_value for credential in credentials)
    assert all(credential.protected_value.startswith('dpapi:') for credential in credentials)


def test_edit_configuration_is_versioned_and_keeps_old_run_binding(db):
    first = create()
    select('c_A', model_profile_id=first['id'], reasoning_effort='low')
    original = store.capture_run_config(db, 'c_A')
    db.commit()
    assert SYNTHETIC_KEY not in json.dumps(original)
    assert store.resolve_model_config(original)['api_key'] == SYNTHETIC_KEY
    updated = client.put('/api/model-profiles/' + first['id'], json={
        'expected_version': 1, 'model_id': 'Next-Model', 'api_key': 'Second-Synthetic-Key',
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()['version'] == 2
    assert store.resolve_model_config(original)['model_id'] == 'Model-CaSe'
    assert store.resolve_model_config(original)['api_key'] == SYNTHETIC_KEY
    db.expire_all()
    fresh = store.capture_run_config(db, 'c_A')
    db.commit()
    assert store.resolve_model_config(fresh)['api_key'] == 'Second-Synthetic-Key'
    assert fresh['model_id'] == 'Next-Model' and fresh['reasoning_effort'] == 'low'
    stale = client.put('/api/model-profiles/' + first['id'], json={'expected_version': 1, 'name': 'Stale'})
    assert stale.status_code == 409 and 'current' in stale.json()['detail']
    assert 'Second-Synthetic-Key' not in stale.text
    assert 'credential_ref' not in json.dumps(store.public_runtime_config(original))


def test_endpoint_origin_change_requires_explicit_key_decision():
    profile = create()
    missing = client.put('/api/model-profiles/' + profile['id'], json={
        'expected_version': 1, 'base_url': 'https://new.invalid/v1',
    })
    assert missing.status_code == 422
    explicit = client.put('/api/model-profiles/' + profile['id'], json={
        'expected_version': 1, 'base_url': 'https://new.invalid/v1', 'api_key': '',
    })
    assert explicit.status_code == 200 and explicit.json()['api_key_set'] is False


def test_conversation_choices_cas_and_default_do_not_create_visible_chats(db):
    first, second = create(name='First'), create(name='Second')
    default = client.put('/api/model-profiles/default', json={'model_profile_id': first['id']})
    assert default.status_code == 200
    assert client.get('/api/conversation-options/c_new').json()['effective_model']['id'] == first['id']
    a = select('c_A', model_profile_id=first['id'], approval_mode='always', reasoning_effort='high')
    b = select('c_B', model_profile_id=second['id'], approval_mode='full_access')
    assert a['revision'] == 1 and b['revision'] == 1
    assert db.query(Conversation).count() == 0, 'settings alone do not materialize empty sidebar entries'
    assert client.get('/api/conversations/page').json()['total'] == 0
    client.put('/api/model-profiles/default', json={'model_profile_id': second['id']})
    assert client.get('/api/conversation-options/c_A').json()['effective_model']['id'] == first['id']
    assert client.get('/api/conversation-options/c_B').json()['approval_mode'] == 'full_access'
    assert client.get('/api/conversation-options/c_new').json()['effective_model']['id'] == second['id']
    conflict = client.put('/api/conversation-options/c_A', json={'expected_revision': 0, 'approval_mode': 'auto'})
    assert conflict.status_code == 409 and conflict.json()['detail']['current']['approval_mode'] == 'always'
    select('c_A', revision=1, model_profile_id=second['id'])
    assert client.get('/api/conversation-options/c_A').json()['reasoning_effort'] is None
    assert settings.approval_mode == 'auto' and settings.llm_model == 'legacy-Model'


def test_default_inherited_choice_is_frozen_on_first_run(db):
    first, second = create(name='First'), create(name='Second')
    client.put('/api/model-profiles/default', json={'model_profile_id': first['id']})
    snapshot = store.capture_run_config(db, 'c_started')
    db.commit()
    assert snapshot['profile_id'] == first['id']
    client.put('/api/model-profiles/default', json={'model_profile_id': second['id']})
    assert client.get('/api/conversation-options/c_started').json()['model_profile_id'] == first['id']


def test_disable_delete_and_revoke_do_not_silently_fallback(db):
    profile = create()
    select('c_chosen', model_profile_id=profile['id'])
    run = store.capture_run_config(db, 'c_chosen')
    db.commit()
    disabled = client.put('/api/model-profiles/' + profile['id'], json={'expected_version': 1, 'enabled': False})
    assert disabled.status_code == 200
    options = client.get('/api/conversation-options/c_chosen').json()
    assert options['effective_model'] is None and options['model_profile_id'] == profile['id'] and options['model_error']
    assert client.put('/api/model-profiles/default', json={'model_profile_id': profile['id']}).status_code == 409
    with pytest.raises(store.ModelConfigurationError):
        db.expire_all()
        store.capture_run_config(db, 'c_chosen')
    db.rollback()
    assert store.resolve_model_config(run)['api_key'] == SYNTHETIC_KEY, 'ordinary disable does not mutate active bindings'
    assert client.delete('/api/model-profiles/' + profile['id'], params={'expected_version': 1}).status_code == 409
    assert client.post('/api/model-profiles/' + profile['id'] + '/revoke').status_code == 200
    with pytest.raises(store.ModelConfigurationError, match='撤销'):
        store.resolve_model_config(run)
    deleted = client.delete('/api/model-profiles/' + profile['id'], params={'expected_version': 3})
    assert deleted.status_code == 200
    assert profile['id'] not in [item['id'] for item in client.get('/api/model-profiles').json()['profiles']]
    assert client.get('/api/conversation-options/c_chosen').json()['model_error']


def test_deleted_profile_credentials_can_still_be_explicitly_revoked(db):
    profile = create()
    select('c_deleted_model', model_profile_id=profile['id'])
    binding = store.capture_run_config(db, 'c_deleted_model')
    db.commit()
    deleted = client.delete('/api/model-profiles/' + profile['id'], params={'expected_version': 1})
    assert deleted.status_code == 200
    assert store.resolve_model_config(binding)['api_key'] == SYNTHETIC_KEY
    assert client.put('/api/model-profiles/' + profile['id'], json={
        'expected_version': 2, 'enabled': True,
    }).status_code == 404, 'deleted catalog entries must not become editable'
    revoked = client.post('/api/model-profiles/' + profile['id'] + '/revoke')
    assert revoked.status_code == 200
    assert SYNTHETIC_KEY not in revoked.text
    with pytest.raises(store.ModelConfigurationError, match='撤销'):
        store.resolve_model_config(binding)
    assert client.post('/api/model-profiles/missing-profile/revoke').status_code == 404
    assert client.post('/api/model-profiles/legacy-default/revoke').status_code == 422


def test_archived_deleted_and_invalid_conversation_options(db):
    db.add_all([Conversation(id='archived', archived_at=utcnow()), DeletedConversation(id='gone')])
    db.commit()
    assert client.get('/api/conversation-options/archived').status_code == 200
    assert client.put('/api/conversation-options/archived', json={'expected_revision': 0, 'approval_mode': 'auto'}).status_code == 409
    for method in (client.get, client.put):
        args = {'json': {'expected_revision': 0, 'approval_mode': 'auto'}} if method == client.put else {}
        assert method('/api/conversation-options/gone', **args).status_code == 410
    assert client.put('/api/conversation-options/invalid.id', json={'expected_revision': 0}).status_code == 422
    assert db.get(ConversationOptions, 'gone') is None


def test_legacy_capture_preserves_configuration_after_settings_change(db, monkeypatch):
    monkeypatch.setattr(settings, 'llm_api_key', SYNTHETIC_KEY)
    monkeypatch.setattr(settings, 'approval_mode', 'read_only')
    snapshot = store.capture_run_config(db, 'legacy_conversation')
    db.commit()
    monkeypatch.setattr(settings, 'llm_api_key', 'Different-Synthetic-Key')
    monkeypatch.setattr(settings, 'llm_model', 'Changed-Model')
    resolved = store.resolve_model_config(snapshot)
    assert snapshot['approval_mode'] == 'always'
    assert snapshot['model_id'] == 'legacy-Model' and resolved['api_key'] == SYNTHETIC_KEY
    assert store.current_policy(db, 'missing', snapshot)['approval_mode'] == 'always'
    assert SYNTHETIC_KEY not in json.dumps(snapshot)


def test_current_policy_refreshes_and_does_not_read_other_conversations(db):
    select('c_A', approval_mode='auto')
    select('c_B', approval_mode='full_access')
    assert store.current_policy(db, 'c_A')['approval_mode'] == 'auto'
    select('c_A', revision=1, approval_mode='always')
    policy = store.current_policy(db, 'c_A')
    assert policy == {'approval_mode': 'always', 'policy_revision': 2}
    assert store.current_policy(db, 'c_B')['approval_mode'] == 'full_access'


@pytest.mark.parametrize('payload', [
    {'api_key': {'secret': SYNTHETIC_KEY}}, {'name': {'secret': SYNTHETIC_KEY}},
    {'extra_field': SYNTHETIC_KEY}, {'api_key': SYNTHETIC_KEY + '\nBAD'},
])
def test_malformed_requests_do_not_echo_submitted_secret(payload):
    response = client.post('/api/model-profiles', json={
        'name': 'Malformed', 'base_url': 'https://example.invalid/v1', 'model_id': 'M', **payload,
    })
    assert response.status_code == 422
    assert SYNTHETIC_KEY not in response.text
    assert '"input":' not in response.text


def test_unsaved_discovery_never_uses_global_key(monkeypatch):
    monkeypatch.setattr(settings, 'llm_api_key', 'DO-NOT-USE-LEGACY')
    observed = []
    monkeypatch.setattr(api, 'discover_models', lambda base, key: observed.append((base, key)) or [{'id': 'New-Model'}])
    response = client.post('/api/model-profiles/discover', json={'base_url': 'https://NEW.invalid/V1'})
    assert response.json()['models'] == [{'id': 'New-Model'}]
    assert observed == [('https://new.invalid/V1', '')]


def test_saved_discovery_and_probe_have_profile_credentials_and_honest_capabilities(monkeypatch):
    profile = create()
    observations = []
    monkeypatch.setattr(api, 'discover_models', lambda base, key, **kwargs: observations.append((base, key)) or [{'id': 'Model-CaSe'}])
    monkeypatch.setattr(api, 'probe_chat', lambda base, key, model, effort, **kwargs: observations.append((base, key, model, effort)))
    assert client.post('/api/model-profiles/' + profile['id'] + '/models').json()['ok']
    response = client.post('/api/model-profiles/' + profile['id'] + '/test').json()
    assert response['ok'] and response['capabilities']['text'] == 'supported'
    assert response['capabilities']['tools'] == response['capabilities']['vision'] == 'unknown'
    assert observations[0][1] == observations[1][1] == SYNTHETIC_KEY
    assert SYNTHETIC_KEY not in json.dumps(response)
    profiles = client.get('/api/model-profiles').json()['profiles']
    assert next(item for item in profiles if item['id'] == profile['id'])['capabilities']['text'] == 'supported'


def test_basic_probe_does_not_erase_independently_verified_features(db, monkeypatch):
    profile = create()
    row = db.get(ModelProfile, profile['id'])
    row.capabilities = {**row.capabilities, 'tools': 'supported', 'vision': 'unsupported'}
    db.commit()
    monkeypatch.setattr(api, 'probe_chat', lambda *args, **kwargs: None)
    result = client.post(f"/api/model-profiles/{profile['id']}/test").json()
    assert result['ok'] and result['capabilities']['tools'] == 'supported' and result['capabilities']['vision'] == 'unsupported'


def test_runtime_config_columns_migrate_existing_rows(monkeypatch):
    from app import db as database
    legacy = create_engine('sqlite:///:memory:')
    with legacy.begin() as connection:
        connection.execute(text('CREATE TABLE qa_records (id TEXT PRIMARY KEY, question TEXT)'))
        connection.execute(text("INSERT INTO qa_records (id,question) VALUES ('old','preserve')"))
        connection.execute(text('CREATE TABLE tasks (id TEXT PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE foreground_runs (id TEXT PRIMARY KEY,updated_at DATETIME)'))
    monkeypatch.setattr(database, 'engine', legacy)
    database._lightweight_migrations()
    database._lightweight_migrations()
    with legacy.connect() as connection:
        for table in ('qa_records', 'tasks', 'foreground_runs'):
            assert 'runtime_config' in {row[1] for row in connection.execute(text(f'PRAGMA table_info({table})'))}
        assert connection.execute(text("SELECT question FROM qa_records WHERE id='old'")).scalar() == 'preserve'
