"""A browser may not operate the desktop API through an attacker origin/Host."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.local_api_guard import LocalAPIGuard


@pytest.fixture
def client():
    instance = FastAPI()
    instance.add_middleware(LocalAPIGuard, allow_development_origins=True)
    instance.state.calls = 0

    @instance.api_route("/api/sensitive", methods=["GET", "POST", "OPTIONS"])
    def sensitive():
        instance.state.calls += 1
        return {"ok": True}

    with TestClient(instance, base_url="http://127.0.0.1:8555", client=("127.0.0.1", 12345)) as test:
        yield test, instance


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example:8555"},
    {"Host": "localhost.attacker.example"},
    {"Host": "localhost@attacker.example"},
    {"Host": "attacker@localhost"},
    {"Host": "127.0.0.1:badport"},
    {"Origin": "https://attacker.example"},
    {"Origin": "null"},
    {"Origin": "http://127.0.0.1:9999"},
    {"Origin": "https://127.0.0.1:8555"},
    {"Origin": "http://127.0.0.1:8555/fake-path"},
    {"X-Forwarded-For": "testclient", "Host": "testserver"},
    {"X-Forwarded-Host": "localhost"},
    {"Forwarded": "for=127.0.0.1"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_invalid_request_is_rejected_before_side_effect(client, headers):
    transport, app = client
    assert transport.post("/api/sensitive", headers=headers).status_code == 403
    assert app.state.calls == 0


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://127.0.0.1:8555"},
                                       {"Origin": "http://localhost:5173"}])
def test_loopback_same_origin_and_declared_dev_origin_work(client, headers):
    transport, app = client
    assert transport.post("/api/sensitive", headers=headers).status_code == 200
    assert app.state.calls == 1


def test_remote_client_is_denied_even_with_local_host():
    called = []
    result = []

    async def inner(scope, receive, send):
        called.append(True)

    async def send(message):
        result.append(message)

    asyncio.run(LocalAPIGuard(inner)({"type": "http", "client": ("198.51.100.2", 5),
                                    "headers": [(b"host", b"127.0.0.1:8555")]}, None, send))
    assert not called and result[0]["status"] == 403


def test_websocket_hostile_origin_is_closed_before_accept():
    called = []
    result = []

    async def inner(scope, receive, send):
        called.append(True)

    async def send(message):
        result.append(message)

    asyncio.run(LocalAPIGuard(inner)({"type": "websocket", "client": ("127.0.0.1", 3),
                                    "headers": [(b"host", b"localhost:8555"),
                                                (b"origin", b"https://attacker.example")]}, None, send))
    assert not called and result == [{"type": "websocket.close", "code": 1008,
                                     "reason": "Untrusted local API request"}]


def test_fake_test_headers_do_not_enable_in_process_exception():
    app = FastAPI()
    app.add_middleware(LocalAPIGuard, test_transport=True)
    with TestClient(app, client=("127.0.0.1", 3)) as client:
        assert client.get("/api/sensitive", headers={"X-Forwarded-For": "testclient"}).status_code == 403


def test_production_guard_does_not_trust_vite_origin_by_default():
    instance = FastAPI()
    instance.add_middleware(LocalAPIGuard)
    @instance.get('/api/private')
    def private(): return {'ok':True}
    with TestClient(instance,base_url='http://127.0.0.1:8555',client=('127.0.0.1',12345)) as client:
        assert client.get('/api/private',headers={'Origin':'http://localhost:5173'}).status_code == 403
