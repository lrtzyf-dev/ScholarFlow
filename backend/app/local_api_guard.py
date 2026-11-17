"""Desktop loopback boundary for HTTP and WebSocket, before endpoint execution.

This rejects hostile browser origins/DNS rebinding; it is not an OS sandbox or
authentication against another process running as the same Windows user.
"""
from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit


def _loopback(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _authority(value: str, *, default_port: int = 80):
    try:
        parsed = urlsplit("http://" + value)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment or not _loopback(parsed.hostname)):
            return None
        return parsed.hostname.lower(), parsed.port or default_port
    except ValueError:
        return None


class LocalAPIGuard:
    def __init__(self, app, *, test_transport: bool = False, allow_development_origins: bool = False):
        self.app = app
        self.test_transport = test_transport
        self.allow_development_origins = allow_development_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        names = [key.lower() for key, _ in headers]
        values = {key.lower(): value.decode("latin-1") for key, value in headers}
        client = (scope.get("client") or ("", 0))[0]
        host = values.get(b"host", "")
        scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
        authority = _authority(host, default_port=443 if scheme == "https" else 80)
        synthetic = self.test_transport and client == "testclient" and host == "testserver"
        # This is a local desktop service, not a public reverse-proxied API.
        # Reject forwarded metadata rather than trusting a spoofed client/Host.
        forwarded = any(key == b"forwarded" or key.startswith(b"x-forwarded-") for key in names)
        allowed = (synthetic or (_loopback(client) and authority is not None)) and not forwarded
        if names.count(b"host") != 1 or names.count(b"origin") > 1:
            allowed = False
        origin = values.get(b"origin")
        if allowed and origin is not None:
            try:
                parsed = urlsplit(origin)
                origin_authority = _authority(parsed.netloc, default_port=443 if parsed.scheme == "https" else 80)
                same = origin_authority is not None and origin_authority == authority and parsed.scheme == scheme
                from .local_session import instance_session
                dev = (self.allow_development_origins and not instance_session.enabled
                       and origin in ("http://localhost:5173", "http://127.0.0.1:5173"))
                allowed = (parsed.scheme in ("http", "https") and not parsed.path
                           and not parsed.query and not parsed.fragment and (same or dev))
            except ValueError:
                allowed = False
        if values.get(b"sec-fetch-site") == "cross-site" and origin is None:
            allowed = False
        if not allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "Untrusted local API request"})
            else:
                body = b'{"detail":"Untrusted local API request"}'
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)
