"""Synthetic credentials only: atomic legacy migration and inaccessible secrets."""
import json

import pytest

from app import config
from app.credential_protection import CredentialProtectionError, protect, unprotect


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    defaults = {name: field.get_default(call_default_factory=True)
                for name, field in config.Settings.model_fields.items()}
    defaults["data_dir"] = str(tmp_path)
    instance = config.Settings(_env_file=None, **defaults)
    monkeypatch.setattr(config, "settings", instance)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "_credential_config_path", None)
    monkeypatch.setattr(config, "_protected_credentials", {})
    monkeypatch.setattr(config, "_credential_errors", {})
    monkeypatch.setattr("app.llm.reset_llm", lambda: None)
    monkeypatch.setattr("app.pipeline.embeddings.reset_embedder", lambda: None)


def test_both_saved_keys_are_protected_and_keep_case_after_restart():
    config.update_settings({"llm_api_key": "Synthetic-Case-LLM-123", "embedding_api_key": "Synthetic-EmB-456"})
    text = config.CONFIG_PATH.read_text(encoding="utf-8")
    assert "Synthetic-Case" not in text and "Synthetic-EmB" not in text
    stored = json.loads(text)
    assert stored["llm_api_key"] == stored["embedding_api_key"] == ""
    assert all(stored["protected_credentials"][key].startswith("dpapi:") for key in config.SECRET_FIELDS)
    config.settings.llm_api_key = config.settings.embedding_api_key = ""
    config._load_config_file()
    assert config.settings.llm_api_key == "Synthetic-Case-LLM-123"
    assert config.settings.embedding_api_key == "Synthetic-EmB-456"
    assert not config.credential_status()
    assert "protected_credentials" not in config.runtime_settings()


def test_legacy_migration_has_verified_encrypted_recovery_and_no_plaintext_backup():
    original = {"llm_api_key": "Synthetic-Legacy-Key", "embedding_api_key": "Synthetic-Embed-Key",
                "llm_model": "Model-CaSe", "work_rules": "用户原有规则", "unknown_legacy": {"keep": True}}
    config.CONFIG_PATH.write_text(json.dumps(original), encoding="utf-8")
    config._load_config_file()
    assert config.settings.llm_api_key == original["llm_api_key"]
    assert config.settings.llm_model == "Model-CaSe"
    assert json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))["unknown_legacy"] == {"keep": True}
    recovery_path = config.CONFIG_PATH.with_suffix(".credential-recovery.json")
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert json.loads(unprotect(recovery["protected_config"])) == original
    for path in config.CONFIG_PATH.parent.iterdir():
        if path.is_file():
            assert original["llm_api_key"] not in path.read_text(encoding="utf-8")
    before = config.CONFIG_PATH.read_bytes()
    config._load_config_file()
    assert config.CONFIG_PATH.read_bytes() == before  # migration is idempotent


def test_protection_failure_does_not_mutate_runtime_or_existing_file(monkeypatch):
    config.update_settings({"llm_api_key": "Synthetic-Original", "work_rules": "original"})
    before = config.CONFIG_PATH.read_bytes(), config.settings.model_dump()

    def failed(value):
        raise CredentialProtectionError("synthetic protection unavailable")

    monkeypatch.setattr(config, "protect", failed)
    with pytest.raises(CredentialProtectionError):
        config.update_settings({"llm_api_key": "Synthetic-New", "work_rules": "must not apply"})
    assert (config.CONFIG_PATH.read_bytes(), config.settings.model_dump()) == before


def test_replace_failure_keeps_original_config_and_cleans_temp(monkeypatch):
    config.update_settings({"llm_api_key": "Synthetic-Original"})
    before = config.CONFIG_PATH.read_bytes(), config.settings.model_dump()
    import app.atomic_files as atomic

    def failed(*args):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(atomic.os, "replace", failed)
    with pytest.raises(ValueError, match="原配置"):
        config.update_settings({"work_rules": "must not apply"})
    assert (config.CONFIG_PATH.read_bytes(), config.settings.model_dump()) == before
    assert not list(config.CONFIG_PATH.parent.glob(".*.tmp"))


def test_failed_legacy_migration_is_visible_and_preserves_original(monkeypatch):
    original = json.dumps({"llm_api_key": "Synthetic-Legacy", "work_rules": "keep"})
    config.CONFIG_PATH.write_text(original, encoding="utf-8")

    def failed(value):
        raise CredentialProtectionError("synthetic")

    monkeypatch.setattr(config, "protect", failed)
    config._load_config_file()
    assert config.CONFIG_PATH.read_text(encoding="utf-8") == original
    assert config.settings.llm_api_key == "Synthetic-Legacy"
    assert "migration" in config.runtime_settings()["credential_errors"]
    with pytest.raises(CredentialProtectionError):
        config.update_settings({"work_rules": "must not apply"})


def test_unreadable_protected_key_cannot_be_erased_or_forwarded_by_other_setting():
    config.CONFIG_PATH.write_text(json.dumps({
        "llm_api_key": "", "llm_base_url": "https://original.invalid/v1",
        "protected_credentials": {"llm_api_key": "dpapi:corrupt"},
    }), encoding="utf-8")
    config._load_config_file()
    assert config.settings.llm_api_key == ""
    assert config.runtime_settings()["llm_api_key_set"] is True
    with pytest.raises(CredentialProtectionError):
        config.require_available_credentials("llm_api_key")
    with pytest.raises(ValueError):
        config.update_settings({"llm_base_url": "https://other.invalid/v1"})
    config.update_settings({"work_rules": "ordinary preference"})
    stored = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert stored["protected_credentials"]["llm_api_key"] == "dpapi:corrupt"
    assert "llm_api_key" in config.credential_status()
    config.update_settings({"llm_api_key": ""})
    assert not config.credential_status()
    assert config.runtime_settings()["llm_api_key_set"] is False


def test_inaccessible_key_does_not_silently_select_mock(db):
    from app.model_profiles import capture_run_config
    from app.llm import get_llm
    config.CONFIG_PATH.write_text(json.dumps({"protected_credentials": {"llm_api_key": "dpapi:corrupt"}}), encoding="utf-8")
    config._load_config_file()
    with pytest.raises(CredentialProtectionError):
        capture_run_config(db, "credential_error_conversation")
    with pytest.raises(CredentialProtectionError):
        get_llm()


def test_inaccessible_embedding_key_does_not_silently_select_hash_vectors():
    from app.pipeline.embeddings import get_embedder
    config.CONFIG_PATH.write_text(json.dumps({"protected_credentials": {"embedding_api_key": "dpapi:corrupt"}}), encoding="utf-8")
    config._load_config_file()
    with pytest.raises(CredentialProtectionError):
        get_embedder()
