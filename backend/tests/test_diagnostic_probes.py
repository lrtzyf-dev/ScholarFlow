"""No public network or real credentials: production adapters, MockTransport."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, diagnostics, diagnostic_probes, model_profiles
from app.call_ledger import list_calls
from app.db import SessionLocal
from app.main import app
from app.pipeline.embeddings import APIEmbedder, embedding_connection
from app.models import Conversation, ConversationOptions


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for key, value in {"llm_demo_mode": False, "llm_api_key": "synthetic-diagnostic-key",
                       "llm_base_url": "https://diagnostic.example.test/v1", "llm_model": "Case-Model",
                       "reasoning_effort": "", "embedding_api_key": "synthetic-embedding-key",
                       "embedding_base_url": "https://embedding.example.test/v1",
                       "embedding_model": "Case-Embed"}.items():
        monkeypatch.setattr(config.settings, key, value)


@pytest.fixture
def transport(monkeypatch):
    real_client = httpx.Client
    calls = []
    handler = [lambda request: httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}})]
    def dispatch(request):
        calls.append(request)
        assert any(row["state"] == "started" for row in list_calls()["items"])
        return handler[0](request)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(dispatch), **kwargs))
    return calls, handler


def target(service="llm", cid=None):
    return next(item for item in diagnostic_probes.probe_targets(cid) if item["service"] == service)


def test_local_report_never_calls_model_or_embedding(transport):
    calls, _ = transport
    report = diagnostics.run_diagnostics()
    assert report["scope"] == "local" and report["remote_requests"] == 0
    assert calls == [] and list_calls()["items"] == []
    assert all(item["available"] for item in report["probe_targets"])
    serialized = json.dumps(report)
    assert "synthetic-diagnostic-key" not in serialized and "synthetic-embedding-key" not in serialized


def test_each_local_failure_is_isolated_and_sanitized(monkeypatch):
    def fail():
        raise RuntimeError("synthetic-diagnostic-key private document body")
    monkeypatch.setattr(diagnostics, "_check_disk", fail)
    report = diagnostics.run_diagnostics()
    assert any(item["id"] == "disk" and item["status"] == "error" for item in report["checks"])
    assert any(item["id"] == "params" for item in report["checks"])
    assert "private document" not in json.dumps(report)


def test_explicit_probe_has_one_call_and_uses_reviewed_config(transport):
    calls, _ = transport
    preview = target()
    result = diagnostic_probes.run_probe("llm", preview["expected_config"])
    assert result["status"] == "ok" and len(calls) == len(result["calls"]) == 1
    body = json.loads(calls[0].content)
    assert body["model"] == "Case-Model"
    assert body["messages"] == [{"role": "user", "content": "Reply with OK."}]
    assert result["calls"][0]["usage_source"] == "actual"
    assert result["calls"][0]["amount"] is None
    assert "synthetic-diagnostic-key" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("llm_api_key", "changed"), ("llm_model", "Other"),
                                         ("llm_base_url", "https://other.example.test/v1")])
def test_changed_configuration_rejected_before_http(monkeypatch, transport, field, value):
    preview = target()
    monkeypatch.setattr(config.settings, field, value)
    with pytest.raises(diagnostic_probes.ProbeChanged):
        diagnostic_probes.run_probe("llm", preview["expected_config"])
    assert transport[0] == [] and list_calls()["items"] == []


def test_conversation_profile_is_tested_without_creating_or_changing_selection(transport):
    with SessionLocal() as db:
        row = model_profiles.save_profile(db, {"name": "local profile", "base_url": "http://localhost:9911/v1",
                                              "model_id": "Local-Case", "api_key": ""})
        db.add(Conversation(id="diagnostic-cid", title="Synthetic"))
        db.add(ConversationOptions(conversation_id="diagnostic-cid", model_profile_id=row.id,
                                   approval_mode="auto", revision=7))
        db.commit()
    preview = target(cid="diagnostic-cid")
    result = diagnostic_probes.run_probe("llm", preview["expected_config"], "diagnostic-cid")
    assert result["target"]["model_id"] == "Local-Case"
    assert "authorization" not in transport[0][0].headers
    with SessionLocal() as db:
        assert db.get(ConversationOptions, "diagnostic-cid").revision == 7


def test_demo_and_missing_key_are_not_remote_tests(monkeypatch, transport):
    for demo in (True, False):
        monkeypatch.setattr(config.settings, "llm_demo_mode", demo)
        monkeypatch.setattr(config.settings, "llm_api_key", "")
        preview = target()
        assert not preview["available"]
        with pytest.raises(diagnostic_probes.ProbeChanged):
            diagnostic_probes.run_probe("llm", preview["expected_config"])
    assert transport[0] == []


def test_remote_failure_has_durable_safe_receipt(transport):
    calls, handler = transport
    handler[0] = lambda request: httpx.Response(401, text="synthetic-diagnostic-key private provider response")
    result = diagnostic_probes.run_probe("llm", target()["expected_config"])
    assert result["status"] == "error" and len(calls) == 1
    assert result["calls"][0]["state"] == "failed"
    assert result["calls"][0]["usage_source"] == "unknown"
    assert "synthetic-diagnostic-key" not in json.dumps(result)
    assert "private provider" not in json.dumps(result)


def test_tool_only_response_is_not_successful_text_probe(transport):
    transport[1][0] = lambda request: httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "unrequested", "type": "function", "function": {
            "name": "invented", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]})
    result = diagnostic_probes.run_probe("llm", target()["expected_config"])
    assert result["status"] == "error" and len(transport[0]) == 1


def test_disconnected_mcp_is_not_reported_as_healthy(monkeypatch):
    from app.mcp_bridge import bridge
    monkeypatch.setattr(bridge, "list_servers", lambda: [{"name": "synthetic", "enabled": True,
                         "status": "disconnected", "error": "", "tool_count": 0}])
    check = diagnostics._check_mcp()
    assert check["status"] == "warn" and "未连接" in check["detail"]


def test_embedding_probe_uses_metered_production_adapter(transport):
    calls, handler = transport
    handler[0] = lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}],
                                                         "usage": {"prompt_tokens": 2, "total_tokens": 2}})
    result = diagnostic_probes.run_probe("embedding", target("embedding")["expected_config"])
    assert result["status"] == "ok" and len(calls) == 1
    assert calls[0].url.path == "/v1/embeddings"
    assert result["calls"][0]["service"] == "embedding"
    assert result["calls"][0]["usage"]["prompt_tokens"] == 2


def test_embedding_never_borrows_chat_key_across_origins(monkeypatch):
    monkeypatch.setattr(config.settings, "embedding_api_key", "")
    assert embedding_connection()["api_key"] == ""
    monkeypatch.setattr(config.settings, "embedding_base_url", "https://diagnostic.example.test/v1")
    assert embedding_connection()["api_key"] == "synthetic-diagnostic-key"


@pytest.mark.parametrize("items", [[{"index": 0, "embedding": []}], [{"index": 4, "embedding": [1]}],
                                  [{"index": 0, "embedding": [float("nan")]}]])
def test_invalid_embedding_records_failure(transport, items):
    transport[1][0] = lambda request: httpx.Response(200, text=json.dumps({"data": items}),
                                                   headers={"Content-Type": "application/json"})
    with pytest.raises(ValueError):
        APIEmbedder().encode(["ping"])
    assert list_calls()["items"][0]["state"] == "failed"


def test_bad_embedding_cannot_echo_provider_strings(transport):
    transport[1][0] = lambda request: httpx.Response(200, json={"data": [
        {"index": 0, "embedding": ["synthetic-secret-in-malformed-response"]}]})
    with pytest.raises(ValueError) as caught:
        APIEmbedder().encode(["ping"])
    assert "synthetic-secret" not in str(caught.value)


def test_api_get_is_local_and_explicit_probe_requires_fresh_target():
    with TestClient(app) as client:
        response = client.get("/api/debug/config")
        assert response.status_code == 200 and response.json()["remote_requests"] == 0
        assert client.get("/api/debug/probe").status_code in (404, 405)
        denied = client.post("/api/debug/probe", json={"service": "llm", "expected_config": "0" * 64})
        assert denied.status_code == 409
        assert list_calls()["items"] == []
