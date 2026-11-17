"""Model discovery works before selecting a model and never forwards saved keys across origins."""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.api import settings as api


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config.settings, "llm_base_url", "https://api.example.test/Proxy/V1")
    monkeypatch.setattr(config.settings, "llm_api_key", "Saved-CaseSensitive-Key")
    # Discovery must not depend on having already selected a valid model or effort.
    monkeypatch.setattr(config.settings, "llm_model", "")
    monkeypatch.setattr(config.settings, "reasoning_effort", "legacy-unsupported-effort")
    before = config.settings.model_dump()
    yield
    assert config.settings.model_dump() == before
    assert not config.CONFIG_PATH.exists()


@pytest.fixture
def transport(monkeypatch):
    real_client = httpx.Client
    calls = []
    handler = [lambda request: httpx.Response(200, json={"data": [{"id": "Org/Model-v1"}]})]

    def record(request):
        calls.append(request)
        return handler[0](request)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(record), **kwargs))
    return calls, handler


def test_registered_endpoint_fetches_models_before_selection(transport):
    calls, handler = transport
    handler[0] = lambda request: httpx.Response(200, json={"data": [
        {"id": "deepseek-v4-pro", "owned_by": "deepseek"},
        {"id": "deepseek-flash", "owned_by": "deepseek"},
    ]})
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        response = client.post("/api/settings/models", json={
            "llm_base_url": " HTTPS://API.EXAMPLE.TEST:443/Proxy/V1/chat/completions/ ",
            "llm_api_key": "  New-Case-Key  ",
        })
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["models"] == [
        {"id": "deepseek-flash", "owned_by": "deepseek"},
        {"id": "deepseek-v4-pro", "owned_by": "deepseek"},
    ]
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert str(calls[0].url) == "https://api.example.test/Proxy/V1/models"
    assert calls[0].headers["authorization"] == "Bearer New-Case-Key"
    assert calls[0].content == b""


@pytest.mark.parametrize("payload", [{}, {"llm_api_key": None, "llm_base_url": None}])
def test_omitted_or_null_values_use_saved_connection(payload, transport):
    calls, _ = transport
    assert api.get_models(api.ModelsIn(**payload))["ok"] is True
    assert str(calls[0].url) == "https://api.example.test/Proxy/V1/models"
    assert calls[0].headers["authorization"] == "Bearer Saved-CaseSensitive-Key"


def test_same_origin_path_change_preserves_saved_key_without_inventing_v1(transport):
    calls, _ = transport
    assert api.get_models(api.ModelsIn(llm_base_url="HTTPS://API.EXAMPLE.TEST:443"))["ok"]
    assert str(calls[0].url) == "https://api.example.test/models"
    assert calls[0].headers["authorization"] == "Bearer Saved-CaseSensitive-Key"


@pytest.mark.parametrize("key", ["", "New-Exact-Key"])
def test_explicit_empty_or_new_key_can_be_used_at_another_origin(key, transport):
    calls, _ = transport
    assert api.get_models(api.ModelsIn(llm_base_url="https://other.example.test/v1", llm_api_key=key))["ok"]
    assert str(calls[0].url) == "https://other.example.test/v1/models"
    assert calls[0].headers.get("authorization") == (f"Bearer {key}" if key else None)


@pytest.mark.parametrize("base", [
    "https://other.example.test/v1", "http://api.example.test/Proxy/V1",
    "https://api.example.test:444/Proxy/V1",
])
@pytest.mark.parametrize("key_field", [{}, {"llm_api_key": None}])
def test_changed_origin_cannot_receive_omitted_saved_key(base, key_field, transport):
    calls, _ = transport
    result = api.get_models(api.ModelsIn(llm_base_url=base, **key_field))
    assert result["ok"] is False
    assert result["models"] == []
    assert "API Key" in result["message"]
    assert calls == []


@pytest.mark.parametrize("base", [
    "", "not-a-url", "ftp://api.example.test/v1", "https://api.example.test:bad/v1",
    "https://user:password@api.example.test/v1", "https://api.example.test/v1?secret=hidden",
    "https://api.example.test/V1/CHAT/Completions", "https://api.example.test/v1\nInjected: yes",
])
def test_invalid_base_never_sends_credentials(base, transport):
    calls, _ = transport
    result = api.get_models(api.ModelsIn(llm_base_url=base, llm_api_key="Supplied-Key"))
    assert result["ok"] is False
    assert result["models"] == []
    assert calls == []
    assert "Supplied-Key" not in result["message"]


def test_invalid_key_never_makes_request(transport):
    calls, _ = transport
    result = api.get_models(api.ModelsIn(llm_api_key="Key\r\nInjected: yes"))
    assert result["ok"] is False
    assert "API Key" in result["message"]
    assert calls == []


def test_model_ids_keep_exact_case_and_paths_with_exact_deduplication(transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(200, json={"data": [
        {"id": "zeta"}, {"id": "Org/Model-V1", "owned_by": "Provider-A"},
        {"id": "alpha"}, {"id": "org/model-v1"},
        {"id": "Org/Model-V1", "owned_by": "Duplicate"}, {"id": "ALPHA"},
    ]})
    result = api.get_models(api.ModelsIn())
    assert result["ok"] is True
    assert result["models"] == [
        {"id": "ALPHA"}, {"id": "alpha"},
        {"id": "Org/Model-V1", "owned_by": "Provider-A"},
        {"id": "org/model-v1"}, {"id": "zeta"},
    ]


@pytest.mark.parametrize("body", [
    [], {}, {"data": {}}, {"data": ["model"]}, {"data": [{}]},
    {"data": [{"id": 42}]}, {"data": [{"id": ""}]}, {"data": [{"id": "   "}]},
    {"data": [{"id": " leading"}]}, {"data": [{"id": "Org/Model\nInjected"}]},
    {"data": [{"id": "Valid-Model"}], "error": {"message": "upstream error"}},
    {"data": [{"id": "Saved-CaseSensitive-Key"}]},
])
def test_invalid_schema_or_echoed_credentials_never_becomes_a_choice(body, transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(200, json=body)
    result = api.get_models(api.ModelsIn())
    assert result["ok"] is False
    assert result["models"] == []
    assert "Saved-CaseSensitive-Key" not in json.dumps(result)


def test_only_model_fields_are_returned_and_owner_cannot_echo_key(transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(200, json={"data": [{
        "id": "Model-V1", "owned_by": "Bearer Saved-CaseSensitive-Key",
        "private": "Saved-CaseSensitive-Key", "created": 123,
    }]})
    result = api.get_models(api.ModelsIn())
    assert result["ok"] is True
    assert result["models"] == [{"id": "Model-V1"}]
    assert "Saved-CaseSensitive-Key" not in json.dumps(result)


def test_empty_list_has_manual_entry_fallback(transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(200, json={"data": []})
    result = api.get_models(api.ModelsIn())
    assert result["ok"] is False
    assert result["models"] == []
    assert "手动" in result["message"]


@pytest.mark.parametrize("status", [200, 204])
def test_non_json_result_is_not_treated_as_an_empty_success(status, transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(status, text="<html>Saved-CaseSensitive-Key</html>")
    result = api.get_models(api.ModelsIn())
    assert result["ok"] is False
    assert "JSON" in result["message"]
    assert "Saved-CaseSensitive-Key" not in json.dumps(result)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 402, 403, 404, 405, 429, 500, 501])
def test_upstream_errors_are_safe_and_redirects_are_not_followed(status, transport):
    calls, handler = transport
    handler[0] = lambda request: httpx.Response(status, headers={
        "Location": "https://other.example.test/models",
    }, json={"error": {"message": "Bearer Saved-CaseSensitive-Key"}})
    result = api.get_models(api.ModelsIn())
    assert result["ok"] is False
    assert result["models"] == []
    assert "Saved-CaseSensitive-Key" not in json.dumps(result)
    assert len(calls) == 1
    assert calls[0].url.host == "api.example.test"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_network_failures_have_safe_messages(error_type, transport):
    _, handler = transport

    def fail(request):
        raise error_type("Saved-CaseSensitive-Key", request=request)

    handler[0] = fail
    result = api.get_models(api.ModelsIn())
    assert result["ok"] is False
    assert result["models"] == []
    assert "Saved-CaseSensitive-Key" not in json.dumps(result)
