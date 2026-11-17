"""Connection diagnostics are requests too; no contents belong in their ledger."""
import httpx
import pytest

from app import config, llm
from app.call_ledger import list_calls
from app.model_connection import discover_models, probe_chat


@pytest.fixture
def transport(monkeypatch):
    original = httpx.Client
    handler = [lambda request: httpx.Response(200, json={"data": []})]
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: original(
        *args, **kwargs, transport=httpx.MockTransport(lambda request: handler[0](request))))
    return handler


def test_model_list_protocol_errors_are_failed_calls_and_do_not_store_provider_body(transport):
    transport[0] = lambda request: httpx.Response(200, json={"data": "private-malformed-body"})
    with pytest.raises(ValueError):
        discover_models("https://test.invalid/v1", "Synthetic-Secret")
    row, = list_calls()["items"]
    assert row["state"] == "failed" and row["service"] == "model_discovery"
    assert row["usage_source"] == "unknown" and row["error_kind"] == "ValueError"
    assert "private-malformed-body" not in str(row)
    assert "Synthetic-Secret" not in str(row)


def test_probe_records_exact_model_id_and_supplied_usage(transport):
    transport[0] = lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "OK"}}], "usage": {"total_tokens": 4}})
    probe_chat("https://test.invalid/v1", "Synthetic-Key", " Mixed Case (Test) ", "")
    row, = list_calls()["items"]
    assert row["state"] == "completed" and row["service"] == "model_probe"
    assert row["model_id"] == "Mixed Case (Test)" and row["usage"] == {"total_tokens": 4}
    assert row["amount"] is None


@pytest.mark.parametrize("change", ["key", "endpoint"])
def test_configuration_change_does_not_share_failed_request_retry_identity(transport, monkeypatch, change):
    monkeypatch.setattr(config.settings, "llm_base_url", "https://test.invalid/v1")
    monkeypatch.setattr(config.settings, "llm_model", "Exact-Case")
    monkeypatch.setattr(config.settings, "llm_api_key", "Synthetic-First-Key")
    transport[0] = lambda request: httpx.Response(503)
    client = llm.APIClient()
    for index in range(2):
        if index == 1:
            monkeypatch.setattr(config.settings, "llm_api_key" if change == "key" else "llm_base_url",
                                "Synthetic-Second-Key" if change == "key" else "https://other.invalid/v1")
        with pytest.raises(httpx.HTTPStatusError):
            client.chat([{"role": "user", "content": "same-input"}])
    rows = list_calls()["items"]
    assert len(rows) == 2 and all(row["retry_of_call_id"] is None for row in rows)
    assert len({row["request_id"] for row in rows}) == 2
