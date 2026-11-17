"""User initiated MCP AuthCode/PKCE, protected persistence and fenced refresh.

The SDK owns discovery, issuer/resource validation, registration and PKCE.
The application owns consent lifetime, no-log loopback callbacks, revocation,
single-flight refresh, and a live authorization check at every HTTP request.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2
from sqlalchemy import text
from .db import SessionLocal
from .mcp_auth_models import McpUserGrant
from .credential_protection import protect, unprotect


class UserOAuthError(ValueError):
    pass


def user_flow(cfg):
    return (cfg.get('oauth') or {}).get('flow') == 'authorization_code'


def fingerprint(cfg):
    relevant = {key: cfg.get(key) for key in ('url', 'transport', 'oauth', 'headers', 'bearer_token')}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def safe_url(value):
    parsed = urlsplit(str(value))
    if parsed.username or parsed.password or parsed.fragment or parsed.scheme not in ('http', 'https'):
        raise UserOAuthError('OAuth 地址必须为有效 HTTPS 地址；不允许 URL 内嵌凭证')
    if parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise UserOAuthError('仅回环地址允许明文 HTTP')
    if not parsed.hostname:
        raise UserOAuthError('OAuth 地址缺少主机')
    return str(value)


@contextmanager
def write():
    with SessionLocal() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _bundle(row):
    if not row.protected:
        return {}
    try:
        return json.loads(unprotect(row.protected))
    except Exception:
        raise UserOAuthError('系统凭证不可解密，请重新授权') from None


def _row(db, cfg):
    row = db.get(McpUserGrant, cfg.get('server_id'))
    if row is None or row.fingerprint != fingerprint(cfg):
        raise UserOAuthError('此连接需要用户授权')
    return row


def begin(cfg):
    safe_url(cfg['url'])
    with write() as db:
        row = db.get(McpUserGrant, cfg['server_id'])
        if row is None:
            row = McpUserGrant(server_id=cfg['server_id'], generation=0)
            db.add(row)
        row.generation += 1
        row.fingerprint, row.state = fingerprint(cfg), 'authorizing'
        row.protected, row.expires_at = None, 0
        row.refresh_owner, row.refresh_until, row.message = None, 0, '等待用户登录'
        return row.generation


def _save(cfg, generation, bundle, *, expires_at=0, state='authorized'):
    sealed = protect(json.dumps(bundle, ensure_ascii=False, separators=(',', ':')))
    with write() as db:
        row = _row(db, cfg)
        if row.generation != generation or row.state not in ('authorizing', 'authorized'):
            raise UserOAuthError('授权已取消或撤销，迟到的响应已丢弃')
        row.protected, row.expires_at, row.state, row.message = sealed, expires_at, state, ''


def status(cfg):
    with SessionLocal() as db:
        try:
            row = _row(db, cfg)
        except UserOAuthError:
            return {'state': 'unauthorized', 'message': '尚未登录'}
        session = _sessions.get(cfg['server_id'])
        result = {'state': row.state, 'message': row.message, 'expires_at': row.expires_at,
                  'refreshing': bool(row.refresh_owner and row.refresh_until > time.time())}
        if row.state == 'authorizing':
            if not session or session.get('generation') != row.generation:
                result.update(state='expired', message='应用重启或授权窗口已关闭，请重新登录')
            else:
                result.update(authorization_url=session.get('url'), deadline=session['deadline'])
        return result


def invalidate(cfg, message='本地授权已撤销'):
    """Synchronously fence active refresh/callback writers before disconnect."""
    if not cfg.get('server_id'):
        return
    with write() as db:
        row = db.get(McpUserGrant, cfg.get('server_id'))
        if row:
            row.generation += 1
            row.state, row.protected, row.expires_at = 'revoked', None, 0
            row.refresh_owner, row.refresh_until, row.message = None, 0, message


def _failure(cfg, generation, message, state='reauthorize'):
    with write() as db:
        row = db.get(McpUserGrant, cfg.get('server_id'))
        if row and row.generation == generation:
            row.state, row.message, row.protected = state, message, None
            row.refresh_owner, row.refresh_until = None, 0


_sessions = {}


class _RedactSDKAuthErrors(logging.Filter):
    def filter(self, record):
        if record.levelno >= logging.WARNING or record.name.startswith('httpx2'):
            record.msg = ('OAuth/HTTP 请求诊断（远端内容和凭证已省略）' if record.levelno >= logging.WARNING
                          else 'HTTP 请求完成（地址与响应内容已省略）')
            record.args = ()
            record.exc_info, record.exc_text = None, None
        return True


logging.getLogger('mcp.client.auth.oauth2').addFilter(_RedactSDKAuthErrors())
logging.getLogger('httpx2').addFilter(_RedactSDKAuthErrors())


async def start(cfg):
    if not user_flow(cfg) or cfg.get('transport') != 'http':
        raise UserOAuthError('请先保存 HTTP 用户授权连接')
    if not cfg.get('enabled', True):
        raise UserOAuthError('请先启用此连接')
    await cancel(cfg, invalidate_grant=False)
    generation = begin(cfg)
    session = {'generation': generation, 'deadline': time.time() + 300, 'url': None}
    _sessions[cfg['server_id']] = session
    session['task'] = asyncio.create_task(_authorize(cfg, generation, session))
    # Return when discovery has prepared a URL, without waiting for a person.
    for _ in range(100):
        if session.get('url') or session['task'].done():
            break
        await asyncio.sleep(.05)
    return status(cfg)


async def cancel(cfg, *, invalidate_grant=True):
    if invalidate_grant:
        invalidate(cfg, '用户已取消授权')
    session = _sessions.pop(cfg.get('server_id'), None)
    if session:
        task = session.get('task')
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class Storage:
    def __init__(self, cfg, generation):
        self.cfg, self.generation = cfg, generation
        self.client = None
        self.provider = None
        self.complete = False

    async def get_tokens(self):
        return None  # Login is explicitly requested; never reuse an old grant.

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        oauth = self.cfg.get('oauth') or {}
        if oauth.get('client_id'):
            if not oauth.get('issuer'):
                raise UserOAuthError('预注册客户端必须填写授权服务器 issuer')
            self.client = OAuthClientInformationFull(client_id=oauth['client_id'],
                issuer=safe_url(oauth['issuer']), token_endpoint_auth_method='none',
                redirect_uris=self.provider.context.client_metadata.redirect_uris)
        return self.client

    async def set_client_info(self, client_info):
        self.client = client_info

    async def set_tokens(self, tokens):
        context = self.provider.context
        metadata = context.oauth_metadata
        if not metadata or not self.client:
            raise UserOAuthError('授权缺少已验证的服务器和客户端信息')
        bundle = {'tokens': tokens.model_dump(mode='json'), 'client': self.client.model_dump(mode='json'),
                  'metadata': metadata.model_dump(mode='json'), 'resource': context.get_resource_url()}
        # Omitted expiry remains unknown; the server's 401 will invalidate it.
        expiry = time.time() + max(0, tokens.expires_in) if tokens.expires_in is not None else -1
        _save(self.cfg, self.generation, bundle, expires_at=expiry)
        self.complete = True


async def _authorize(cfg, generation, session):
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata, AuthorizationCodeResult
    callback = asyncio.get_running_loop().create_future()
    server = None

    async def receive(reader, writer):
        status_code = 400
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            if len(raw) > 8192:
                raise ValueError()
            request_line, *headers = raw.decode('ascii').split('\r\n')
            method, target, protocol = request_line.split(' ')
            parsed = urlsplit(target)
            query = parse_qs(parsed.query, keep_blank_values=True)
            host = next((h.split(':', 1)[1].strip() for h in headers if h.lower().startswith('host:')), '')
            if (method != 'GET' or parsed.path != '/oauth/callback' or parsed.netloc
                    or host != session['callback_host'] or time.time() > session['deadline']
                    or any(len(values) != 1 for values in query.values())):
                raise ValueError()
            state = query.get('state', [''])[0]
            if not session.get('state') or not secrets.compare_digest(state, session['state']):
                raise ValueError()
            with SessionLocal() as db:
                row = _row(db, cfg)
                if row.generation != generation or row.state != 'authorizing':
                    raise ValueError()
            if callback.done():
                raise ValueError()
            if query.get('error'):
                callback.set_exception(UserOAuthError('用户或授权服务器拒绝了登录'))
            else:
                code = query.get('code', [''])[0]
                if not code or len(code) > 4096:
                    raise ValueError()
                callback.set_result(AuthorizationCodeResult(code=code, state=state, iss=query.get('iss', [None])[0]))
            status_code = 200
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            body = b'Authorization received. Return to ScholarFlow.' if status_code == 200 else b'Invalid or expired authorization callback.'
            writer.write(f'HTTP/1.1 {status_code} OK\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    async def redirect(url):
        safe_url(url)
        query = parse_qs(urlsplit(url).query)
        if query.get('code_challenge_method') != ['S256'] or not query.get('state'):
            raise UserOAuthError('授权服务器不满足 PKCE S256 要求')
        session['url'], session['state'] = url, query['state'][0]

    async def wait_callback():
        return await asyncio.wait_for(callback, max(1, session['deadline'] - time.time()))

    class StrictProvider(OAuthClientProvider):
        def _select_authorization_server(self, advertised):
            configured = (cfg.get('oauth') or {}).get('issuer')
            if configured:
                from mcp.client.auth.utils import issuers_match
                match = next((value for value in advertised if issuers_match(value, configured)), None)
                if not match:
                    raise UserOAuthError('连接声明的授权服务器与预注册 issuer 不一致')
                return match
            return super()._select_authorization_server(advertised)

        async def _perform_authorization_code_grant(self):
            metadata = self.context.oauth_metadata
            if not metadata or 'S256' not in (metadata.code_challenge_methods_supported or []):
                raise UserOAuthError('服务器元数据没有声明支持 PKCE S256，拒绝降级授权')
            configured = (cfg.get('oauth') or {}).get('issuer')
            if configured:
                from mcp.client.auth.utils import issuers_match
                if not issuers_match(str(metadata.issuer), configured):
                    raise UserOAuthError('授权服务器与预注册 issuer 不一致')
            for field in ('authorization_endpoint', 'token_endpoint'):
                safe_url(getattr(metadata, field))
            return await super()._perform_authorization_code_grant()

        async def _handle_token_response(self, response):
            if response.status_code not in (200, 201):
                raise UserOAuthError('授权代码交换失败，请重新登录')
            return await super()._handle_token_response(response)

    try:
        server = await asyncio.start_server(receive, '127.0.0.1', 0, limit=8192)
        port = server.sockets[0].getsockname()[1]
        session['callback_host'] = f'127.0.0.1:{port}'
        redirect_uri = f'http://127.0.0.1:{port}/oauth/callback'
        storage = Storage(cfg, generation)
        provider = StrictProvider(cfg['url'], OAuthClientMetadata(
            client_name='研序 ScholarFlow', redirect_uris=[redirect_uri],
            grant_types=['authorization_code', 'refresh_token'], response_types=['code'],
            token_endpoint_auth_method='none', scope=(cfg.get('oauth') or {}).get('scope') or None),
            storage=storage, redirect_handler=redirect, callback_handler=wait_callback)
        storage.provider = provider
        flow = provider.async_auth_flow(httpx2.Request('POST', cfg['url'],
            headers={'MCP-Protocol-Version': '2026-07-28', 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 'oauth-probe', 'method': 'initialize',
                  'params': {'protocolVersion': '2026-07-28', 'capabilities': {},
                             'clientInfo': {'name': 'ScholarFlow OAuth', 'version': '1'}}}))
        try:
            request = await anext(flow)
            async with httpx2.AsyncClient(timeout=20, follow_redirects=False) as client:
                while True:
                    safe_url(str(request.url))
                    response = await client.send(request)
                    request = await flow.asend(response)
                    if storage.complete:
                        break  # Credentials saved; the probe never executes a tool.
        finally:
            await flow.aclose()
        if not storage.complete:
            raise UserOAuthError('服务器没有要求用户授权或未完成授权')
    except asyncio.CancelledError:
        raise
    except (Exception, StopAsyncIteration) as exc:
        message = str(exc) if isinstance(exc, UserOAuthError) else '授权未完成，请检查服务器支持并重试'
        _failure(cfg, generation, message, 'expired' if isinstance(exc, TimeoutError) else 'reauthorize')
    finally:
        if server:
            server.close()
            await server.wait_closed()
        if not callback.done():
            callback.cancel()
        session.pop('state', None)
        session.pop('url', None)


async def access_token(cfg):
    """Shared durable refresh lease and CAS prevent refresh rotation races."""
    owner, deadline = uuid4().hex, time.time() + 35
    while True:
        blocked = None
        with write() as db:
            row = _row(db, cfg)
            if row.state != 'authorized':
                raise UserOAuthError('用户授权已失效或撤销，请重新登录')
            bundle = _bundle(row)
            tokens = bundle.get('tokens') or {}
            if row.expires_at == -1 or row.expires_at > time.time() + 30:
                return tokens['access_token']
            if not tokens.get('refresh_token'):
                row.state, row.message = 'reauthorize', '访问令牌已过期，服务器未提供刷新令牌'
                blocked = '访问令牌已过期，请重新登录'
            elif not row.refresh_owner or row.refresh_until < time.time():
                # Expired lease may have spent a rotating token; do not replay it.
                if row.refresh_owner:
                    row.state, row.message = 'reauthorize', '上次令牌刷新结果未知，请重新登录'
                    row.refresh_owner = None
                    blocked = '上次令牌刷新结果未知，请重新登录'
                else:
                    generation = row.generation
                    row.refresh_owner, row.refresh_until = owner, time.time() + 30
                    break
        if blocked:
            raise UserOAuthError(blocked)
        if time.time() > deadline:
            raise UserOAuthError('令牌正在刷新，请稍后重试')
        await asyncio.sleep(.05)
    try:
        from mcp.shared.auth import OAuthToken
        metadata, client_info = bundle['metadata'], bundle['client']
        endpoint = safe_url(metadata['token_endpoint'])
        data = {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
                'client_id': client_info['client_id'], 'resource': bundle['resource']}
        auth_method = client_info.get('token_endpoint_auth_method')
        headers = {}
        if auth_method == 'client_secret_post':
            data['client_secret'] = client_info.get('client_secret', '')
        elif auth_method == 'client_secret_basic':
            import base64
            from urllib.parse import quote
            pair = quote(client_info['client_id'], safe='') + ':' + quote(client_info.get('client_secret', ''), safe='')
            headers['Authorization'] = 'Basic ' + base64.b64encode(pair.encode()).decode()
        elif auth_method not in (None, 'none'):
            raise UserOAuthError('服务器要求不受支持的令牌认证方法')
        async with httpx2.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(endpoint, data=data, headers=headers)
        if response.status_code != 200:
            raise UserOAuthError('令牌刷新被拒，请重新授权')
        refreshed = OAuthToken.model_validate(response.json()).model_dump(mode='json')
        if not refreshed.get('refresh_token') or refreshed['refresh_token'] == tokens['refresh_token']:
            # Public clients must rotate refresh tokens. Fail closed, never replay the old one.
            raise UserOAuthError('服务器未轮换刷新令牌，请重新登录')
        if refreshed.get('scope') is None:
            refreshed['scope'] = tokens.get('scope')
        elif not set(refreshed['scope'].split()).issubset(set((tokens.get('scope') or '').split())):
            raise UserOAuthError('刷新响应扩大了授权范围，请重新登录确认')
        bundle['tokens'] = refreshed
        sealed = protect(json.dumps(bundle, separators=(',', ':')))
        with write() as db:
            row = _row(db, cfg)
            if row.generation != generation or row.state != 'authorized' or row.refresh_owner != owner:
                raise UserOAuthError('授权已撤销，刷新响应已丢弃')
            row.protected = sealed
            row.expires_at = time.time() + max(0, refreshed['expires_in']) if refreshed.get('expires_in') is not None else -1
            row.refresh_owner, row.refresh_until = None, 0
        return refreshed['access_token']
    except BaseException:
        _failure(cfg, generation, '令牌刷新失败或结果未知，请重新登录')
        raise UserOAuthError('令牌刷新失败或授权已撤销，请重新登录') from None


class LiveUserAuth(httpx2.Auth):
    def __init__(self, cfg):
        self.cfg = cfg

    async def async_auth_flow(self, request):
        # Auth is run on each HTTP request, including an established MCP session.
        target, bound = urlsplit(str(request.url)), urlsplit(self.cfg['url'])
        if (target.scheme, target.netloc) != (bound.scheme, bound.netloc):
            raise UserOAuthError('OAuth 凭证不能发送到其他来源')
        request.headers['Authorization'] = 'Bearer ' + await access_token(self.cfg)
        response = yield request
        if response.status_code in (401, 403):
            invalidate(self.cfg, '服务器拒绝当前授权，请重新登录')


async def revoke(cfg):
    with SessionLocal() as db:
        try:
            bundle = _bundle(_row(db, cfg))
        except UserOAuthError:
            bundle = {}
    invalidate(cfg)  # Local revocation always wins, even if the remote is offline.
    await cancel(cfg, invalidate_grant=False)
    endpoint = (bundle.get('metadata') or {}).get('revocation_endpoint')
    if not endpoint:
        return {'local_revoked': True, 'remote_revoked': False, 'message': '本地授权已撤销；服务器未提供撤销接口，远端授权需在服务商账号中撤销'}
    try:
        tokens, client_info = bundle['tokens'], bundle['client']
        async with httpx2.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.post(safe_url(endpoint), data={
                'token': tokens.get('refresh_token') or tokens['access_token'],
                'token_type_hint': 'refresh_token' if tokens.get('refresh_token') else 'access_token',
                'client_id': client_info['client_id']})
        remote = response.status_code == 200
    except Exception:
        remote = False
    return {'local_revoked': True, 'remote_revoked': remote,
            'message': '本地和远端授权已撤销' if remote else '本地已撤销，远端撤销未确认，请在服务商账号中检查'}
