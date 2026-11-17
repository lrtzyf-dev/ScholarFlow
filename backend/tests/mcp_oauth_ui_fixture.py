"""Local-only synthetic OAuth issuer for browser regression; never production."""
import html
from urllib.parse import urlencode
from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse, Response
from app.main import app
_original_routes = list(app.router.routes)


def base(request):
    return str(request.base_url).rstrip('/')


@app.get('/oauth-fixture/mcp')
@app.post('/oauth-fixture/mcp')
async def resource(request: Request):
    if request.headers.get('authorization') == 'Bearer synthetic-ui-access':
        if request.method == 'GET':
            return Response(status_code=405)
        payload = await request.json()
        method = payload.get('method')
        if method == 'initialize':
            return {'jsonrpc': '2.0', 'id': payload['id'], 'result': {'protocolVersion': payload['params']['protocolVersion'],
                'serverInfo': {'name': 'local-oauth-fixture', 'version': '1'}, 'capabilities': {'tools': {}}}}
        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': payload['id'], 'result': {'tools': [
                {'name': 'fixture_read', 'description': '本地合成只读工具', 'inputSchema': {'type': 'object', 'properties': {}}}]}}
        if method.startswith('notifications/'):
            return Response(status_code=202)
        if method == 'tools/call':
            return {'jsonrpc': '2.0', 'id': payload.get('id'), 'result': {'content': [{'type': 'text', 'text': 'local fixture'}]}}
        return {'jsonrpc': '2.0', 'id': payload.get('id'), 'error': {'code': -32601, 'message': 'Method not found'}}
    return JSONResponse({}, status_code=401, headers={'WWW-Authenticate':
        f'Bearer resource_metadata="{base(request)}/oauth-fixture/resource-metadata"'})


@app.get('/oauth-fixture/resource-metadata')
def prm(request: Request):
    return {'resource': base(request) + '/oauth-fixture/mcp', 'authorization_servers': [base(request) + '/oauth-fixture']}


@app.get('/.well-known/oauth-authorization-server/oauth-fixture')
def metadata(request: Request):
    issuer = base(request) + '/oauth-fixture'
    return {'issuer': issuer, 'authorization_endpoint': issuer + '/authorize', 'token_endpoint': issuer + '/token',
        'registration_endpoint': issuer + '/register', 'revocation_endpoint': issuer + '/revoke',
        'response_types_supported': ['code'], 'code_challenge_methods_supported': ['S256'],
        'grant_types_supported': ['authorization_code', 'refresh_token'], 'token_endpoint_auth_methods_supported': ['none']}


@app.post('/oauth-fixture/register')
async def register(request: Request):
    return {**await request.json(), 'client_id': 'browser-fixture-public', 'token_endpoint_auth_method': 'none'}


@app.get('/oauth-fixture/authorize')
def authorize(request: Request, redirect_uri: str, state: str):
    location = redirect_uri + '?' + urlencode({'state': state, 'code': 'browser-fixture-code', 'iss': base(request) + '/oauth-fixture'})
    return HTMLResponse('<h1>本地合成授权服务器</h1><p>仅用于测试，无真实账号或费用。</p><a href="' + html.escape(location, quote=True) + '">同意本地测试授权</a>')


@app.post('/oauth-fixture/token')
def token():
    return {'access_token': 'synthetic-ui-access', 'refresh_token': 'synthetic-ui-refresh', 'token_type': 'Bearer', 'expires_in': 600}


@app.post('/oauth-fixture/revoke')
def revoke():
    return {}


@app.post('/api/test-fixture/mcp-tool')
async def tool(request: Request):
    import json
    from starlette.concurrency import run_in_threadpool
    from app.mcp_bridge import bridge
    payload = await request.json()
    return json.loads(await run_in_threadpool(bridge.call_tool, payload['name'], {}))


# The desktop app may mount an existing frontend dist at '/'. Fixture routes
# must precede that fallback, just as production API routes do.
app.router.routes[:] = [route for route in app.router.routes if route not in _original_routes] + _original_routes
