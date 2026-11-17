"""Synthetic, isolated coverage for credential-free public exports."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import backup_credentials as credentials, data_backup as backup
from app.db import Base
from app.models import ConversationOptions, ModelCredential, ModelProfile, TaskRecord, Subscription
from app.run_inputs import RunInput
from app.service_tool_models import ServiceToolConfiguration, ServiceToolCredential
from app.mcp_auth_models import McpUserGrant


PLAIN = "test-key-d61795391bee4f92"
PROTECTED = "dpapi:synthetic-ciphertext-only-a61949"
SERVICE_PROTECTED = "dpapi:synthetic-service-ciphertext-7f1792"
MCP_PROTECTED = "dpapi:synthetic-mcp-ciphertext-8129c1"
OAUTH_PROTECTED = "dpapi:synthetic-oauth-grant-ciphertext-42ad17"
FREED = "test-key-7159a603328b1691"
SECRETS = (PLAIN, PROTECTED, SERVICE_PROTECTED, MCP_PROTECTED, OAUTH_PROTECTED, FREED)
PROFILE_ID = "a" * 32
CREDENTIAL_ID = "b" * 32
SERVICE_ID = "c" * 32
FROZEN = {"version": 1, "profile_id": PROFILE_ID, "profile_version": 3,
          "base_url": "https://model.example/v1", "model_id": "Model-Exact-ID",
          "credential_ref": CREDENTIAL_ID, "mode": "api", "source_binding": [{"revision_fingerprint": "unchanged"}]}


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def make_private_data(root):
    root.mkdir(parents=True)
    engine = create_engine("sqlite:///" + str(root / "scholarflow.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ModelProfile(id=PROFILE_ID, name="Saved model", base_url="https://model.example/v1",
                            model_id="Model-Exact-ID", credential_ref=CREDENTIAL_ID, version=3, enabled=1))
        db.add(ModelCredential(id=CREDENTIAL_ID, profile_id=PROFILE_ID, protected_value=PROTECTED))
        db.add(ServiceToolCredential(id=SERVICE_ID, provider="deepseek", protected_value=SERVICE_PROTECTED))
        db.add(ServiceToolConfiguration(provider="deepseek", credential_ref=SERVICE_ID,
                                        enabled_services=["search"], version=4))
        db.add(McpUserGrant(server_id="synthetic-oauth",fingerprint="f"*64,generation=3,state="authorized",
            protected=OAUTH_PROTECTED,expires_at=9000000000,refresh_owner="prior-owner",refresh_until=9000000000,
            message="Prior authorization diagnostic"))
        db.add(ConversationOptions(conversation_id="conversation", revision=4,
                                   model_profile_id=PROFILE_ID, approval_mode="full_access"))
        db.add(RunInput(input_id="input", run_id="run", conversation_id="conversation", kind="message",
                        content="synthetic queued message", workspace_revision=0, revision=1,
                        status="queued", reason=""))
        db.add(TaskRecord(id="task", type="agent", status="paused", title="Prior task",
                          runtime_config=FROZEN, checkpoint={"runtime_config": FROZEN, "question": "keep transcript"}))
        db.add(Subscription(topic="must stay dormant", active=1))
        db.commit()
    engine.dispose()
    write_json(root / "config.json", {"llm_api_key": PLAIN, "embedding_api_key": PLAIN,
        "llm_base_url": "https://model.example/v1", "llm_model": "Model-Exact-ID",
        "protected_credentials": {"llm_api_key": PROTECTED, "embedding_api_key": PROTECTED},
        "config_schema_version": 2, "legacy_credential_generation": 7, "approval_mode": "full_access",
        "work_rules": "retain these research preferences"})
    write_json(root / "config.credential-recovery.json", {"schema_version": 1, "protected_config": PROTECTED})
    write_json(root / "mcp_servers.json", {"servers": [{"name": "external", "enabled": True,
        "transport": "http", "url": "https://mcp.example/path", "auth_protected": MCP_PROTECTED,
        "headers": {"Authorization": PLAIN}, "bearer_token": PLAIN,
        "oauth": {"client_id": "client", "client_secret": PLAIN}, "env": {"API_KEY": PLAIN}}],
        "disabled_builtin": []})
    (root / ".env").write_text("LLM_API_KEY=" + PLAIN, encoding="utf-8")
    (root / ".config.json.abcd.tmp").write_text(PLAIN, encoding="utf-8")
    (root / "documents").mkdir()
    (root / "documents" / "config.json").write_text('{"api_key":"user-document-verbatim"}', encoding="utf-8")
    (root / "backups").mkdir()
    (root / "backups" / "old-schema.sqlite").write_bytes(PROTECTED.encode())
    return root


def tree_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def make_deleted_pages(root):
    with closing(sqlite3.connect(root / "scholarflow.db")) as db:
        db.execute("PRAGMA secure_delete=OFF")
        db.executemany("INSERT INTO model_credentials(id,protected_value,legacy_generation,created_at) VALUES (?,?,0,CURRENT_TIMESTAMP)",
                       [(f"old-{i}", FREED * 1000) for i in range(48)])
        db.commit()
        db.execute("DELETE FROM model_credentials WHERE id LIKE 'old-%'")
        db.commit()
        assert db.execute("PRAGMA freelist_count").fetchone()[0] > 0
    assert FREED.encode() in (root / "scholarflow.db").read_bytes()


def test_public_archive_excludes_managed_secrets_and_free_pages_without_editing_original(tmp_path):
    root = make_private_data(tmp_path / "source")
    make_deleted_pages(root)
    before = tree_hashes(root)
    exported = backup.create_backup(root)
    assert tree_hashes(root) == before
    manifest = backup.verify_archive(exported["path"], extract_to=tmp_path / "exported")
    assert manifest["credential_portability"] == credentials.EXCLUDED_RECONFIGURE
    assert manifest["credential_exclusion_scope"] == "application_managed_stores"
    with zipfile.ZipFile(exported["path"]) as archive:
        names = archive.namelist()
        for secret in SECRETS:
            assert secret.encode() not in Path(exported["path"]).read_bytes()
            assert all(secret.encode() not in archive.read(name) for name in names)
        assert "data/config.credential-recovery.json" not in names
        assert "data/.env" not in names
        assert "data/.config.json.abcd.tmp" not in names
        assert not any(name.startswith("data/backups/") for name in names)
        assert archive.read("data/documents/config.json") == (root / "documents/config.json").read_bytes()
    extracted = tmp_path / "exported"
    config = json.loads((extracted / "config.json").read_text())
    assert config["llm_api_key"] == config["embedding_api_key"] == ""
    assert "protected_credentials" not in config and config["legacy_credential_generation"] == 8
    assert config["llm_model"] == "Model-Exact-ID" and config["work_rules"] == "retain these research preferences"
    server = json.loads((extracted / "mcp_servers.json").read_text())["servers"][0]
    assert server == {"name": "external", "enabled": False, "transport": "http", "url": "https://mcp.example/path"}
    with closing(sqlite3.connect(extracted / "scholarflow.db")) as db:
        assert db.execute("SELECT count(*) FROM model_credentials").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM service_tool_credentials").fetchone() == (0,)
        assert db.execute("SELECT credential_ref,enabled,version FROM model_profiles").fetchone() == (None, 0, 4)
        assert db.execute("SELECT credential_ref,enabled_services,version FROM service_tool_configurations").fetchone() == (None, "[]", 5)
        assert db.execute("SELECT protected,state,generation,expires_at,refresh_owner,refresh_until,message FROM mcp_user_grants").fetchone() == (None,"unauthorized",4,0,None,0,"")
        frozen, checkpoint = db.execute("SELECT runtime_config,checkpoint FROM tasks").fetchone()
        assert json.loads(frozen) == FROZEN and json.loads(checkpoint)["runtime_config"] == FROZEN
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA freelist_count").fetchone() == (0,)


def test_actual_restore_requires_reconfiguration_and_frozen_task_never_borrows_new_key(tmp_path, monkeypatch):
    from app import config, model_profiles, service_tool_config
    root = make_private_data(tmp_path / "source")
    exported = backup.create_backup(root)
    target = tmp_path / "restored"
    preview = backup.prepare_restore(target, exported["path"])
    assert preview["credential_portability"] == credentials.EXCLUDED_RECONFIGURE
    backup.schedule_restore(target, preview["id"])
    result = backup.apply_pending_restore(target)
    assert result["credential_portability"] == credentials.EXCLUDED_RECONFIGURE
    isolated_settings = config.Settings(_env_file=None, llm_api_key="environment-must-not-revive",
                                         embedding_api_key="environment-must-not-revive")
    for name, value in {"settings": isolated_settings, "CONFIG_PATH": target / "config.json",
                        "_credential_config_path": None, "_protected_credentials": {}, "_credential_errors": {}}.items():
        monkeypatch.setattr(config, name, value)
    config._load_config_file()
    assert isolated_settings.llm_api_key == isolated_settings.embedding_api_key == ""
    assert config.credential_status() == {}
    restored_mcp = json.loads((target / "mcp_servers.json").read_text())
    assert all(server["enabled"] is False for server in restored_mcp["servers"])
    assert all(not any(server.get(key) for key in ("auth_protected", "headers", "bearer_token", "oauth", "env"))
               for server in restored_mcp["servers"])
    engine = create_engine("sqlite:///" + str(target / "scholarflow.db"))
    with Session(engine) as db:
        old = db.get(TaskRecord, "task").runtime_config
        with pytest.raises(model_profiles.ModelConfigurationError, match="撤销或不可用"):
            model_profiles.resolve_model_config(old, db)
        with pytest.raises(service_tool_config.ServiceConfigurationError, match="尚未启用"):
            service_tool_config.require_configuration(db, "search", version=4, credential_ref=SERVICE_ID)
        db.add(ModelCredential(id="d" * 32, profile_id=PROFILE_ID, protected_value="new-synthetic-key"))
        profile = db.get(ModelProfile, PROFILE_ID)
        profile.credential_ref, profile.enabled = "d" * 32, 1
        profile.version += 1
        db.commit()
        # Reconfiguring the active model never revives an old immutable ref.
        with pytest.raises(model_profiles.ModelConfigurationError, match="撤销或不可用"):
            model_profiles.resolve_model_config(old, db)
        assert db.get(Subscription, db.query(Subscription).first().id).active == 0
        assert db.query(RunInput).filter_by(status="queued").count() == 0
        assert db.query(ConversationOptions.approval_mode).filter_by(approval_mode="read_only").count() == 1
        assert db.query(ServiceToolCredential).count() == 0
    engine.dispose()


def test_internal_previous_rollback_keeps_original_credential_ids_and_protected_bytes(tmp_path):
    target = make_private_data(tmp_path / "target")
    source = make_private_data(tmp_path / "source")
    exported = backup.create_backup(source)
    preview = backup.prepare_restore(target, exported["path"])
    backup.schedule_restore(target, preview["id"])
    backup.apply_pending_restore(target)
    previous = backup.recovery_root(target) / "staged" / preview["id"] / "previous"
    assert json.loads((previous / "config.json").read_text())["llm_api_key"] == PLAIN
    undo = backup.prepare_rollback(target)
    assert undo["credential_portability"] == credentials.PRIVATE_ORIGINAL_USER
    backup.schedule_restore(target, undo["id"])
    backup.apply_pending_restore(target)
    restored = json.loads((target / "config.json").read_text())
    assert restored["llm_api_key"] == PLAIN and restored["protected_credentials"]["llm_api_key"] == PROTECTED
    assert restored["approval_mode"] == "read_only"
    assert (target / "config.credential-recovery.json").exists()
    with closing(sqlite3.connect(target / "scholarflow.db")) as db:
        assert db.execute("SELECT id,protected_value FROM model_credentials").fetchone() == (CREDENTIAL_ID, PROTECTED)
        assert db.execute("SELECT credential_ref,enabled,version FROM model_profiles").fetchone() == (CREDENTIAL_ID, 1, 3)
        assert db.execute("SELECT id,protected_value FROM service_tool_credentials").fetchone() == (SERVICE_ID, SERVICE_PROTECTED)
        assert db.execute("SELECT credential_ref,version FROM service_tool_configurations").fetchone() == (SERVICE_ID, 4)
        assert db.execute("SELECT active FROM subscriptions").fetchone() == (0,)
    assert json.loads((target / "mcp_servers.json").read_text())["servers"][0]["enabled"] is False
    assert not list((backup.recovery_root(target) / "staged").glob("*.sfbak"))


def test_public_entry_cannot_opt_into_private_credentials(tmp_path):
    root = make_private_data(tmp_path / "source")
    with pytest.raises(TypeError):
        backup.create_backup(root, include_credentials=True)
    with pytest.raises(backup.BackupError, match="内部恢复回退"):
        backup._create_backup(root, origin_data_dir=root,
                               destination=tmp_path / "leak.sfbak", _private_rollback=backup._PRIVATE_ROLLBACK)
    assert not (tmp_path / "leak.sfbak").exists()


@pytest.mark.parametrize("kind", ["config_list", "config_unknown", "config_future", "config_duplicate",
                                  "mcp_list", "mcp_unknown", "profile_column", "unknown_credential_table", "credential_trigger", "credential_default",
                                  "credential_case_table", "unknown_case_table", "unknown_case_column"])
def test_unrecognized_managed_credential_structures_fail_closed(tmp_path, kind):
    root = make_private_data(tmp_path / "source")
    if kind == "config_list":
        write_json(root / "config.json", [PLAIN])
    elif kind == "config_unknown":
        write_json(root / "config.json", {"future_auth": {"secret": PLAIN}})
    elif kind == "config_future":
        write_json(root / "config.json", {"config_schema_version": 99, "llm_api_key": PLAIN})
    elif kind == "config_duplicate":
        (root / "config.json").write_text('{"llm_api_key":"a","llm_api_key":"b"}', encoding="utf-8")
    elif kind == "mcp_list":
        write_json(root / "mcp_servers.json", {"servers": [PLAIN]})
    elif kind == "mcp_unknown":
        write_json(root / "mcp_servers.json", {"servers": [{"name": "external", "future_auth": PLAIN}]})
    else:
        with closing(sqlite3.connect(root / "scholarflow.db")) as db:
            if kind == "profile_column":
                db.execute("ALTER TABLE model_profiles ADD COLUMN future_auth TEXT")
            elif kind == "unknown_credential_table":
                db.execute("CREATE TABLE plugin_credentials(secret TEXT)")
                db.execute("INSERT INTO plugin_credentials VALUES (?)", (PLAIN,))
            elif kind == "credential_trigger":
                db.execute("CREATE TABLE leaking(value TEXT)")
                db.execute("CREATE TRIGGER leak_after_delete AFTER DELETE ON model_credentials BEGIN INSERT INTO leaking VALUES(old.protected_value); END")
            elif kind == "credential_default":
                db.execute("DROP TABLE model_credentials")
                db.execute("CREATE TABLE model_credentials(id TEXT,protected_value TEXT DEFAULT 'test-key-1c553a218a039686')")
            elif kind == "credential_case_table":
                db.execute("ALTER TABLE model_credentials RENAME TO temporary_credentials")
                db.execute("ALTER TABLE temporary_credentials RENAME TO MODEL_CREDENTIALS")
            elif kind == "unknown_case_table":
                db.execute("CREATE TABLE PLUGIN_CREDENTIALS(secret TEXT)")
                db.execute("INSERT INTO PLUGIN_CREDENTIALS VALUES (?)", (PLAIN,))
            elif kind == "unknown_case_column":
                db.execute("CREATE TABLE plugin_storage(Api_Key TEXT)")
                db.execute("INSERT INTO plugin_storage VALUES (?)", (PLAIN,))
            db.commit()
    before = tree_hashes(root)
    with pytest.raises(backup.BackupError, match="无法安全识别"):
        backup.create_backup(root)
    assert tree_hashes(root) == before
    assert not list((backup.recovery_root(root) / "exports").glob("*.sfbak"))
    assert not list((backup.recovery_root(root) / "exports").glob("*.tmp"))
    assert not list((backup.recovery_root(root) / "exports").glob("*.sqlite3*"))


@pytest.mark.parametrize("legacy_scope", [None, credentials.PRIVATE_ORIGINAL_USER])
def test_legacy_v1_archive_keeps_honest_credential_scope_and_is_readable(tmp_path, legacy_scope):
    source = make_private_data(tmp_path / "source")
    path = tmp_path / "legacy.sfbak"
    files = []
    with zipfile.ZipFile(path, "w") as archive:
        for relative in ("scholarflow.db", "config.json", "config.credential-recovery.json"):
            body = (source / relative).read_bytes()
            archive.writestr("data/" + relative, body)
            files.append({"path": relative, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        manifest = {"format": backup.FORMAT, "version": 1, "database": "scholarflow.db", "files": files}
        if legacy_scope:
            manifest["credential_portability"] = legacy_scope
        archive.writestr("manifest.json", json.dumps(manifest))
    assert backup.verify_archive(path).get("credential_portability") == legacy_scope
    target = tmp_path / "target"
    preview = backup.prepare_restore(target, path)
    assert preview["credential_portability"] == legacy_scope
    backup.schedule_restore(target, preview["id"])
    backup.apply_pending_restore(target)
    assert json.loads((target / "config.json").read_text())["protected_credentials"]["llm_api_key"] == PROTECTED


def repack_claimed_excluded(stage, original_manifest, path):
    manifest = dict(original_manifest)
    manifest["credential_portability"] = credentials.EXCLUDED_RECONFIGURE
    manifest["files"] = []
    with zipfile.ZipFile(path, "w") as archive:
        for source in sorted(stage.rglob("*")):
            if source.is_file():
                relative, body = source.relative_to(stage).as_posix(), source.read_bytes()
                archive.writestr("data/" + relative, body)
                manifest["files"].append({"path": relative, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


@pytest.mark.parametrize("kind", ["plaintext", "protected", "mcp", "model_table", "service_table",
                                  "profile_ref", "mcp_enabled", "recovery", "dotenv", "unknown_schema"])
def test_forged_excluded_manifest_is_rejected_without_sanitizing_or_replacing_live_data(tmp_path, kind):
    source = make_private_data(tmp_path / "source")
    exported = backup.create_backup(source)
    stage = tmp_path / "forgery"
    manifest = backup.verify_archive(exported["path"], extract_to=stage)
    if kind in ("plaintext", "protected"):
        config = json.loads((stage / "config.json").read_text())
        config["llm_api_key"] = PLAIN if kind == "plaintext" else ""
        if kind == "protected":
            config["protected_credentials"] = {"llm_api_key": PROTECTED}
        write_json(stage / "config.json", config)
    elif kind in ("mcp", "mcp_enabled"):
        value = json.loads((stage / "mcp_servers.json").read_text())
        if kind == "mcp":
            value["servers"][0]["auth_protected"] = MCP_PROTECTED
        else:
            value["servers"][0]["enabled"] = True
        write_json(stage / "mcp_servers.json", value)
    elif kind == "recovery":
        write_json(stage / "config.credential-recovery.json", {"protected_config": PROTECTED})
    elif kind == "dotenv":
        (stage / ".env").write_text("LLM_API_KEY=" + PLAIN)
    else:
        with closing(sqlite3.connect(stage / "scholarflow.db")) as db:
            if kind == "model_table":
                db.execute("INSERT INTO model_credentials(id,protected_value,created_at) VALUES ('forged',?,CURRENT_TIMESTAMP)", (PROTECTED,))
            elif kind == "service_table":
                db.execute("INSERT INTO service_tool_credentials(id,provider,protected_value,created_at) VALUES ('forged','deepseek',?,CURRENT_TIMESTAMP)", (SERVICE_PROTECTED,))
            elif kind == "profile_ref":
                db.execute("UPDATE model_profiles SET credential_ref='forged'")
            elif kind == "unknown_schema":
                db.execute("CREATE TABLE PluginStorage(Api_Key TEXT)")
                db.execute("INSERT INTO PluginStorage VALUES (?)", (PLAIN,))
            db.commit()
    forged = repack_claimed_excluded(stage, manifest, tmp_path / "forged.sfbak")
    archive_hash = hashlib.sha256(forged.read_bytes()).hexdigest()
    target = make_private_data(tmp_path / "target")
    before = tree_hashes(target)
    with pytest.raises(backup.BackupError, match="声明已排除凭证"):
        backup.prepare_restore(target, forged)
    assert tree_hashes(target) == before
    assert hashlib.sha256(forged.read_bytes()).hexdigest() == archive_hash
    assert not list((backup.recovery_root(target) / "staged").glob("*/review.json"))


def test_claimed_credential_validation_is_read_only_for_valid_and_invalid_content(tmp_path):
    root = make_private_data(tmp_path / "source")
    exported = backup.create_backup(root)
    stage = tmp_path / "staged"
    manifest = backup.verify_archive(exported["path"], extract_to=stage)
    relatives = [item["path"] for item in manifest["files"]]
    before = tree_hashes(stage)
    credentials.validate_excluded_backup(stage, manifest["database"], relatives)
    assert tree_hashes(stage) == before
    config = json.loads((stage / "config.json").read_text())
    config["llm_api_key"] = PLAIN
    write_json(stage / "config.json", config)
    before = tree_hashes(stage)
    with pytest.raises(credentials.CredentialExportError, match="声明已排除凭证"):
        credentials.validate_excluded_backup(stage, manifest["database"], relatives)
    assert tree_hashes(stage) == before


@pytest.mark.parametrize("scope", [None, credentials.EXCLUDED_RECONFIGURE, credentials.PRIVATE_ORIGINAL_USER])
@pytest.mark.parametrize("trigger_kind", ["privilege_and_credential", "self_reverting"])
def test_restore_rejects_persistent_triggers_before_automation_updates(tmp_path, scope, trigger_kind):
    source = make_private_data(tmp_path / "source")
    exported = backup.create_backup(source)
    stage = tmp_path / "triggered"
    manifest = backup.verify_archive(exported["path"], extract_to=stage)
    manifest["credential_portability"] = scope
    with closing(sqlite3.connect(stage / "scholarflow.db")) as db:
        db.execute("""CREATE TRIGGER hostile_after_option AFTER UPDATE ON conversation_options
            BEGIN
              UPDATE subscriptions SET active=1;
              UPDATE run_inputs SET status='queued', reason='synthetic re-enable';
              UPDATE conversation_options SET approval_mode='full_access';
              INSERT INTO model_credentials(id,profile_id,protected_value,legacy_generation,created_at)
              VALUES ('hostile',NULL,'synthetic-trigger-secret',0,CURRENT_TIMESTAMP);
            END""")
        if trigger_kind == "self_reverting":
            db.execute("""CREATE TRIGGER hostile_skip_readonly BEFORE UPDATE ON conversation_options
                BEGIN SELECT RAISE(IGNORE); END""")
        db.commit()
    malicious = repack_claimed_excluded(stage, manifest, tmp_path / "triggered.sfbak")
    target = make_private_data(tmp_path / "target")
    before = tree_hashes(target)
    with pytest.raises(backup.BackupError, match="持久触发器"):
        backup.prepare_restore(target, malicious)
    assert tree_hashes(target) == before
    assert not list((backup.recovery_root(target) / "staged").glob("*/review.json"))
    assert not list((backup.recovery_root(target) / "staged").glob("*/data/scholarflow.db"))


def test_fts5_virtual_table_survives_credential_export_and_restore(tmp_path):
    source = make_private_data(tmp_path / "source")
    with closing(sqlite3.connect(source / "scholarflow.db")) as db:
        db.execute("CREATE VIRTUAL TABLE paper_search USING fts5(title, body)")
        db.execute("INSERT INTO paper_search VALUES ('synthetic-title','synthetic-body')")
        db.commit()
    exported = backup.create_backup(source)
    target = tmp_path / "target"
    preview = backup.prepare_restore(target, exported["path"])
    restored_db = backup.recovery_root(target) / "staged" / preview["id"] / "data" / "scholarflow.db"
    assert not preview.get("error")
    with closing(sqlite3.connect(restored_db)) as db:
        assert db.execute("SELECT title,body FROM paper_search WHERE paper_search MATCH 'synthetic'").fetchone() == (
            "synthetic-title", "synthetic-body")
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone() is None
