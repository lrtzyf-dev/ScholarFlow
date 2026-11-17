"""Agent round budgets validate atomically and persist without reading user config."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.api import settings as settings_api


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    # Supply every field explicitly so Settings does not consult environment
    # values or the developer's .env file for this test instance.
    defaults = {name: field.get_default(call_default_factory=True)
                for name, field in config.Settings.model_fields.items()}
    defaults["data_dir"] = str(tmp_path)
    isolated = config.Settings(_env_file=None, **defaults)
    monkeypatch.setattr(config, "settings", isolated)
    monkeypatch.setattr(settings_api, "settings", isolated)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("app.llm.reset_llm", lambda: None)
    monkeypatch.setattr("app.pipeline.embeddings.reset_embedder", lambda: None)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as value:
        yield value


def test_default_budget_is_24_and_is_returned_by_settings_api(client):
    assert config.Settings.model_fields["max_agent_steps"].default == 24
    assert config.settings.max_agent_steps == 24
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["max_agent_steps"] == 24
    assert "max_agent_steps" in config.RUNTIME_INT_FIELDS
    assert not config.CONFIG_PATH.exists()


@pytest.mark.parametrize("budget", [1, 37, 128])
def test_budget_put_get_and_config_reload_preserve_integer_value(client, budget):
    response = client.put("/api/settings", json={"max_agent_steps": budget})
    assert response.status_code == 200
    assert response.json()["max_agent_steps"] == budget
    assert client.get("/api/settings").json()["max_agent_steps"] == budget
    persisted = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert persisted["max_agent_steps"] == budget
    assert type(persisted["max_agent_steps"]) is int
    config.settings.max_agent_steps = 24
    config._load_config_file()
    assert config.settings.max_agent_steps == budget


@pytest.mark.parametrize("payload", [{}, {"work_rules": "仅使用实际文件结果"}, {"max_agent_steps": None}])
def test_omitted_budget_retains_current_value_in_runtime_and_file(client, payload):
    assert client.put("/api/settings", json={"max_agent_steps": 41}).status_code == 200
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    assert response.json()["max_agent_steps"] == 41
    assert config.settings.max_agent_steps == 41
    assert json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))["max_agent_steps"] == 41


@pytest.mark.parametrize("invalid", [0, -1, 129, 1.5, "24", " 24 ", True, False, [], {}])
def test_invalid_api_budget_is_422_and_changes_neither_runtime_nor_file(client, invalid):
    assert client.put("/api/settings", json={"max_agent_steps": 31, "work_rules": "original"}).status_code == 200
    before, contents = config.settings.model_dump(), config.CONFIG_PATH.read_bytes()
    response = client.put("/api/settings", json={"max_agent_steps": invalid, "work_rules": "must not apply"})
    assert response.status_code == 422
    assert config.settings.model_dump() == before
    assert config.CONFIG_PATH.read_bytes() == contents


@pytest.mark.parametrize("invalid", [0, 129, 2.5, "12", True, None, [], {}])
def test_invalid_direct_update_is_atomic_even_with_other_valid_fields(invalid):
    config.update_settings({"max_agent_steps": 31, "work_rules": "original"})
    before, contents = config.settings.model_dump(), config.CONFIG_PATH.read_bytes()
    with pytest.raises(ValueError, match="1–128"):
        config.update_settings({"max_agent_steps": invalid, "work_rules": "must not apply",
                                "memory_capture": False})
    assert config.settings.model_dump() == before
    assert config.CONFIG_PATH.read_bytes() == contents


@pytest.mark.parametrize("legacy_budget", [0, 129, -5, 1.5, "24", True, False, None, [], {}])
def test_startup_loader_ignores_invalid_legacy_budget_and_keeps_usable_default(legacy_budget):
    # This is the same loader called during app.config import at startup.
    contents = json.dumps({"max_agent_steps": legacy_budget, "work_rules": "retained valid rule"})
    config.CONFIG_PATH.write_text(contents, encoding="utf-8")
    config._load_config_file()
    assert config.settings.max_agent_steps == 24
    assert config.settings.work_rules == "retained valid rule"
    assert config.CONFIG_PATH.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("contents", ["{broken", "[]", "null"])
def test_startup_loader_ignores_malformed_or_non_object_legacy_config(contents):
    config.CONFIG_PATH.write_text(contents, encoding="utf-8")
    config._load_config_file()
    assert config.settings.max_agent_steps == 24
    assert config.CONFIG_PATH.read_text(encoding="utf-8") == contents
