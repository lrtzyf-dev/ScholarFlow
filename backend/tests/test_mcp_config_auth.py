"""MCP settings preserve authenticated HTTP config without persisting secrets."""
import base64
import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from app.api import mcp as mcp_api
from app.credential_protection import CredentialProtectionError
from app.mcp_bridge import MCPBridge, _redact_error, _runtime_config


def _fake_protection(monkeypatch):
    import app.credential_protection as protection

    monkeypatch.setattr(protection, "protect", lambda value: "dpapi:" + base64.b64encode(
        value.encode("utf-8")).decode("ascii"))
    monkeypatch.setattr(protection, "unprotect", lambda value: base64.b64decode(
        value[6:], validate=True).decode("utf-8"))


def _bridge(monkeypatch, path):
    import app.mcp_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "MCP_CONFIG_PATH", path)
    bridge = MCPBridge.__new__(MCPBridge)
    bridge._loop = None
    bridge._thread = None
    bridge._conns = {}
    bridge._disabled_builtin = set()
    bridge._lock = __import__("threading").Lock()
    bridge._builtin_servers = {}
    return bridge


def test_settings_api_accepts_http_auth_fields_and_bridge_seals_them(tmp_path, monkeypatch):
    _fake_protection(monkeypatch)
    bridge = _bridge(monkeypatch, tmp_path / "mcp_servers.json")
    monkeypatch.setattr(mcp_api, "bridge", bridge)
    secrets = ["Bearer-Case-Sensitive-Token", "Synthetic-Client-Secret", "X-Lab-Secret"]
    payload = mcp_api.ServerIn(
        name="remote-lab", transport="http", enabled=False,
        url="https://mcp.example.invalid/mcp",
        headers={"X-Lab": secrets[2]}, bearer_token=secrets[0],
        oauth={"client_id": "lab-client", "client_secret": secrets[1],
               "token_url": "https://id.example.invalid/token", "scope": "read"},
    )

    response = mcp_api.upsert_server(payload)
    disk_text = (tmp_path / "mcp_servers.json").read_text(encoding="utf-8")

    assert response["name"] == "remote-lab"
    assert all(secret not in json.dumps(response) for secret in secrets)
    assert all(secret not in disk_text for secret in secrets)
    assert "auth_protected" in disk_text
    assert "bearer_token" not in disk_text and "client_secret" not in disk_text
    assert _runtime_config(bridge._conns["remote-lab"].config)["bearer_token"] == secrets[0]

    # Reopening the saved configuration still supplies the credentials only at
    # the runtime boundary, never through the public server listing.
    reopened = _bridge(monkeypatch, tmp_path / "mcp_servers.json")
    reopened._load()
    runtime = _runtime_config(reopened._conns["remote-lab"].config)
    assert runtime["headers"] == {"X-Lab": secrets[2]}
    assert runtime["oauth"]["client_secret"] == secrets[1]
    public = json.dumps(reopened.list_servers())
    assert all(secret not in public for secret in secrets)
    assert secrets[0] not in _redact_error(RuntimeError(secrets[0]),
                                            reopened._conns["remote-lab"].config)


def test_http_transport_builds_headers_from_decrypted_runtime_config(tmp_path, monkeypatch):
    _fake_protection(monkeypatch)
    bridge = _bridge(monkeypatch, tmp_path / "mcp_servers.json")
    bridge.upsert_server({"name": "remote", "transport": "http", "enabled": False,
                          "url": "https://mcp.example.invalid/mcp",
                          "headers": {"X-Lab": "Header-Value"},
                          "bearer_token": "Static-Token",
                          "oauth": {"client_id": "client", "client_secret": "Secret-Value",
                                    "token_url": "https://id.example.invalid/token"}})
    runtime = _runtime_config(bridge._conns["remote"].config)

    import mcp.client.streamable_http as transport
    import app.mcp_oauth as oauth

    observed = {}

    class FakeClient:
        def __init__(self, headers):
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    @asynccontextmanager
    async def fake_streamable_http_client(url, http_client):
        observed["url"] = url
        observed["headers"] = http_client.headers
        yield (None, None, None)

    async def fake_get_token(**kwargs):
        observed["oauth_secret"] = kwargs["client_secret"]
        return "OAuth-Access-Token"

    monkeypatch.setattr(transport, "create_mcp_http_client", lambda headers=None: FakeClient(headers or {}))
    monkeypatch.setattr(transport, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(oauth.manager, "get_token", fake_get_token)

    async def connect():
        async with bridge._http_transport(runtime):
            pass

    asyncio.run(connect())

    assert observed["url"] == runtime["url"]
    assert observed["headers"]["X-Lab"] == "Header-Value"
    assert observed["headers"]["Authorization"] == "Bearer OAuth-Access-Token"
    assert observed["oauth_secret"] == "Secret-Value"


def test_partial_server_edit_preserves_auth_but_explicit_empty_clears_it(tmp_path, monkeypatch):
    _fake_protection(monkeypatch)
    bridge = _bridge(monkeypatch, tmp_path / "mcp_servers.json")
    bridge.upsert_server({"name": "lab", "transport": "http", "enabled": False,
                          "url": "https://mcp.example.invalid/mcp",
                          "bearer_token": "Original-Token"})

    bridge.upsert_server({"name": "lab", "enabled": False, "url": "https://mcp.example.invalid/mcp"})
    assert _runtime_config(bridge._conns["lab"].config)["bearer_token"] == "Original-Token"

    bridge.upsert_server({"name": "lab", "enabled": False, "bearer_token": ""})
    assert "bearer_token" not in _runtime_config(bridge._conns["lab"].config)


def test_legacy_plaintext_auth_is_migrated_and_protected(tmp_path, monkeypatch):
    _fake_protection(monkeypatch)
    path = tmp_path / "mcp_servers.json"
    secret = "Legacy-Header-Token"
    path.write_text(json.dumps({"servers": [{
        "name": "legacy", "transport": "http", "enabled": False,
        "url": "https://mcp.example.invalid/mcp", "headers": {"Authorization": secret},
    }]}), encoding="utf-8")
    bridge = _bridge(monkeypatch, path)

    bridge._load()

    persisted = path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "auth_protected" in persisted
    assert _runtime_config(bridge._conns["legacy"].config)["headers"]["Authorization"] == secret


def test_legacy_credentials_are_not_rewritten_if_system_protection_is_unavailable(tmp_path, monkeypatch):
    import app.credential_protection as protection

    path = tmp_path / "mcp_servers.json"
    secret = "Legacy-Token-Value"
    original = json.dumps({"servers": [{"name": "legacy", "bearer_token": secret}]})
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(protection, "protect", lambda value: (_ for _ in ()).throw(
        CredentialProtectionError("unsupported")))
    bridge = _bridge(monkeypatch, path)
    bridge._load()

    with pytest.raises(CredentialProtectionError):
        bridge._persist()
    assert path.read_text(encoding="utf-8") == original

