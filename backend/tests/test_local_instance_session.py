"""Real HTTP/WS middleware handshake, no user profile or model access."""
import asyncio
import importlib.util
from pathlib import Path
from fastapi import FastAPI, WebSocket, Request
from fastapi.testclient import TestClient
import pytest
from app.local_session import LocalInstanceSession, LocalInstanceMiddleware
from app.local_api_guard import LocalAPIGuard

ORIGIN='http://127.0.0.1:8555'


def make_app(session):
    app=FastAPI()
    app.state.calls=0
    app.add_middleware(LocalInstanceMiddleware,session=session)
    app.add_middleware(LocalAPIGuard)
    @app.get('/api/private')
    @app.get('/api/health')
    def private():
        app.state.calls+=1
        return {'ok':True}
    @app.websocket('/api/socket')
    async def socket(ws: WebSocket):
        await ws.accept();await ws.send_text('authenticated');await ws.close()
    return app


def test_one_use_launch_readiness_then_origin_header_reload_and_rotation():
    session=LocalInstanceSession();secret=session.enable(ORIGIN);app=make_app(session)
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',123)) as client:
        assert client.get('/api/private').status_code==401 and app.state.calls==0
        assert client.get('/api/local-session/status').json()=={'required':True,'authenticated':False}
        auth={'Authorization':'Bearer '+secret}
        assert client.get('/api/health',headers=auth).status_code==200
        assert client.get('/api/private',headers=auth).status_code==401
        result=client.post('/api/local-session/bootstrap',headers=auth)
        assert result.status_code==200
        assert 'set-cookie' not in result.headers and not client.cookies
        token = result.json()['token']
        assert len(token) == 64 and secret != token
        assert client.get('/api/private').status_code==401
        headers = {'Authorization': 'ScholarFlow ' + token}
        assert client.get('/api/private', headers=headers).status_code==200
        assert client.get('/api/local-session/status', headers=headers).json()['authenticated']
        with client.websocket_connect(ORIGIN.replace('http:', 'ws:') + '/api/socket', headers=headers) as ws:
            assert ws.receive_text()=='authenticated'
        # Relaunch/other data-dir cookie and old one-use handoff cannot inherit.
        session.enable(ORIGIN)
        assert client.get('/api/private', headers=headers).status_code==401
        assert client.post('/api/local-session/bootstrap',headers=auth).status_code==401


@pytest.mark.parametrize('headers',[{'Origin':'http://localhost:5173'}, {'Origin':'http://127.0.0.1:9999'},
    {'Origin':'null'},{'Host':'localhost:8555'}, {'Origin':'https://attacker.example'}])
def test_origin_alias_or_development_page_cannot_exchange_launch_secret(headers):
    session=LocalInstanceSession();secret=session.enable(ORIGIN);app=make_app(session)
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',123)) as client:
        result=client.post('/api/local-session/bootstrap',headers={**headers,'Authorization':'Bearer '+secret})
        assert result.status_code in (401,403)
        assert app.state.calls==0
        assert client.post('/api/local-session/bootstrap',headers={'Authorization':'Bearer '+secret}).status_code==200


def test_duplicate_headers_and_websocket_without_cookie_do_not_run():
    session=LocalInstanceSession();secret=session.enable(ORIGIN);app=make_app(session)
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',123)) as client:
        assert client.post('/api/local-session/bootstrap',headers=[('Authorization','Bearer '+secret),('Authorization','Bearer '+secret)]).status_code==401
        with pytest.raises(Exception):
            with client.websocket_connect(ORIGIN.replace('http:', 'ws:') + '/api/socket'): pass
        assert app.state.calls==0
        assert client.post('/api/local-session/bootstrap',headers={'Authorization':'Bearer '+secret}).status_code==200
        session.close()
        assert client.get('/api/private').status_code==401


def test_explicit_standalone_server_does_not_invent_desktop_handshake():
    session=LocalInstanceSession();app=make_app(session)
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',123)) as client:
        assert client.get('/api/local-session/status').json()['required'] is False
        assert client.get('/api/private').status_code==200


@pytest.mark.parametrize('url',[None,'https://attacker.example','file:///tmp/index.html','http://127.0.0.1:8555.attacker.example',
    'http://attacker@127.0.0.1:8555','http://127.0.0.1:9999','http://localhost:8555'])
def test_native_picker_is_denied_for_untrusted_current_page(url):
    path=Path(__file__).resolve().parents[2]/'desktop'/'launcher.py'
    spec=importlib.util.spec_from_file_location('test_desktop_bridge',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    class Window:
        def get_current_url(self):return url
        def create_file_dialog(self,*args,**kwargs):raise AssertionError('Must not open native picker')
    bridge=module.DesktopApi(ORIGIN);bridge._window=Window()
    assert bridge.choose_project_folder() is None


def test_native_picker_rechecks_navigation_before_returning_selected_path():
    path=Path(__file__).resolve().parents[2]/'desktop'/'launcher.py'
    spec=importlib.util.spec_from_file_location('test_desktop_bridge_valid',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    class Window:
        url=ORIGIN+'/chat'
        def get_current_url(self):return self.url
        def create_file_dialog(self,*args,**kwargs):
            self.url='https://attacker.example'
            return ['D:/Synthetic/PrivateProject']
    bridge=module.DesktopApi(ORIGIN);bridge._window=Window()
    assert bridge.choose_project_folder() is None


@pytest.mark.parametrize('header', [b'authorization', b'cookie'])
def test_non_ascii_authentication_headers_are_denied_without_exception(header):
    session=LocalInstanceSession();secret=session.enable(ORIGIN);app=make_app(session)
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',123)) as client:
        value=b'Bearer \xff' if header==b'authorization' else b'sf_instance_fixture="\xff"'
        response=client.post('/api/local-session/bootstrap',headers=[(header,value)])
        assert response.status_code==401 and app.state.calls==0
        assert client.post('/api/local-session/bootstrap',headers={'Authorization':'Bearer '+secret}).status_code==200


def test_same_site_cross_port_subresource_has_no_instance_authority():
    session=LocalInstanceSession();secret=session.enable(ORIGIN);app=make_app(session)
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',123)) as client:
        result = client.post('/api/local-session/bootstrap',headers={'Authorization':'Bearer '+secret})
        auth = {'Authorization': 'ScholarFlow ' + result.json()['token']}
        assert client.get('/api/private',headers={**auth,'Sec-Fetch-Site':'same-site'}).status_code==401
        assert client.get('/api/private',headers={**auth,'Sec-Fetch-Site':'same-origin'}).status_code==200


def test_enabled_instance_protects_static_document_and_denial_responses():
    session=LocalInstanceSession();session.enable(ORIGIN);app=make_app(session)
    @app.get('/')
    def static():return {'static':True}
    with TestClient(app,base_url=ORIGIN,client=('127.0.0.1',123)) as client:
        for path in ('/', '/api/private'):
            response=client.get(path)
            assert response.headers['x-frame-options']=='DENY'
            assert response.headers['x-content-type-options']=='nosniff'
            assert response.headers['referrer-policy']=='no-referrer'
            assert "frame-ancestors 'none'" in response.headers['content-security-policy']
            assert "frame-src 'self' blob:" in response.headers['content-security-policy']


@pytest.mark.parametrize('event', ['reload','close','session_close'])
def test_native_picker_discards_result_after_document_or_session_invalidation(event):
    path=Path(__file__).resolve().parents[2]/'desktop'/'launcher.py'
    spec=importlib.util.spec_from_file_location('test_desktop_bridge_epoch',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    session=LocalInstanceSession();session.enable(ORIGIN)
    bridge=module.DesktopApi(ORIGIN,session=session)
    class Window:
        def get_current_url(self):return ORIGIN+'/chat'
        def create_file_dialog(self,*args,**kwargs):
            if event=='reload':bridge._before_load()
            elif event=='close':bridge._closing()
            else:session.close()
            return ['D:/Synthetic/PrivateProject']
    bridge._window=Window()
    assert bridge.choose_project_folder() is None


def test_native_picker_success_cancel_and_exclusion():
    path=Path(__file__).resolve().parents[2]/'desktop'/'launcher.py'
    spec=importlib.util.spec_from_file_location('test_desktop_bridge_success',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    bridge=module.DesktopApi(ORIGIN)
    class Window:
        selected=['D:/Synthetic/Project']
        def get_current_url(self):return ORIGIN+'/chat'
        def create_file_dialog(self,*args,**kwargs):return self.selected
    bridge._window=Window()
    assert bridge.choose_project_folder()=='D:/Synthetic/Project'
    bridge._window.selected=[]
    assert bridge.choose_project_folder() is None
    bridge._picker_lock.acquire()
    try:assert bridge.choose_project_folder() is None
    finally:bridge._picker_lock.release()


def authenticated_client(*, clock=None):
    session = LocalInstanceSession(clock=clock)
    secret = session.enable(ORIGIN)
    app = make_app(session)
    @app.get('/api/echo')
    async def echo(request: Request):
        return {'query': request.scope['query_string'].decode('ascii')}
    client = TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 123))
    response = client.post('/api/local-session/bootstrap', headers={'Authorization': 'Bearer ' + secret})
    assert response.status_code == 200 and 'set-cookie' not in response.headers
    return session, app, client, {'Authorization': 'ScholarFlow ' + response.json()['token']}


def ticket(client, auth, target='/api/echo?a=1&a=2&path=%2Fref', kind='download'):
    response = client.post('/api/local-session/ticket', headers=auth, json={'target': target, 'kind': kind})
    assert response.status_code == 200, response.text
    value = response.json()
    assert len(value['ticket']) == 64 and value['expires_in'] == 30
    return value['url']


def test_no_ambient_cookie_or_old_custom_header_grants_authentication():
    session, app, client, auth = authenticated_client()
    assert not client.cookies
    raw_token = auth['Authorization'].split(' ', 1)[1]
    assert client.get('/api/private', headers={'Cookie': 'sf_instance_fake=' + raw_token}).status_code == 401
    assert client.get('/api/private', headers={'X-ScholarFlow-Session': raw_token}).status_code == 401
    assert client.get('/api/private', headers=auth).status_code == 200
    assert app.state.calls == 1


@pytest.mark.parametrize('kind', ['download', 'events'])
def test_ticket_preserves_exact_query_removes_credential_and_only_one_get(kind):
    session, app, client, auth = authenticated_client()
    url = ticket(client, auth, kind=kind)
    response = client.get(url)
    assert response.status_code == 200 and response.json()['query'] == 'a=1&a=2&path=%2Fref'
    assert '__sf_ticket' not in response.text and 'set-cookie' not in response.headers
    assert client.get(url).status_code == 401
    assert client.get('/api/private').status_code == 401
    assert app.state.calls == 0


def test_ticket_cannot_change_method_path_query_origin_or_kind():
    session, app, client, auth = authenticated_client()
    url = ticket(client, auth)
    assert client.post(url).status_code == 401
    assert client.head(url).status_code == 401
    assert client.get(url.replace('/api/echo', '/api/private')).status_code == 401
    assert client.get(url.replace('a=1&a=2', 'a=2&a=1')).status_code == 401
    assert client.get(url, headers={'Origin': 'http://127.0.0.1:9999'}).status_code in (401, 403)
    with pytest.raises(Exception):
        with client.websocket_connect(ORIGIN.replace('http:', 'ws:') + url):
            pass
    assert client.get(url).status_code == 200
    assert app.state.calls == 0


def test_websocket_ticket_is_single_use_and_not_a_http_credential():
    session, app, client, auth = authenticated_client()
    url = ticket(client, auth, '/api/socket', 'websocket')
    assert client.get(url).status_code == 401
    with client.websocket_connect(ORIGIN.replace('http:', 'ws:') + url) as ws:
        assert ws.receive_text() == 'authenticated'
    with pytest.raises(Exception):
        with client.websocket_connect(ORIGIN.replace('http:', 'ws:') + url):
            pass


@pytest.mark.parametrize('variant', ['duplicate', 'not_last', 'encoded_name', 'same_auth_invalid'])
def test_ambiguous_ticket_query_is_rejected_without_endpoint_execution(variant):
    session, app, client, auth = authenticated_client()
    url = ticket(client, auth, '/api/private')
    if variant == 'duplicate': url += '&' + url.split('?', 1)[1]
    if variant == 'not_last': url += '&later=1'
    if variant == 'encoded_name': url = url.replace('__sf_ticket', '%5f%5fsf_ticket')
    if variant == 'same_auth_invalid': url += 'invalid'
    assert client.get(url, headers=auth if variant == 'same_auth_invalid' else {}).status_code == 401
    assert app.state.calls == 0


@pytest.mark.parametrize('target', ['/elsewhere', 'https://remote.example/api/private', '//127.0.0.1/api/private',
    '/api/../private', '/api/%2e%2e/private', '/api/private#fragment', '/api/private?', '/api/private?__sf_ticket=old',
    '/api/private?%5F%5Fsf_ticket=old', '/api/private?bad=%zz', '/api/private?word=hello world', '/api/private\\other', None, {'nested': True}])
def test_ticket_rejects_nested_or_noncanonical_targets(target):
    session, app, client, auth = authenticated_client()
    response = client.post('/api/local-session/ticket', headers=auth, json={'target': target, 'kind': 'download'})
    assert response.status_code == 400 and not session._tickets and app.state.calls == 0


@pytest.mark.parametrize('raw', [b'null', b'[]', b'broken', b'{"target":"/api/private","target":"/api/echo","kind":"download"}',
    b'{"target":"/api/private","kind":"download","extra":true}', b'{"target":"/api/private","kind":"POST"}',
    b'{"target":"/api/private","kind":{}}', b'\xff'])
def test_invalid_json_ticket_body_never_reaches_endpoint(raw):
    session, app, client, auth = authenticated_client()
    response = client.post('/api/local-session/ticket', headers={**auth, 'Content-Type': 'application/json'}, content=raw)
    assert response.status_code == 400 and not session._tickets and app.state.calls == 0


def test_ticket_body_limit_missing_auth_expiry_capacity_and_rotation():
    now = [1.0]
    session, app, client, auth = authenticated_client(clock=lambda: now[0])
    assert client.post('/api/local-session/ticket', json={'target': '/api/private', 'kind': 'download'}).status_code == 401
    assert client.post('/api/local-session/ticket', headers=auth, content='a' * 4097).status_code == 400
    response = client.post('/api/local-session/ticket', headers={**auth, 'Content-Type': 'application/json'}, content='a' * 4097)
    assert response.status_code == 413 and not session._tickets
    url = ticket(client, auth, '/api/private')
    now[0] += 30
    assert client.get(url).status_code == 401
    assert not session._tickets
    headers = {b'host': '127.0.0.1:8555', b'authorization': auth['Authorization']}
    for _ in range(512):
        session.issue_ticket(headers, '/api/private', 'download')
    assert client.post('/api/local-session/ticket', headers=auth, json={'target': '/api/private', 'kind': 'events'}).status_code == 429
    now[0] += 31
    url = ticket(client, auth, '/api/private')
    assert len(session._tickets) == 1
    session.enable(ORIGIN)
    assert client.get(url).status_code == 401 and not session._tickets
    assert client.get('/api/private', headers=auth).status_code == 401 and app.state.calls == 0


def test_activation_preserves_its_own_descriptor_credential_without_api_bypass():
    session, app, client, auth = authenticated_client()
    @app.post('/api/runtime/activate')
    async def activate(request: Request):
        from fastapi import HTTPException
        if request.headers.get('x-scholarflow-instance') != 'descriptor-synthetic':
            raise HTTPException(403, 'invalid descriptor')
        return {'activated': True}
    headers = {'X-ScholarFlow-Instance': 'descriptor-synthetic'}
    assert client.post('/api/runtime/activate', headers=headers).json() == {'activated': True}
    assert client.post('/api/runtime/activate', headers={'X-ScholarFlow-Instance': 'wrong'}).status_code == 403
    assert client.post('/api/runtime/activate').status_code == 401
    assert client.post('/api/runtime/activate', headers=[('X-ScholarFlow-Instance', 'descriptor-synthetic')] * 2).status_code == 401
    assert client.get('/api/private', headers=headers).status_code == 401
    assert client.get('/api/runtime/activate', headers=headers).status_code == 401
    assert app.state.calls == 0


def test_ticket_matches_raw_encoded_path_and_consumes_atomically():
    from concurrent.futures import ThreadPoolExecutor
    session, app, client, auth = authenticated_client()
    url = ticket(client, auth, '/api/e%63ho?case=%2f')
    assert client.get(url.replace('/api/e%63ho', '/api/echo')).status_code == 401
    assert client.get(url).json()['query'] == 'case=%2f'
    one_use = ticket(client, auth, '/api/private')
    with ThreadPoolExecutor(max_workers=2) as workers:
        statuses = list(workers.map(lambda _: client.get(one_use).status_code, range(2)))
    assert sorted(statuses) == [200, 401] and app.state.calls == 1
