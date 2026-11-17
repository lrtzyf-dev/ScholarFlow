import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from app import mcp_user_oauth as oauth
from app.db import SessionLocal
from app.mcp_auth_models import McpUserGrant

CFG = {'server_id': 'test-user-oauth', 'transport': 'http', 'url': 'https://mcp.example/mcp',
       'oauth': {'flow': 'authorization_code'}}


@pytest.fixture
def provider(monkeypatch):
    original = httpx2.AsyncClient
    state = {'requests': [], 's256': True, 'refreshes': 0}

    async def respond(request):
        state['requests'].append((request.method, str(request.url)))
        path = request.url.path
        if request.url.host == 'mcp.example' and path == '/mcp':
            return httpx2.Response(401, headers={'WWW-Authenticate': 'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp"'})
        if request.url.host == 'mcp.example' and '.well-known' in path:
            return httpx2.Response(200, json={'resource': CFG['url'], 'authorization_servers': ['https://auth.example']})
        if '.well-known' in path:
            metadata = {'issuer': 'https://auth.example', 'authorization_endpoint': 'https://auth.example/authorize',
                'token_endpoint': 'https://auth.example/token', 'registration_endpoint': 'https://auth.example/register',
                'revocation_endpoint': 'https://auth.example/revoke', 'response_types_supported': ['code'],
                'grant_types_supported': ['authorization_code', 'refresh_token'], 'token_endpoint_auth_methods_supported': ['none']}
            if state['s256']:
                metadata['code_challenge_methods_supported'] = ['S256']
            return httpx2.Response(200, json=metadata)
        if path == '/register':
            data = json.loads(request.content)
            return httpx2.Response(201, json={**data, 'client_id': 'public-desktop', 'token_endpoint_auth_method': 'none'})
        if path == '/token':
            data = parse_qs(request.content.decode())
            if data['grant_type'] == ['authorization_code']:
                assert data['resource'] == [CFG['url']]
                assert 43 <= len(data['code_verifier'][0]) <= 128
                state['verifier'] = data['code_verifier'][0]
                return httpx2.Response(200, json={'access_token': 'secret-access-1', 'refresh_token': 'secret-refresh-1', 'token_type': 'Bearer', 'expires_in': 600})
            state['refreshes'] += 1
            if state.get('entered'):
                state['entered'].set()
                await state['release'].wait()
            if state.get('invalid_grant'):
                return httpx2.Response(400, json={'error': 'invalid_grant', 'secret': 'secret-refresh-1'})
            return httpx2.Response(200, json={'access_token': 'secret-access-2', 'refresh_token': 'secret-refresh-2', 'token_type': 'Bearer', 'expires_in': 600})
        if path == '/revoke':
            return httpx2.Response(200)
        raise AssertionError((request.method, str(request.url)))

    monkeypatch.setattr(oauth.httpx2, 'AsyncClient', lambda **kwargs: original(transport=httpx2.MockTransport(respond), **kwargs))
    return state


async def login(provider, *, wrong_state_first=False):
    current = await oauth.start(CFG)
    assert current['state'] == 'authorizing', current
    query = parse_qs(urlsplit(current['authorization_url']).query)
    assert query['code_challenge_method'] == ['S256']
    callback = query['redirect_uri'][0]
    async with httpx.AsyncClient() as local:
        if wrong_state_first:
            rejected = await local.get(callback, params={'code': 'fake', 'state': 'wrong'})
            assert rejected.status_code == 400
            assert oauth.status(CFG)['state'] == 'authorizing'
        response = await local.get(callback, params={'code': 'test-code', 'state': query['state'][0], 'iss': 'https://auth.example'})
        assert response.status_code == 200
    await oauth._sessions[CFG['server_id']]['task']
    assert oauth.status(CFG)['state'] == 'authorized', oauth.status(CFG)
    return query


def expire():
    with oauth.write() as db:
        row = db.get(McpUserGrant, CFG['server_id'])
        row.expires_at = time.time() - 1


@pytest.mark.asyncio
async def test_discovery_pkce_state_callback_persistence(provider):
    query = await login(provider, wrong_state_first=True)
    import base64, hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(provider['verifier'].encode()).digest()).decode().rstrip('=')
    assert query['code_challenge'] == [expected]
    with SessionLocal() as db:
        row = db.get(McpUserGrant, CFG['server_id'])
        assert row.protected.startswith('dpapi:') and 'secret-access' not in row.protected and 'test-code' not in row.protected
    oauth._sessions.clear()  # A restart loses only interactive state.
    assert await oauth.access_token(CFG) == 'secret-access-1'
    assert not any('/authorize' in url for _, url in provider['requests'])


@pytest.mark.asyncio
async def test_missing_pkce_metadata_fails_without_browser_or_token(provider):
    provider['s256'] = False
    result = await oauth.start(CFG)
    await oauth._sessions[CFG['server_id']]['task']
    assert oauth.status(CFG)['state'] == 'reauthorize'
    assert not result.get('authorization_url')
    assert not any('/token' in url for _, url in provider['requests'])


@pytest.mark.asyncio
async def test_concurrent_refresh_rotates_only_once(provider):
    await login(provider)
    expire()
    result = await asyncio.gather(*(oauth.access_token(CFG) for _ in range(12)))
    assert result == ['secret-access-2'] * 12
    assert provider['refreshes'] == 1


@pytest.mark.asyncio
async def test_revoke_fences_late_refresh(provider):
    await login(provider)
    expire()
    provider['entered'], provider['release'] = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(oauth.access_token(CFG))
    await asyncio.wait_for(provider['entered'].wait(), 3)
    result = await oauth.revoke(CFG)
    provider['release'].set()
    with pytest.raises(oauth.UserOAuthError):
        await pending
    assert result['local_revoked'] and result['remote_revoked']
    assert oauth.status(CFG)['state'] == 'revoked'
    with pytest.raises(oauth.UserOAuthError):
        await oauth.access_token(CFG)


@pytest.mark.asyncio
async def test_invalid_grant_requires_login_and_never_exposes_error_body(provider, caplog):
    await login(provider)
    expire()
    provider['invalid_grant'] = True
    with pytest.raises(oauth.UserOAuthError) as error:
        await oauth.access_token(CFG)
    assert 'secret-' not in str(error.value) + caplog.text + json.dumps(oauth.status(CFG))
    assert oauth.status(CFG)['state'] == 'reauthorize'


@pytest.mark.asyncio
async def test_cancel_and_changed_config_reject_late_callback(provider):
    result = await oauth.start(CFG)
    query = parse_qs(urlsplit(result['authorization_url']).query)
    oauth.invalidate(CFG, 'changed')
    async with httpx.AsyncClient() as local:
        response = await local.get(query['redirect_uri'][0], params={'state': query['state'][0], 'code': 'late'})
        assert response.status_code == 400
    await oauth.cancel(CFG, invalidate_grant=False)
    assert oauth.status(CFG)['state'] == 'revoked'
    assert oauth.status({**CFG, 'url': 'https://elsewhere.example/mcp'})['state'] == 'unauthorized'


@pytest.mark.asyncio
async def test_expired_refresh_lease_is_not_replayed(provider):
    await login(provider)
    expire()
    with oauth.write() as db:
        row = db.get(McpUserGrant, CFG['server_id'])
        row.refresh_owner, row.refresh_until = 'crashed-process', time.time() - 1
    with pytest.raises(oauth.UserOAuthError):
        await oauth.access_token(CFG)
    assert provider['refreshes'] == 0
    assert oauth.status(CFG)['state'] == 'reauthorize'


@pytest.mark.asyncio
async def test_auth_checks_every_request_and_never_leaks_cross_origin(provider):
    await login(provider)
    auth = oauth.LiveUserAuth(CFG)
    request = httpx2.Request('GET', CFG['url'])
    flow = auth.async_auth_flow(request)
    signed = await anext(flow)
    assert signed.headers['Authorization'] == 'Bearer secret-access-1'
    await flow.aclose()
    with pytest.raises(oauth.UserOAuthError):
        await anext(auth.async_auth_flow(httpx2.Request('GET', 'https://evil.example')))
    oauth.invalidate(CFG)
    with pytest.raises(oauth.UserOAuthError):
        await anext(auth.async_auth_flow(httpx2.Request('GET', CFG['url'])))


def test_non_ascii_and_delimiter_server_names_have_unambiguous_tool_ids():
    from app.mcp_bridge import _qualified_server_name
    names = ['甲', '乙', 'same__connection', 'same_connection']
    qualified = [_qualified_server_name(name) for name in names]
    assert len(set(qualified)) == len(names)
    assert all('__' not in value for value in qualified)


@pytest.mark.asyncio
async def test_no_remote_revoke_endpoint_reports_local_only(provider):
    await login(provider)
    with oauth.write() as db:
        row = db.get(McpUserGrant, CFG['server_id'])
        bundle = oauth._bundle(row)
        bundle['metadata'].pop('revocation_endpoint', None)
        row.protected = oauth.protect(json.dumps(bundle))
    result = await oauth.revoke(CFG)
    assert result['local_revoked'] and result['remote_revoked'] is False
    assert '未提供' in result['message']
