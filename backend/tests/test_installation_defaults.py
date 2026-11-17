"""New defaults never replace an existing user's provider or model binding."""
import json

import pytest

from app import config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for field in ("LLM_BASE_URL", "LLM_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL"):
        monkeypatch.delenv(field, raising=False)


def make_settings():
    return config.Settings(_env_file=None)


def test_new_install_defaults_and_restart_are_stable(tmp_path):
    first = make_settings()
    embedding_before = first.embedding_base_url, first.embedding_model
    assert config.initialize_installation_defaults(first, tmp_path, tmp_path / "config.json", tmp_path / "app.db") == "new"
    assert (first.llm_base_url, first.llm_model) == ("https://api.deepseek.com/v1", "deepseek-flash")
    marker = json.loads((tmp_path / "installation-defaults.json").read_text(encoding="utf-8"))
    assert marker == {"schema_version": 1, "default_generation": "deepseek-flash", "independent_profile_bootstrap": True}
    assert (first.embedding_base_url, first.embedding_model) == embedding_before
    (tmp_path / "app.db").touch()
    second = make_settings()
    assert config.initialize_installation_defaults(second, tmp_path, tmp_path / "config.json", tmp_path / "app.db") == "current"
    assert second.llm_model == "deepseek-flash"


@pytest.mark.parametrize("artifact", ["config.json", "app.db"])
def test_old_implicit_defaults_are_preserved_without_reading_user_data(tmp_path, artifact):
    (tmp_path / artifact).write_text("opaque-old-data", encoding="utf-8")
    instance = make_settings()
    assert config.initialize_installation_defaults(instance, tmp_path, tmp_path / "config.json", tmp_path / "app.db") == "legacy"
    assert instance.llm_model == "qwen-flash"
    assert instance.llm_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert (tmp_path / artifact).read_text(encoding="utf-8") == "opaque-old-data"
    assert not (tmp_path / "installation-defaults.json").exists()


def test_explicit_environment_and_saved_configuration_keep_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://Models.Example.Test/Custom/v1")
    monkeypatch.setenv("LLM_MODEL", "MiXeD/Chosen-Model")
    instance = make_settings()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"llm_model": "Saved/Case-Model", "llm_base_url": "https://saved.test/v1"}), encoding="utf-8")
    config.initialize_installation_defaults(instance, tmp_path, path, tmp_path / "app.db")
    assert instance.llm_model == "MiXeD/Chosen-Model"
    assert instance.llm_base_url == "https://models.example.test/Custom/v1"
    monkeypatch.setattr(config, "settings", instance)
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    config._load_config_file()
    assert instance.llm_model == "Saved/Case-Model"
    assert instance.llm_base_url == "https://saved.test/v1"


def test_marker_failure_does_not_create_a_database_or_config(tmp_path, monkeypatch):
    def full_disk(*args, **kwargs):
        raise OSError("synthetic disk full")
    monkeypatch.setattr(config, "atomic_write_json", full_disk)
    with pytest.raises(OSError):
        config.initialize_installation_defaults(make_settings(), tmp_path, tmp_path / "config.json", tmp_path / "app.db")
    assert not list(tmp_path.iterdir())
