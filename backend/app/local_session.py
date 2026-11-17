"""Ephemeral desktop-instance authentication, without credential persistence.

This blocks unauthenticated access to the local service. It does not isolate
the app from an OS administrator or a process able to inspect the same user's
memory/browser profile. Host/Origin checks remain a separate outer boundary.
"""
from __future__ import annotations
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, unquote_plus, unquote


class LocalInstanceSession:
    def __init__(self, *, clock=None):
        self._lock = threading.RLock()
        self._origin = None
        self._bootstrap = None
        self._token = None
        self._tickets = {}
        self._clock = clock or time.monotonic

    @property
    def enabled(self):
        with self._lock:
            return self._origin is not None

    @property
    def active(self):
        with self._lock:
            return self._token is not None

    def enable(self, origin):
        parsed = urlsplit(origin)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('Desktop instance requires a fixed loopback origin')
        with self._lock:
            self._origin = origin
            self._bootstrap = secrets.token_urlsafe(48)
            self._token = secrets.token_urlsafe(48)
            self._tickets.clear()
            return self._bootstrap

    def close(self):
        with self._lock:
            self._bootstrap = self._token = None
            self._tickets.clear()

    def _same_origin(self, headers):
        return (headers.get(b'host') == self._origin.removeprefix('http://')
                and headers.get(b'origin', self._origin) == self._origin
                and headers.get(b'sec-fetch-site', 'none') in ('none', 'same-origin'))

    @staticmethod
    def _matches(actual, expected):
        # compare_digest(str, str) rejects non-ASCII input. Untrusted header
        # octets must be an authentication failure, never a server exception.
        return (isinstance(actual, str) and actual.isascii()
                and len(actual) == len(expected) and secrets.compare_digest(actual, expected))

    def authenticated(self, headers):
        with self._lock:
            if not self._token or not self._same_origin(headers):
                return False
            # Cookies are host-scoped, not port-scoped: another loopback
            # service must never receive ambient desktop credentials.
            return self._matches(headers.get(b'authorization', ''), 'ScholarFlow ' + self._token)

    def readiness_authorized(self, headers):
        with self._lock:
            return bool(self._bootstrap and self._same_origin(headers) and self._matches(
                headers.get(b'authorization', ''), 'Bearer ' + self._bootstrap))

    def bootstrap(self, headers):
        with self._lock:
            if not self._bootstrap or not self.readiness_authorized(headers):
                return None
            self._bootstrap = None
            # The caller keeps this in origin-bound sessionStorage/memory.
            # Never set an ambient cookie or put the reusable token in a URL.
            return self._token

    @staticmethod
    def valid_target(target):
        if (not isinstance(target, str) or not target.isascii() or not target.startswith('/api/')
                or len(target) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in target)
                or '\\' in target or '#' in target or target.endswith('?')
                or re.search(r'%(?![a-fA-F0-9]{2})', target)):
            return False
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or any(part in ('.', '..') for part in unquote(parsed.path).split('/')):
            return False
        return not any(unquote_plus(item.partition('=')[0]) == '__sf_ticket' for item in parsed.query.split('&'))

    def _prune_tickets(self):
        now = self._clock()
        for identity, receipt in list(self._tickets.items()):
            if receipt['expires'] <= now:
                self._tickets.pop(identity, None)

    def issue_ticket(self, headers, target, kind):
        with self._lock:
            if not self.authenticated(headers):
                return None
            if not self.valid_target(target) or kind not in ('download', 'websocket', 'events'):
                raise ValueError('Invalid ticket target or kind')
            self._prune_tickets()
            if len(self._tickets) >= 512:
                raise OverflowError('Too many outstanding tickets')
            identity = secrets.token_urlsafe(48)
            self._tickets[identity] = {'target': target.encode('ascii'), 'kind': kind, 'expires': self._clock() + 30}
            return {'ticket': identity, 'url': target + ('&' if '?' in target else '?') + '__sf_ticket=' + identity,
                    'kind': kind, 'expires_in': 30}

    def consume_ticket(self, headers, scope):
        """Return a query-stripped scope only for this exact single GET/WS."""
        with self._lock:
            if not self._token or not self._same_origin(headers):
                return None
            raw = scope.get('query_string', b'')
            parts = raw.split(b'&')
            reserved = [i for i, item in enumerate(parts) if unquote_plus(item.partition(b'=')[0].decode('latin1')) == '__sf_ticket']
            if reserved != [len(parts) - 1] or not re.fullmatch(rb'__sf_ticket=[A-Za-z0-9_-]{64}', parts[-1]):
                return None
            identity = parts[-1].split(b'=', 1)[1].decode('ascii')
            self._prune_tickets()
            receipt = self._tickets.get(identity)
            if receipt is None:
                return None
            query = b'&'.join(parts[:-1])
            path = scope.get('raw_path') or scope.get('path', '').encode('utf-8')
            target = path + (b'?' + query if len(parts) > 1 else b'')
            transport = scope['type']
            allowed = (transport == 'websocket' and receipt['kind'] == 'websocket' or
                       transport == 'http' and scope.get('method') == 'GET' and receipt['kind'] in ('download', 'events'))
            if not allowed or target != receipt['target']:
                return None
            self._tickets.pop(identity)
            return {**scope, 'query_string': query}


instance_session = LocalInstanceSession()


class LocalInstanceMiddleware:
    def __init__(self, app, *, session=None):
        self.app, self.session = app, session or instance_session

    async def __call__(self, scope, receive, send):
        kind = scope['type']
        if kind not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        path, session = scope.get('path', ''), self.session
        if not session.enabled:
            if kind == 'http' and path == '/api/local-session/status':
                return await self._reply(send, 200, b'{"required":false,"authenticated":false}')
            return await self.app(scope, receive, send)
        if kind == 'http':
            original_send = send

            async def protected_send(message):
                if message['type'] == 'http.response.start':
                    message = dict(message)
                    existing = {key.lower() for key, _ in message.get('headers', [])}
                    headers = list(message.get('headers', []))
                    # Documents stay data: they cannot embed a foreign bridge
                    # caller or frame the desktop application. Do not restrict
                    # PDF workers, inline chart styles, or ordinary link-outs.
                    for key, value in (
                        (b'content-security-policy', b"frame-ancestors 'none'; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'"),
                        (b'x-frame-options', b'DENY'),
                        (b'x-content-type-options', b'nosniff'),
                        (b'referrer-policy', b'no-referrer'),
                    ):
                        if key not in existing:
                            headers.append((key, value))
                    message['headers'] = headers
                await original_send(message)

            send = protected_send
        if not (path.startswith('/api') or path in ('/docs', '/redoc', '/openapi.json')):
            return await self.app(scope, receive, send)
        pairs = [(k.lower(), v.decode('latin1')) for k, v in scope.get('headers', [])]
        headers = dict(pairs)
        duplicate = any(sum(k == key for k, _ in pairs) > 1 for key in (b'host', b'origin', b'authorization', b'x-scholarflow-instance'))
        authenticated = not duplicate and session.authenticated(headers)
        if kind == 'http' and path == '/api/local-session/status' and scope.get('method') == 'GET':
            body = b'{"required":true,"authenticated":' + (b'true' if authenticated else b'false') + b'}'
            return await self._reply(send, 200, body)
        if kind == 'http' and path == '/api/local-session/bootstrap' and scope.get('method') == 'POST' and not duplicate:
            token = session.bootstrap(headers)
            if token:
                return await self._reply(send, 200, json.dumps({'authenticated': True, 'token': token}).encode())
        if kind == 'http' and path == '/api/local-session/ticket' and scope.get('method') == 'POST' and authenticated:
            return await self._ticket(receive, send, headers)
        # Startup readiness gets one narrow authenticated read. This does not
        # consume the launch secret needed by the first trusted page.
        readiness = (kind == 'http' and path == '/api/health' and scope.get('method') == 'GET'
                     and not duplicate and session.readiness_authorized(headers))
        # Activation has its own per-instance descriptor credential. Do not
        # require a browser session from a second desktop launcher.
        activation = (kind == 'http' and path == '/api/runtime/activate' and scope.get('method') == 'POST'
                      and not duplicate and bool(headers.get(b'x-scholarflow-instance')) and session._same_origin(headers))
        raw_query = scope.get('query_string', b'')
        has_ticket = any(unquote_plus(item.partition(b'=')[0].decode('latin1')) == '__sf_ticket' for item in raw_query.split(b'&'))
        if has_ticket:
            cleaned = None if duplicate else session.consume_ticket(headers, scope)
            if cleaned is not None:
                return await self.app(cleaned, receive, send)
            authenticated = readiness = activation = False
        if authenticated or readiness or activation:
            return await self.app(scope, receive, send)
        if kind == 'websocket':
            return await send({'type':'websocket.close', 'code':1008, 'reason':'Desktop instance authentication required'})
        return await self._reply(send, 401, b'{"detail":"Open this page from the current ScholarFlow desktop window"}')

    async def _ticket(self, receive, send, headers):
        if headers.get(b'content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return await self._reply(send, 400, b'{"detail":"Ticket body must be JSON"}')
        raw = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            if message['type'] != 'http.request':
                continue
            chunk = message.get('body', b'')
            if len(raw) + len(chunk) > 4096:
                return await self._reply(send, 413, b'{"detail":"Ticket body exceeds 4096 bytes"}')
            raw.extend(chunk)
            if not message.get('more_body', False):
                break
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate JSON field')
                result[key] = value
            return result
        try:
            body = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(body, dict) or set(body) != {'target', 'kind'}:
                raise ValueError('Invalid ticket body')
            result = self.session.issue_ticket(headers, body['target'], body['kind'])
            if result is None:
                return await self._reply(send, 401, b'{"detail":"Desktop session expired"}')
        except (ValueError, TypeError, RecursionError):
            return await self._reply(send, 400, b'{"detail":"Invalid ticket target or kind"}')
        except OverflowError:
            return await self._reply(send, 429, b'{"detail":"Too many outstanding tickets"}')
        return await self._reply(send, 200, json.dumps(result).encode('utf-8'))

    @staticmethod
    async def _reply(send, status, body):
        headers = [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()),
                   (b'cache-control', b'no-store'), (b'referrer-policy', b'no-referrer')]
        await send({'type':'http.response.start', 'status':status, 'headers':headers})
        await send({'type':'http.response.body', 'body':body})
