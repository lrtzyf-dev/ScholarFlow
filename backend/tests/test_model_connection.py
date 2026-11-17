"""Connection configuration, protocol parity, and credential destination regressions."""
import json

import httpx
import pytest
from fastapi import HTTPException

from app import config, diagnostics, llm
from app.api import settings as api
from app.model_connection import normalize_base_url, normalize_effort
from app.pipeline import embeddings


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    for name in (*config.RUNTIME_FIELDS, *config.RUNTIME_BOOL_FIELDS, *config.RUNTIME_FLOAT_FIELDS):
        monkeypatch.setattr(config.settings, name, getattr(config.settings, name))
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(llm, "_llm", None)
    monkeypatch.setattr(embeddings, "_embedder", None)
    config.settings.llm_base_url = "https://api.example.test/V1"
    config.settings.llm_demo_mode = False
    config.settings.llm_api_key = "CaseSensitive-Test-Key"
    config.settings.llm_model = "Org/Model-V1"
    config.settings.reasoning_effort = ""
    config.settings.embedding_base_url = ""
    config.settings.embedding_api_key = ""


@pytest.fixture
def transport(monkeypatch):
    real_client = httpx.Client
    calls = []
    handler = [lambda request: httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "OK"}}]})]

    def record(request):
        calls.append(request)
        return handler[0](request)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(record), **kwargs))
    return calls, handler


@pytest.mark.parametrize(("value", "expected"), [
    (" HTTPS://API.Example.Test:443/V1/ ", "https://api.example.test/V1"),
    ("hTtP://LOCALHOST:80/Proxy/v1/chat/completions/", "http://localhost/Proxy/v1"),
    ("https://EXAMPLE.test/Route%2FCase/v1", "https://example.test/Route%2FCase/v1"),
    ("http://[::1]:8080/v1/", "http://[::1]:8080/v1"),
    ("https://EXAMPLE.test", "https://example.test"),
    (" \n\t ", ""),
])
def test_url_case_matrix(value, expected):
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize("value", [
    "ftp://example.test/v1", "example.test/v1", "//example.test/v1", "https:///v1",
    "https://user:secret@example.test/v1", "https://@example.test/v1",
    "https://example.test/v1?api-version=preview", "https://example.test/v1?",
    "https://example.test/v1#anchor", "https://example.test/v1#",
    "https://exam\nple.test/v1", "https://example.test/v1\rInjected:bad",
    "https://example.test/a b", "https://example.test/a\\b", "https://example.test/%0A",
    "https://example.test:bad/v1", "https://example.test:70000/v1",
    "https://example.test/V1/CHAT/Completions", "https://example.test/V1/Responses/",
])
def test_invalid_url_rejected_before_request(value, transport):
    calls, _ = transport
    result = api.test_connection(api.TestIn(llm_base_url=value, llm_api_key="new-key"))
    assert result["ok"] is False
    assert calls == []


@pytest.mark.parametrize("effort", ["", "MiNiMaL", " LOW ", "Medium", "HiGh", "NONE", "xHIGH"])
def test_test_and_actual_chat_share_case_preserving_request(effort, transport):
    calls, _ = transport
    supplied = dict(llm_base_url=" HTTPS://API.EXAMPLE.TEST:443/V1/chat/completions/ ",
                    llm_api_key="  AbCd-Secret-XyZ  ", llm_model="  Org/Model-V1  ",
                    reasoning_effort=effort)
    assert api.test_connection(api.TestIn(**supplied))["ok"] is True
    config.update_settings(supplied)
    assert llm.APIClient().chat([{"role": "user", "content": "hello"}]).content == "OK"
    first, second = [json.loads(r.content) for r in calls]
    for body in (first, second):
        assert body["model"] == "Org/Model-V1"
        assert body["max_tokens"] == 8192  # Connection probes and actual requests share the compatibility output cap.
        if effort.strip():
            assert body["reasoning_effort"] == effort.strip().lower()
            assert "temperature" not in body
        else:
            assert body["temperature"] == 0.2
    assert {str(r.url) for r in calls} == {"https://api.example.test/V1/chat/completions"}
    assert {r.headers["authorization"] for r in calls} == {"Bearer AbCd-Secret-XyZ"}


@pytest.mark.parametrize("field", ["llm_api_key", "llm_model", "reasoning_effort"])
def test_embedded_control_characters_rejected_atomically(field, transport):
    calls, _ = transport
    before = config.runtime_settings()
    result = api.test_connection(api.TestIn(**{field: "valid\ninvalid"}))
    assert result["ok"] is False
    with pytest.raises(HTTPException) as exc:
        api.put_settings(api.SettingsIn(llm_base_url="https://api.example.test/Other", **{field: "valid\ninvalid"}))
    assert exc.value.status_code == 422
    assert config.runtime_settings() == before
    assert not config.CONFIG_PATH.exists()
    assert not calls


@pytest.mark.parametrize("base", ["https://other.example.test/V1", "http://api.example.test/V1",
                                  "https://api.example.test:444/V1"])
def test_origin_change_never_reuses_saved_key(base, transport):
    calls, _ = transport
    before = config.runtime_settings()
    result = api.test_connection(api.TestIn(llm_base_url=base))
    assert result["ok"] is False
    with pytest.raises(ValueError, match="API Key"):
        config.update_settings({"llm_base_url": base})
    assert config.runtime_settings() == before
    assert not config.CONFIG_PATH.exists()
    assert not calls


@pytest.mark.parametrize("key", ["new-Case-Key", ""])
def test_explicit_new_or_cleared_key_allows_origin_change(key, transport):
    calls, _ = transport
    values = {"llm_base_url": "https://other.example.test/Proxy/v1", "llm_api_key": key}
    assert api.test_connection(api.TestIn(**values))["ok"] is True
    config.update_settings(values)
    assert config.settings.llm_api_key == key
    assert calls[0].headers.get("authorization") == (f"Bearer {key}" if key else None)


def test_none_fallback_and_empty_values_are_distinct(transport):
    calls, _ = transport
    assert api.test_connection(api.TestIn())["ok"] is True
    assert calls[0].headers["authorization"] == "Bearer CaseSensitive-Test-Key"
    assert api.test_connection(api.TestIn(llm_api_key=""))["ok"] is True
    assert "authorization" not in calls[1].headers
    assert api.test_connection(api.TestIn(llm_base_url="", llm_api_key=""))["ok"] is False
    assert api.test_connection(api.TestIn(llm_model=""))["ok"] is False
    assert len(calls) == 2


def test_same_origin_path_change_keeps_key(transport):
    calls, _ = transport
    base = "HTTPS://API.EXAMPLE.TEST:443/Another/V1"
    assert api.test_connection(api.TestIn(llm_base_url=base))["ok"] is True
    config.update_settings({"llm_base_url": base})
    assert config.settings.llm_api_key == "CaseSensitive-Test-Key"
    assert str(calls[0].url) == "https://api.example.test/Another/V1/chat/completions"


@pytest.mark.parametrize(("code", "body"), [
    (302, "redirect"), (200, "<html>login</html>"), (204, ""),
    (200, {}), (200, {"choices": []}), (200, {"choices": [{}]}),
    (200, {"choices": [{"message": {}}]}), (200, {"choices": [{"message": {"content": 42}}]}),
])
def test_probe_rejects_non_chat_responses(code, body, transport):
    calls, handler = transport
    handler[0] = lambda request: httpx.Response(code, headers={"Location": "https://other.example.test"},
                                               **({"json": body} if isinstance(body, dict) else {"text": body}))
    assert api.test_connection(api.TestIn())["ok"] is False
    assert len(calls) == 1


def test_provider_error_cannot_echo_credentials(transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(400, text=request.headers["authorization"])
    result = api.test_connection(api.TestIn())
    assert not result["ok"]
    assert config.settings.llm_api_key not in result["message"]


def test_stream_usage_only_chunk_preserves_answer_and_usage(transport):
    calls, handler = transport
    payloads = [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " world"}}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
    ]
    wire = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads) + "data: [DONE]\n\n"
    handler[0] = lambda request: httpx.Response(200, text=wire, headers={"Content-Type": "text/event-stream"})
    events = list(llm.APIClient().stream_chat([{"role": "user", "content": "hi"}]))
    final = events[-1][1]
    assert final.content == "Hello world"
    assert final.usage["total_tokens"] == 5
    assert json.loads(calls[0].content)["stream_options"] == {"include_usage": True}


def test_cleared_values_survive_reload_and_embedding_key_is_masked():
    config.update_settings({"llm_api_key": "New-Key-AbCd", "embedding_api_key": "Embed-Key-ZyXw"})
    visible = config.runtime_settings()
    assert visible["llm_api_key_set"] is True
    assert visible["embedding_api_key_set"] is True
    assert visible["llm_api_key"] != "New-Key-AbCd"
    assert visible["embedding_api_key"] != "Embed-Key-ZyXw"
    config.update_settings({"llm_api_key": "", "llm_base_url": "", "llm_model": "",
                            "embedding_api_key": "", "reasoning_effort": ""})
    config.settings.llm_api_key = "ENV-KEY"
    config.settings.llm_base_url = "https://env.example.test/V1"
    config.settings.llm_model = "ENV-MODEL"
    config.settings.embedding_api_key = "ENV-EMBED-KEY"
    config.settings.reasoning_effort = "high"
    config._load_config_file()
    assert config.settings.llm_api_key == config.settings.llm_base_url == config.settings.llm_model == ""
    assert config.settings.embedding_api_key == config.settings.reasoning_effort == ""
    visible = config.runtime_settings()
    assert visible["llm_api_key_set"] is visible["embedding_api_key_set"] is False
    assert visible["llm_mode"] == "unconfigured"


def test_embedding_inherited_origin_change_requires_new_embedding_key():
    config.settings.embedding_api_key = "Embed-Only-Key"
    with pytest.raises(ValueError, match="Embedding"):
        config.update_settings({"llm_base_url": "https://other.example.test/v1", "llm_api_key": "new-key"})
    config.update_settings({"llm_base_url": "https://other.example.test/v1", "llm_api_key": "new-key",
                            "embedding_api_key": ""})
    assert config.settings.embedding_api_key == ""


def test_explicit_embedding_origin_change_requires_key():
    config.settings.embedding_api_key = "Embed-Only-Key"
    with pytest.raises(ValueError, match="Embedding"):
        config.update_settings({"embedding_base_url": "https://embedding.example.test/v1"})


def test_embedding_case_and_full_endpoint(transport):
    calls, handler = transport
    handler[0] = lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.2, 0.4]}]})
    config.update_settings({"embedding_base_url": " HTTPS://Embed.Example.Test/Proxy/V1/embeddings/ ",
                            "embedding_api_key": "  EmbED-Key  ", "embedding_model": "  Org/EmbED-v3  "})
    vector = embeddings.APIEmbedder().encode(["test"])
    assert vector.shape == (1, 2)
    assert str(calls[0].url) == "https://embed.example.test/Proxy/V1/embeddings"
    assert calls[0].headers["authorization"] == "Bearer EmbED-Key"
    assert json.loads(calls[0].content)["model"] == "Org/EmbED-v3"


def test_embedding_endpoint_requires_exact_case(transport):
    calls, _ = transport
    with pytest.raises(ValueError, match="大小写"):
        config.update_settings({"embedding_base_url": "https://embed.example.test/V1/Embeddings"})
    assert not calls


def test_env_case_and_loaded_config_share_normalization(monkeypatch):
    monkeypatch.setenv("lLm_BaSe_Url", " HTTPS://API.Example.Test/Custom/V1/chat/completions/ ")
    monkeypatch.setenv("LlM_MoDeL", "  Org/MixedCASE-v2  ")
    monkeypatch.setenv("lLm_ApI_KeY", "  CaseSensitive-Key  ")
    monkeypatch.setenv("REASONING_EFFORT", " HIGH ")
    from_env = config.Settings(_env_file=None)
    assert from_env.llm_base_url == "https://api.example.test/Custom/V1"
    assert from_env.llm_model == "Org/MixedCASE-v2"
    assert from_env.llm_api_key == "CaseSensitive-Key"
    assert from_env.reasoning_effort == "high"
    config.CONFIG_PATH.write_text(json.dumps({"llm_base_url": "HTTPS://API.EXAMPLE.TEST/Mixed/V1/",
                                               "llm_model": "  Org/CamelModel ", "reasoning_effort": "LOW"}), encoding="utf-8")
    config._load_config_file()
    assert config.settings.llm_base_url == "https://api.example.test/Mixed/V1"
    assert config.settings.llm_model == "Org/CamelModel"
    assert config.settings.reasoning_effort == "low"


def test_invalid_legacy_config_can_be_corrected_without_startup_failure(transport):
    calls, _ = transport
    config.CONFIG_PATH.write_text(json.dumps({
        "llm_base_url": "https://api.example.test/v1?wrong=query",
        "reasoning_effort": "unknown-old-option",
    }), encoding="utf-8")
    config._load_config_file()
    visible = config.runtime_settings()
    assert visible["llm_base_url"] == "https://api.example.test/v1?wrong=query"
    assert visible["reasoning_effort"] == "unknown-old-option"
    assert api.test_connection(api.TestIn())["ok"] is False
    assert not calls
    config.update_settings({"llm_base_url": "https://api.example.test/v1", "reasoning_effort": "HIGH"})
    assert api.test_connection(api.TestIn())["ok"] is True


def test_diagnostics_accepts_uppercase_scheme_and_effort(transport):
    calls, _ = transport
    config.settings.llm_base_url = "HTTPS://API.EXAMPLE.TEST/V1"
    config.settings.reasoning_effort = " HiGh "
    assert diagnostics._check_llm()["status"] == "ok"
    assert all(check["id"] != "param_effort" for check in diagnostics._check_params())
    assert calls == []  # Local diagnostics no longer perform a paid request.
    from app.diagnostic_probes import probe_targets, run_probe
    preview = next(item for item in probe_targets() if item["service"] == "llm")
    assert run_probe("llm", preview["expected_config"])["status"] == "ok"
    assert json.loads(calls[0].content)["reasoning_effort"] == "high"
    assert normalize_effort(" HiGh ") == "high"
