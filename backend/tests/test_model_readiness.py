"""Cloud configuration, explicit demo and keyless local APIs stay distinct."""
import json

import pytest
from fastapi.testclient import TestClient

from app import config, llm, model_profiles
from app.main import app
from app.models import Conversation, ConversationOptions, DesktopState, ModelProfile
from app.runtime_context import bind_runtime_config


client = TestClient(app)


@pytest.fixture(autouse=True)
def no_implicit_demo(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_demo_mode", False)
    monkeypatch.setattr(config.settings, "llm_api_key", "")
    monkeypatch.setattr(config.settings, "llm_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(config.settings, "llm_model", "deepseek-flash")
    llm.reset_llm()
    yield
    llm.reset_llm()


def test_new_cloud_default_is_unconfigured_not_a_demo(db):
    assert config.settings.llm_mode == "unconfigured"
    catalog = client.get("/api/model-profiles").json()
    default = next(p for p in catalog["profiles"] if p["id"] == catalog["default_profile_id"])
    assert default["model_id"] == "deepseek-flash"
    assert default["mode"] == "unconfigured"
    assert "演示" not in default["name"]
    options = client.get("/api/conversation-options/new-cloud").json()
    assert "密钥" in options["model_error"]
    with pytest.raises(model_profiles.ModelConfigurationError, match="尚未配置"):
        model_profiles.capture_run_config(db, "new-cloud")
    assert db.get(ConversationOptions, "new-cloud") is None
    with pytest.raises(model_profiles.ModelConfigurationError, match="尚未配置"):
        llm.get_llm()


def test_explicit_demo_selection_is_durable_and_does_not_change_other_conversations(db):
    result = client.put("/api/conversation-options/demo-chat", json={
        "expected_revision": 0, "model_profile_id": model_profiles.DEMO_PROFILE_ID,
    })
    assert result.status_code == 200
    assert result.json()["effective_model"]["mode"] == "mock"
    binding = model_profiles.capture_run_config(db, "demo-chat")
    db.commit()
    assert binding["mode"] == "mock" and binding["credential_ref"] is None
    assert model_profiles.resolve_model_config(binding)["api_key"] == ""
    assert client.get("/api/conversation-options/other-chat").json()["effective_model"]["mode"] == "unconfigured"
    db.expire_all()
    assert model_profiles.capture_run_config(db, "demo-chat")["profile_id"] == model_profiles.DEMO_PROFILE_ID
    with bind_runtime_config(binding):
        assert isinstance(llm.get_llm(), llm.MockLLM)
    assert client.post("/api/model-profiles/offline-demo/test").json()["ok"] is False
    assert client.put("/api/model-profiles/offline-demo", json={"expected_version": 1, "name": "alter"}).status_code == 422


@pytest.mark.parametrize("base", ["http://127.0.0.1:11434/v1", "http://localhost:8080/v1", "http://[::1]:8080/v1", "http://192.168.1.8:8000/v1"])
def test_user_configured_keyless_local_profile_uses_real_api(db, base):
    created = client.post("/api/model-profiles", json={"name": "My local", "base_url": base, "model_id": "Local-Case"})
    assert created.status_code == 200
    profile = created.json()
    assert profile["mode"] == "api" and not profile["api_key_set"]
    assert client.put("/api/conversation-options/local-chat", json={
        "expected_revision": 0, "model_profile_id": profile["id"],
    }).status_code == 200
    binding = model_profiles.capture_run_config(db, "local-chat")
    db.commit()
    with bind_runtime_config(binding):
        assert isinstance(llm.get_llm(), llm.APIClient)
    resolved = model_profiles.resolve_model_config(binding)
    assert resolved["api_key"] == "" and resolved["model_id"] == "Local-Case"


def test_named_cloud_profile_requires_credential_without_becoming_demo(db):
    profile = client.post("/api/model-profiles", json={
        "name": "Cloud", "base_url": "https://cloud.example/v1", "model_id": "Cloud-CaSe",
    }).json()
    assert profile["mode"] == "unconfigured"
    client.put("/api/conversation-options/cloud-chat", json={
        "expected_revision": 0, "model_profile_id": profile["id"],
    })
    with pytest.raises(model_profiles.ModelConfigurationError, match="密钥"):
        model_profiles.capture_run_config(db, "cloud-chat")
    db.rollback()
    updated = client.put(f"/api/model-profiles/{profile['id']}", json={
        "expected_version": 1, "api_key": "Synthetic-Readiness-CaSe-123",
    })
    assert updated.json()["mode"] == "api"
    assert "Synthetic-Readiness" not in updated.text
    binding = model_profiles.capture_run_config(db, "cloud-chat")
    db.commit()
    assert binding["mode"] == "api"


def test_settings_demo_mode_requires_explicit_opt_in_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    assert config.runtime_settings()["llm_demo_mode"] is False
    response = client.put("/api/settings", json={"llm_demo_mode": True})
    assert response.status_code == 200 and response.json()["llm_mode"] == "mock"
    assert json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))["llm_demo_mode"] is True
    assert isinstance(llm.get_llm(), llm.MockLLM)
    response = client.put("/api/settings", json={"llm_demo_mode": False})
    assert response.json()["llm_mode"] == "unconfigured"
    with pytest.raises(model_profiles.ModelConfigurationError):
        llm.get_llm()


def test_frozen_legacy_mock_is_preserved_but_invalid_mode_never_falls_back():
    old = {"version": 1, "profile_id": "legacy-default", "mode": "mock", "model_id": "Old-demo"}
    assert model_profiles.resolve_model_config(old)["mode"] == "mock"
    with bind_runtime_config(old):
        assert isinstance(llm.get_llm(), llm.MockLLM)
    with bind_runtime_config({"version": 1, "mode": "unconfigured"}):
        with pytest.raises(model_profiles.ModelConfigurationError):
            llm.get_llm()


def test_new_settings_without_environment_never_auto_enables_demo(monkeypatch):
    monkeypatch.delenv("LLM_DEMO_MODE", raising=False)
    fresh = config.Settings(_env_file=None, llm_api_key="")
    assert fresh.llm_demo_mode is False and fresh.llm_mode == "unconfigured"


def test_new_install_bootstraps_editable_default_once_and_preserves_later_choices(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    config.initialize_installation_defaults(config.settings, tmp_path, tmp_path / "config.json", tmp_path / "new.db")
    model_profiles.initialize_installation_profile()
    model_profiles.initialize_installation_profile()
    db.expire_all()
    selected = model_profiles.default_profile_id(db)
    assert selected != model_profiles.LEGACY_PROFILE_ID
    profile = model_profiles.get_profile(db, selected)
    assert profile.model_id == "deepseek-flash" and profile.credential_ref is None
    assert model_profiles.profile_snapshot(profile)["mode"] == "unconfigured"
    assert db.query(ModelProfile).count() == 1
    model_profiles.set_default(db, model_profiles.DEMO_PROFILE_ID)
    db.commit()
    model_profiles.initialize_installation_profile()
    db.expire_all()
    assert model_profiles.default_profile_id(db) == model_profiles.DEMO_PROFILE_ID


@pytest.mark.parametrize("existing", ["old_marker", "conversation", "profile", "options", "default"])
def test_bootstrap_does_not_override_existing_installation_or_selections(db, tmp_path, monkeypatch, existing):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    marker = {"schema_version": 1, "default_generation": "deepseek-flash"}
    if existing != "old_marker":
        marker["independent_profile_bootstrap"] = True
    (tmp_path / "installation-defaults.json").write_text(json.dumps(marker), encoding="utf-8")
    if existing == "conversation":
        db.add(Conversation(id="old", title="Keep"))
    elif existing == "profile":
        model_profiles.save_profile(db, {"name": "Old", "base_url": "http://localhost/v1", "model_id": "OLD"})
    elif existing == "options":
        db.add(ConversationOptions(conversation_id="old", revision=1, model_profile_id="legacy-default"))
    elif existing == "default":
        model_profiles.set_default(db, model_profiles.DEMO_PROFILE_ID)
    db.commit()
    expected_default = model_profiles.default_profile_id(db)
    expected_profiles = db.query(ModelProfile).count()
    db.rollback()
    model_profiles.initialize_installation_profile()
    db.expire_all()
    assert model_profiles.default_profile_id(db) == expected_default
    assert db.query(ModelProfile).count() == expected_profiles


def test_bootstrap_transaction_failure_can_retry_without_duplicate(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    config.initialize_installation_defaults(config.settings, tmp_path, tmp_path / "config.json", tmp_path / "new.db")
    original = model_profiles.set_default

    def fail_before_commit(*args):
        raise OSError("synthetic failure before default commit")

    with monkeypatch.context() as local:
        local.setattr(model_profiles, "set_default", fail_before_commit)
        with pytest.raises(OSError):
            model_profiles.initialize_installation_profile()
    db.expire_all()
    assert db.query(ModelProfile).count() == 0
    assert db.get(DesktopState, model_profiles.BOOTSTRAP_KEY) is None
    db.rollback()
    assert model_profiles.set_default is original
    model_profiles.initialize_installation_profile()
    db.expire_all()
    assert db.query(ModelProfile).count() == 1
