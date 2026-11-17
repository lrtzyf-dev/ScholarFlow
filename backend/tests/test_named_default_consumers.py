"""Standalone research screens honor named defaults without legacy secrets."""
import json
import httpx
import pytest

from app import config, llm, model_profiles
from app.db import SessionLocal
from app.models import ModelCredential
from app.runtime_context import bind_runtime_config
from app.translate.translator import translate_text
from app.writing.framework import build_framework


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "")
    monkeypatch.setattr(config.settings, "llm_demo_mode", False)
    monkeypatch.setattr(llm, "_llm", None)


def choose(model="Named-Case"):
    with SessionLocal() as db:
        row = model_profiles.save_profile(db, {"name": model, "base_url": "http://localhost:9982/v1",
                                              "model_id": model, "api_key": ""})
        model_profiles.set_default(db, row.id)
        db.commit()
        return row.id


@pytest.fixture
def transport(monkeypatch):
    real = httpx.Client
    calls = []
    content = ["A synthetic translation."]
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content[0]},
                                                       "finish_reason": "stop"}]})
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    return calls, content


def test_translate_uses_named_default_without_global_key(transport):
    choose()
    result = translate_text("这是合成测试文字。")
    assert result["mode"] == "llm"
    assert result["translation"] == "A synthetic translation."
    assert json.loads(transport[0][0].content)["model"] == "Named-Case"
    with SessionLocal() as db:
        assert db.query(ModelCredential).count() == 0, "keyless capture must not persist unused credential copies"


def test_framework_uses_named_default_without_legacy_key(db, transport):
    choose()
    transport[1][0] = '{"sections": [{"key":"abstract","bullets":[]},{"key":"introduction","bullets":[]},{"key":"related_work","bullets":[]}]}'
    result = build_framework(db, "合成研究提纲")
    assert result["mode"] != "offline"
    assert json.loads(transport[0][0].content)["model"] == "Named-Case"


def test_returned_client_keeps_selected_profile_when_default_changes(transport):
    choose("First-Case")
    first = llm.get_llm()
    choose("Second-Case")
    second = llm.get_llm()
    first.chat([{"role": "user", "content": "test"}])
    second.chat([{"role": "user", "content": "test"}])
    assert [json.loads(request.content)["model"] for request in transport[0]] == ["First-Case", "Second-Case"]


def test_run_binding_still_wins_over_named_default(transport):
    choose()
    with bind_runtime_config({"version": 1, "mode": "mock", "model_id": "offline-demo"}):
        assert isinstance(llm.get_llm(), llm.MockLLM)
    assert transport[0] == []


def test_explicit_default_demo_does_not_require_global_credentials():
    with SessionLocal() as db:
        model_profiles.set_default(db, model_profiles.DEMO_PROFILE_ID)
        db.commit()
    assert isinstance(llm.get_llm(), llm.MockLLM)


def test_disabled_named_default_fails_instead_of_falling_back():
    identity = choose()
    with SessionLocal() as db:
        row = model_profiles.get_profile(db, identity)
        row.enabled = 0
        db.commit()
    with pytest.raises(model_profiles.ModelConfigurationError):
        llm.get_llm()


def test_health_displays_named_default_instead_of_legacy_status():
    from app.main import health
    identity = choose()
    assert health()["llm_mode"] == "api" and health()["llm_model"] == "Named-Case"
    with SessionLocal() as db:
        model_profiles.get_profile(db, identity).enabled = 0
        db.commit()
    assert health()["llm_mode"] == "unconfigured"


def test_named_to_legacy_race_never_creates_rolled_back_credential(monkeypatch, transport):
    selected = choose()
    original = model_profiles.default_profile_id
    reads = []
    def switch_after_read(db):
        chosen = original(db)
        reads.append(chosen)
        with SessionLocal() as other:
            model_profiles.set_default(other, model_profiles.LEGACY_PROFILE_ID)
            other.commit()
        return chosen
    monkeypatch.setattr(model_profiles, "default_profile_id", switch_after_read)
    monkeypatch.setattr(config.settings, "llm_api_key", "synthetic-legacy-secret")
    client = llm.get_llm()
    assert reads == [selected]
    assert client._runtime_config["profile_id"] == selected
    with SessionLocal() as db:
        assert db.query(ModelCredential).count() == 0
    client.chat([{"role": "user", "content": "synthetic"}])
    assert json.loads(transport[0][0].content)["model"] == "Named-Case"


def test_standalone_selection_cannot_override_conversation(db):
    with pytest.raises(model_profiles.ModelConfigurationError):
        model_profiles.capture_run_config(db, "conversation", selected_profile_id="another-profile")
