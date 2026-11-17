"""Legacy key revocation with isolated files/SQLite and synthetic secrets only."""
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import config, db as database, model_profiles as store
from app.credential_protection import protect
from app.llm import APIClient
from app.models import ModelCredential


OLD_KEY = "Synthetic-Legacy-Old-Key"
NEW_KEY = "Synthetic-Legacy-New-Key"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    defaults = {name: field.get_default(call_default_factory=True)
                for name, field in config.Settings.model_fields.items()}
    defaults["data_dir"] = str(tmp_path)
    instance = config.Settings(_env_file=None, **defaults)
    monkeypatch.setattr(config, "settings", instance)
    monkeypatch.setattr(store, "settings", instance)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "_credential_config_path", None)
    monkeypatch.setattr(config, "_protected_credentials", {})
    monkeypatch.setattr(config, "_credential_errors", {})
    monkeypatch.setattr("app.llm.reset_llm", lambda: None)
    monkeypatch.setattr("app.pipeline.embeddings.reset_embedder", lambda: None)
    engine = create_engine(f"sqlite:///{tmp_path / 'scholarflow.db'}",
                           connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def configure(key=OLD_KEY):
    config.update_settings({"llm_api_key": key, "llm_base_url": "https://synthetic.invalid/v1",
                            "llm_model": "Synthetic-Case-Model"})


def capture(sessions, cid):
    with sessions() as db:
        binding = store.capture_run_config(db, cid)
        db.commit()
    return binding


@pytest.mark.parametrize("clear", ["", "   "])
def test_explicit_clear_revokes_old_api_clients_before_decryption(isolated, monkeypatch, clear):
    configure()
    binding = capture(isolated, "legacy_before_clear")
    client = APIClient(runtime_config=binding)
    assert client._connection(None)[1] == OLD_KEY
    with isolated() as db:
        assert db.get(ModelCredential, binding["credential_ref"]).legacy_generation == 0

    result = config.update_settings({"llm_api_key": clear})
    assert result["llm_api_key_set"] is False
    assert config.settings.legacy_credential_generation == 1
    stored = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert stored["legacy_credential_generation"] == 1
    assert stored["llm_api_key"] == "" and "llm_api_key" not in stored["protected_credentials"]
    assert OLD_KEY not in json.dumps(stored)

    def forbidden_decrypt(value):
        pytest.fail("revoked credentials must be rejected before decryption")

    monkeypatch.setattr(store, "unprotect", forbidden_decrypt)
    with pytest.raises(store.ModelConfigurationError, match="撤销"):
        client._connection(None)


def restart_probe(directory, binding):
    # Use a fresh interpreter, the persisted config and the same isolated DB.
    # Only booleans and the nonsecret generation leave the child process.
    script = """
import json,sys
from app import config,model_profiles as store
binding=json.loads(sys.argv[1])
try:
    store.resolve_model_config(binding)
except store.ModelConfigurationError:
    rejected=True
else:
    rejected=False
print(json.dumps({'rejected':rejected,'generation':config.settings.legacy_credential_generation,
                  'key_empty':config.settings.llm_api_key==''}))
"""
    env = {**os.environ, "DATA_DIR": str(directory), "LLM_API_KEY": "", "EMBEDDING_API_KEY": "",
           "REDIS_URL": "", "LLM_BASE_URL": "https://synthetic.invalid/v1", "LLM_MODEL": "Synthetic-Model"}
    result = subprocess.run([sys.executable, "-c", script, json.dumps(binding)],
                            cwd=config.BACKEND_DIR, env=env, capture_output=True,
                            text=True, check=True, timeout=20)
    return json.loads(result.stdout)


def test_clear_survives_restart_and_rekey_never_reauthorizes_old_bindings(isolated, tmp_path):
    configure()
    old_binding = capture(isolated, "legacy_restart_old")
    config.update_settings({"llm_api_key": ""})
    assert restart_probe(tmp_path, old_binding) == {"rejected": True, "generation": 1, "key_empty": True}

    config.update_settings({"llm_api_key": NEW_KEY})
    new_binding = capture(isolated, "legacy_restart_new")
    assert config.settings.legacy_credential_generation == 1
    assert store.resolve_model_config(new_binding)["api_key"] == NEW_KEY
    assert restart_probe(tmp_path, old_binding) == {"rejected": True, "generation": 1, "key_empty": False}
    assert restart_probe(tmp_path, new_binding) == {"rejected": False, "generation": 1, "key_empty": False}
    config.update_settings({"llm_api_key": ""})
    assert config.settings.legacy_credential_generation == 2
    with pytest.raises(store.ModelConfigurationError):
        store.resolve_model_config(new_binding)


def test_rotation_and_unrelated_settings_keep_existing_legacy_bindings(isolated):
    configure()
    old_binding = capture(isolated, "legacy_rotation_old")
    config.update_settings({"llm_api_key": NEW_KEY})
    new_binding = capture(isolated, "legacy_rotation_new")
    config.update_settings({"work_rules": "Synthetic preference", "embedding_api_key": "", "llm_api_key": None,
                            "legacy_credential_generation": 999})
    assert config.settings.legacy_credential_generation == 0
    assert store.resolve_model_config(old_binding)["api_key"] == OLD_KEY
    assert store.resolve_model_config(new_binding)["api_key"] == NEW_KEY
    assert "legacy_credential_generation" not in config.runtime_settings()


def test_named_profile_binding_is_independent_of_legacy_revocation(isolated):
    configure()
    old_binding = capture(isolated, "legacy_independent")
    with isolated() as db:
        profile = store.save_profile(db, {"name": "Synthetic Named Provider", "base_url": "https://named.invalid/v1",
                                         "model_id": "Named-Model", "api_key": "Synthetic-Named-Key"})
        store.set_default(db, profile.id)
        db.commit()
    named_binding = capture(isolated, "named_independent")
    config.update_settings({"llm_api_key": ""})
    with pytest.raises(store.ModelConfigurationError):
        store.resolve_model_config(old_binding)
    assert store.resolve_model_config(named_binding)["api_key"] == "Synthetic-Named-Key"


def test_failed_clear_keeps_file_key_generation_and_old_binding(isolated, monkeypatch):
    configure()
    binding = capture(isolated, "legacy_failed_clear")
    before = config.CONFIG_PATH.read_bytes(), config.settings.model_dump()

    def failed_write(*args):
        raise OSError("synthetic disk unavailable")

    monkeypatch.setattr(config, "atomic_write_json", failed_write)
    with pytest.raises(ValueError, match="原配置"):
        config.update_settings({"llm_api_key": ""})
    assert (config.CONFIG_PATH.read_bytes(), config.settings.model_dump()) == before
    assert store.resolve_model_config(binding)["api_key"] == OLD_KEY


def test_unreadable_saved_key_can_be_cleared_without_decrypting_old_bindings(isolated, monkeypatch):
    configure()
    binding = capture(isolated, "legacy_unreadable")
    data = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    data["protected_credentials"]["llm_api_key"] = "dpapi:corrupt"
    config.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
    config._load_config_file()
    assert "llm_api_key" in config.credential_status()
    config.update_settings({"llm_api_key": ""})
    assert not config.credential_status()
    assert config.settings.legacy_credential_generation == 1
    monkeypatch.setattr(store, "unprotect", lambda value: pytest.fail("must not decrypt revoked key"))
    with pytest.raises(store.ModelConfigurationError):
        store.resolve_model_config(binding)


def test_old_credential_rows_migrate_to_generation_zero_and_revoke(isolated, tmp_path, monkeypatch):
    configure()
    legacy = create_engine(f"sqlite:///{tmp_path / 'old-credentials.db'}")
    try:
        with legacy.begin() as conn:
            conn.execute(text("CREATE TABLE model_credentials (id TEXT PRIMARY KEY, profile_id TEXT, "
                              "protected_value TEXT NOT NULL, revoked_at DATETIME, created_at DATETIME)"))
            conn.execute(text("INSERT INTO model_credentials (id,protected_value) VALUES (:id,:value)"),
                         {"id": "old_credential", "value": protect(OLD_KEY)})
        monkeypatch.setattr(database, "engine", legacy)
        database._lightweight_migrations()
        database._lightweight_migrations()
        with legacy.connect() as conn:
            column = next(row for row in conn.execute(text("PRAGMA table_info(model_credentials)"))
                          if row[1] == "legacy_generation")
            assert column[3] == 1 and column[4] == "0"
            assert conn.execute(text("SELECT legacy_generation FROM model_credentials")).scalar() == 0
        with sessionmaker(bind=legacy)() as db:
            assert store.read_credential(db, "old_credential") == OLD_KEY
            config.update_settings({"llm_api_key": ""})
            with pytest.raises(store.ModelConfigurationError):
                store.read_credential(db, "old_credential")
    finally:
        legacy.dispose()


def test_capture_cannot_stamp_preclear_key_with_postclear_generation(isolated, monkeypatch):
    configure()
    captured, release, clearing = threading.Event(), threading.Event(), threading.Event()
    real_protect = store.protect

    def delayed_protect(value):
        captured.set()
        assert release.wait(5)
        return real_protect(value)

    def clear():
        clearing.set()
        config.update_settings({"llm_api_key": ""})

    monkeypatch.setattr(store, "protect", delayed_protect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending_capture = pool.submit(capture, isolated, "legacy_capture_race")
        assert captured.wait(5)
        pending_clear = pool.submit(clear)
        assert clearing.wait(5)
        try:
            pending_clear.result(timeout=5)
            assert config.settings.legacy_credential_generation == 1
        finally:
            release.set()
        binding = pending_capture.result(timeout=10)
    with isolated() as db:
        assert db.get(ModelCredential, binding["credential_ref"]).legacy_generation == 0
    assert config.settings.legacy_credential_generation == 1
    with pytest.raises(store.ModelConfigurationError):
        store.resolve_model_config(binding)
