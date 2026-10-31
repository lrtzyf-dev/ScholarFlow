"""MCP OAuth 客户端凭证流与认证头构建测试（伪造 httpx，无真实网络）。"""
import asyncio

import httpx
import pytest

import app.mcp_oauth as oauth_mod


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeAsyncClient:
    responder = None
    instances: list = []

    def __init__(self, *a, **k):
        self.post_calls: list = []
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None):
        self.post_calls.append((url, data))
        return FakeAsyncClient.responder(url, data)


@pytest.fixture(autouse=True)
def patch_client(monkeypatch):
    FakeAsyncClient.instances = []
    monkeypatch.setattr(oauth_mod.httpx, "AsyncClient", FakeAsyncClient)
    oauth_mod.manager.clear()
    yield
    oauth_mod.manager.clear()


def _run(coro):
    return asyncio.run(coro)


def test_token_fetched_and_cached():
    def responder(url, data):
        assert data["grant_type"] == "client_credentials"
        assert data["client_id"] == "cid"
        return FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600,
                                 "token_type": "Bearer"})

    FakeAsyncClient.responder = responder
    mgr = oauth_mod.OAuthTokenManager()
    t1 = _run(mgr.get_token(client_id="cid", client_secret="sec",
                            token_url="https://id/token", scope="read"))
    assert t1 == "tok-1"
    # 第二次命中缓存，不再发起请求
    t2 = _run(mgr.get_token(client_id="cid", client_secret="sec",
                            token_url="https://id/token", scope="read"))
    assert t2 == "tok-1"
    assert _total_posts() == 1


def _total_posts():
    return sum(len(i.post_calls) for i in FakeAsyncClient.instances)


def test_force_refresh_calls_again():
    FakeAsyncClient.responder = lambda u, d: FakeResponse(
        200, {"access_token": "tok-new", "expires_in": 3600})
    mgr = oauth_mod.OAuthTokenManager()
    _run(mgr.get_token(client_id="c", client_secret="s", token_url="https://id/token"))
    out = _run(mgr.get_token(client_id="c", client_secret="s",
                             token_url="https://id/token", force=True))
    assert out == "tok-new"
    assert _total_posts() == 2


def test_auth_failure_raises():
    FakeAsyncClient.responder = lambda u, d: FakeResponse(401, "forbidden")
    mgr = oauth_mod.OAuthTokenManager()
    with pytest.raises(oauth_mod.OAuthError, match="认证失败"):
        _run(mgr.get_token(client_id="c", client_secret="s", token_url="https://id/token"))


def test_missing_access_token_raises():
    FakeAsyncClient.responder = lambda u, d: FakeResponse(200, {"foo": "bar"})
    mgr = oauth_mod.OAuthTokenManager()
    with pytest.raises(oauth_mod.OAuthError):
        _run(mgr.get_token(client_id="c", client_secret="s", token_url="https://id/token"))


def test_missing_token_url_raises():
    mgr = oauth_mod.OAuthTokenManager()
    with pytest.raises(oauth_mod.OAuthError, match="token_url"):
        _run(mgr.get_token(client_id="c", client_secret="s", token_url=""))


def test_network_error_raises(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(oauth_mod.httpx, "AsyncClient", Boom)
    with pytest.raises(oauth_mod.OAuthError, match="无法连接"):
        _run(oauth_mod.manager.get_token(
            client_id="c", client_secret="s", token_url="https://id/token"))


def test_build_headers_static_and_bearer():
    cfg = {"headers": {"X-Trace": "abc"}, "bearer_token": "static-b"}
    h = _run(oauth_mod.build_http_headers(cfg))
    assert h["X-Trace"] == "abc"
    assert h["Authorization"] == "Bearer static-b"


def test_build_headers_oauth_overrides_bearer():
    FakeAsyncClient.responder = lambda u, d: FakeResponse(
        200, {"access_token": "oauth-tok", "expires_in": 3600})
    cfg = {
        "bearer_token": "static-b",
        "oauth": {"client_id": "c", "client_secret": "s",
                  "token_url": "https://id/token", "scope": "read"},
    }
    h = _run(oauth_mod.build_http_headers(cfg))
    assert h["Authorization"] == "Bearer oauth-tok"


def test_build_headers_empty_config():
    assert _run(oauth_mod.build_http_headers({})) == {}
