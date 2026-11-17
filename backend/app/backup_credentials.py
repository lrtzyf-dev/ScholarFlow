"""Credential-free export transforms for managed application stores only.

This module intentionally has no configuration/engine imports: it is also used
before those components load during restart-only restore. User documents,
transcripts, source bytes and frozen credential references are never rewritten.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


EXCLUDED_RECONFIGURE = "credentials_excluded_reconfigure"
PRIVATE_ORIGINAL_USER = "protected_credentials_require_original_os_user"
MAX_CONFIGURATION_BYTES = 4 * 1024 * 1024


class CredentialExportError(ValueError):
    pass


_STRING_SETTINGS = frozenset({
    "llm_base_url", "llm_model", "embedding_base_url", "embedding_model",
    "approval_mode", "work_rules", "reasoning_effort",
})
_BOOL_SETTINGS = frozenset({"desktop_notifications", "memory_recall", "memory_capture"})
_NUMBER_SETTINGS = frozenset({"min_relevance_score", "max_agent_steps", "legacy_credential_generation"})
_CONFIG_SECRETS = frozenset({"llm_api_key", "embedding_api_key", "protected_credentials"})
_CONFIG_FIELDS = _STRING_SETTINGS | _BOOL_SETTINGS | _NUMBER_SETTINGS | _CONFIG_SECRETS | {"config_schema_version"}
_MCP_SECRETS = frozenset({"auth_protected", "headers", "bearer_token", "oauth", "env"})
_MCP_FIELDS = frozenset({"name", "transport", "enabled", "command", "args", "cwd", "url", "builtin"}) | _MCP_SECRETS

# These are exact managed stores/artifacts at DATA_DIR's root, not patterns
# applied to user files or arbitrary directory contents.
def excluded_managed_path(relative: str) -> bool:
    name = relative.casefold()
    if "/" in name:
        return False
    return (name in {"config.credential-recovery.json", ".env"}
            or (name.startswith((".config.json.", ".config.credential-recovery.json.")) and name.endswith(".tmp")))


def _bad_structure():
    raise CredentialExportError("应用凭证配置包含当前版本无法安全识别的结构，未生成备份；请先在设置中重新保存配置")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _bad_structure()
        result[key] = value
    return result


def _json(raw):
    if len(raw) > MAX_CONFIGURATION_BYTES:
        _bad_structure()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: _bad_structure())
    except (UnicodeError, ValueError, RecursionError):
        _bad_structure()
    if not isinstance(value, dict):
        _bad_structure()
    return value


def is_managed_configuration(relative: str) -> bool:
    return relative.casefold() in {"config.json", "mcp_servers.json"}


def sanitized_configuration(relative: str, raw: bytes) -> bytes:
    """Rewrite only schema-known credential slots, retaining ordinary settings."""
    value = _json(raw)
    if relative.casefold() == "config.json":
        if set(value) - _CONFIG_FIELDS:
            _bad_structure()
        if "config_schema_version" in value and (type(value["config_schema_version"]) is not int
                                                or value["config_schema_version"] not in (1, 2)):
            _bad_structure()
        if any(not isinstance(value[key], str) for key in _STRING_SETTINGS & value.keys()):
            _bad_structure()
        if any(type(value[key]) is not bool for key in _BOOL_SETTINGS & value.keys()):
            _bad_structure()
        if any(type(value[key]) not in (int, float) for key in _NUMBER_SETTINGS & value.keys()):
            _bad_structure()
        result = {key: item for key, item in value.items() if key not in _CONFIG_SECRETS}
        # Explicit empties override environment defaults on restore; protected
        # bytes are absent rather than transferred to another Windows account.
        result.update(llm_api_key="", embedding_api_key="")
        generation = value.get("legacy_credential_generation", 0)
        if type(generation) is not int or generation < 0:
            _bad_structure()
        result["legacy_credential_generation"] = generation + 1
    elif relative.casefold() == "mcp_servers.json":
        if set(value) - {"servers", "disabled_builtin"} or not isinstance(value.get("servers", []), list):
            _bad_structure()
        disabled = value.get("disabled_builtin", [])
        if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
            _bad_structure()
        result = {"servers": [], "disabled_builtin": disabled}
        for server in value.get("servers", []):
            if not isinstance(server, dict) or set(server) - _MCP_FIELDS:
                _bad_structure()
            for key in ("name", "transport", "command", "cwd", "url"):
                if key in server and not isinstance(server[key], str):
                    _bad_structure()
            for key in ("enabled", "builtin"):
                if key in server and type(server[key]) is not bool:
                    _bad_structure()
            if "args" in server and (not isinstance(server["args"], list)
                                     or any(not isinstance(item, str) for item in server["args"])):
                _bad_structure()
            cleaned = {key: item for key, item in server.items() if key not in _MCP_SECRETS}
            cleaned["enabled"] = False
            result["servers"].append(cleaned)
    else:
        _bad_structure()
    return json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


_TABLE_FIELDS = {
    "model_credentials": {"id", "profile_id", "legacy_generation", "protected_value", "revoked_at", "created_at"},
    "service_tool_credentials": {"id", "provider", "protected_value", "revoked_at", "created_at"},
    "model_profiles": {"id", "name", "base_url", "model_id", "reasoning_effort", "credential_ref", "enabled", "version",
                       "capabilities", "context_window_tokens", "max_output_tokens", "deleted_at", "created_at", "updated_at"},
    "service_tool_configurations": {"provider", "version", "enabled_services", "credential_ref", "updated_at"},
    "zotero_bindings": {"id", "conversation_id", "version", "user_id", "collection_key", "collection_name",
                        "protected_credential", "status", "access", "cursor", "last_batch_id", "last_error", "created_at", "updated_at"},
    "mcp_user_grants": {"server_id", "fingerprint", "generation", "state", "protected", "expires_at",
                        "refresh_owner", "refresh_until", "message"},
}
_REQUIRED_FIELDS = {
    "model_credentials": {"id", "protected_value"},
    "service_tool_credentials": {"id", "protected_value"},
    "model_profiles": {"credential_ref", "enabled", "version"},
    "service_tool_configurations": {"credential_ref", "enabled_services", "version"},
    "zotero_bindings": {"protected_credential", "status", "version"},
    "mcp_user_grants": {"protected", "state", "generation", "expires_at", "refresh_owner", "refresh_until", "message"},
}
_SECRET_COLUMNS = {"protected_value", "auth_protected", "protected_credentials", "api_key", "llm_api_key",
                   "embedding_api_key", "bearer_token", "client_secret", "access_token", "refresh_token", "protected_credential", "protected"}


def _database_schema(db):
    schema = db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master").fetchall()
    tables = {name for name, kind, _, _ in schema if kind == "table"}
    for name, kind, table, sql in schema:
        if table.casefold() in _TABLE_FIELDS and kind == "trigger":
            _bad_structure()
        if name.casefold() in _TABLE_FIELDS and kind != "table":
            _bad_structure()
    for table in tables:
        identifier = '"' + table.replace('"', '""') + '"'
        info = db.execute("PRAGMA table_info(" + identifier + ")").fetchall()
        columns = {row[1] for row in info}
        normalized_columns = {name.casefold() for name in columns}
        if table not in _TABLE_FIELDS:
            if (table.casefold() in _TABLE_FIELDS or normalized_columns & _SECRET_COLUMNS
                    or table.casefold().endswith("_credentials")):
                _bad_structure()
            continue
        if (columns != normalized_columns or columns - _TABLE_FIELDS[table]
                or not _REQUIRED_FIELDS[table] <= columns):
            _bad_structure()
        # A secret embedded in a schema default would survive DELETE.
        if any(row[1] in _SECRET_COLUMNS and row[4] is not None for row in info):
            _bad_structure()
    return tables


def validate_restore_database(path) -> None:
    """Reject persistent triggers before restore code executes any SQL writes.

    The application has no supported persistent triggers. FTS5 virtual tables
    and their shadow tables remain allowed; only sqlite_master trigger objects
    are rejected, regardless of their target table or name.
    """
    uri = "file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            trigger = db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' LIMIT 1"
            ).fetchone()
            if trigger:
                raise CredentialExportError("恢复数据库包含应用不使用的持久触发器，拒绝恢复且未修改当前数据")
    except sqlite3.Error:
        raise CredentialExportError("恢复数据库对象无法安全验证，拒绝恢复且未修改当前数据") from None


def validate_excluded_backup(root, database, relatives):
    """Read-only validation of a claimed credential-free, extracted archive.

    A manifest is an untrusted assertion. Never silently sanitize an imported
    archive or relabel old/unknown archives as credential-free.
    """
    root = Path(root)
    validate_restore_database(root / database)
    try:
        for relative in relatives:
            if excluded_managed_path(relative):
                _bad_structure()
            if not is_managed_configuration(relative):
                continue
            with (root / relative).open("rb") as stream:
                raw = stream.read(MAX_CONFIGURATION_BYTES + 1)
            value = _json(raw)
            sanitized_configuration(relative, raw)  # Structural validation only; no writes.
            if relative.casefold() == "config.json":
                if (any(value.get(key) != "" for key in ("llm_api_key", "embedding_api_key"))
                        or value.get("protected_credentials") not in (None, {})):
                    _bad_structure()
            else:
                for server in value.get("servers", []):
                    if (server.get("enabled") is not False
                            or any(server.get(key) not in (None, "", {}, []) for key in _MCP_SECRETS)):
                        _bad_structure()
        uri = "file:" + quote((root / database).resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            tables = _database_schema(db)
            for table in ("model_credentials", "service_tool_credentials"):
                if table in tables and db.execute("SELECT count(*) FROM " + table).fetchone() != (0,):
                    _bad_structure()
            if "model_profiles" in tables and db.execute(
                    "SELECT count(*) FROM model_profiles WHERE credential_ref IS NOT NULL OR enabled != 0 OR enabled IS NULL").fetchone() != (0,):
                _bad_structure()
            if "service_tool_configurations" in tables:
                for reference, enabled in db.execute("SELECT credential_ref,enabled_services FROM service_tool_configurations"):
                    if reference is not None or json.loads(enabled) != []:
                        _bad_structure()
            if "zotero_bindings" in tables and db.execute(
                    "SELECT count(*) FROM zotero_bindings WHERE protected_credential IS NOT NULL OR status != 'needs_configuration' OR status IS NULL").fetchone() != (0,):
                _bad_structure()
            if "mcp_user_grants" in tables and db.execute(
                    "SELECT count(*) FROM mcp_user_grants WHERE protected IS NOT NULL OR state != 'unauthorized' OR state IS NULL "
                    "OR expires_at != 0 OR refresh_owner IS NOT NULL OR refresh_until != 0 OR message != ''").fetchone() != (0,):
                _bad_structure()
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA freelist_count").fetchone() != (0,):
                _bad_structure()
    except CredentialExportError:
        raise CredentialExportError("备份声明已排除凭证，但内容不符或无法安全验证；未准备恢复，原数据未改动") from None
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise CredentialExportError("备份声明已排除凭证，但内容不符或无法安全验证；未准备恢复，原数据未改动") from None


def sanitized_database(path) -> dict:
    """Scrub only an isolated SQLite snapshot, then rebuild its free pages.

Frozen run/service receipt references remain unchanged and point to missing
credential records. They must fail reauthorization rather than silently inherit
an unrelated new default or become anonymous requests.
"""
    try:
        with closing(sqlite3.connect(path, timeout=5)) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            if db.execute("PRAGMA secure_delete").fetchone() != (1,):
                raise CredentialExportError("当前数据库无法安全排除凭证，未生成备份")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
                _bad_structure()
            tables = _database_schema(db)
            removed = {}
            for table in ("model_credentials", "service_tool_credentials"):
                if table in tables:
                    removed[table] = db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                    db.execute("DELETE FROM " + table)
            if "model_profiles" in tables:
                db.execute("UPDATE model_profiles SET credential_ref=NULL,enabled=0,version=version+1")
            if "service_tool_configurations" in tables:
                db.execute("UPDATE service_tool_configurations SET credential_ref=NULL,enabled_services='[]',version=version+1")
            if "zotero_bindings" in tables:
                removed["zotero_credentials"] = db.execute("SELECT count(*) FROM zotero_bindings WHERE protected_credential IS NOT NULL").fetchone()[0]
                db.execute("UPDATE zotero_bindings SET protected_credential=NULL,status='needs_configuration',version=version+1")
            if "mcp_user_grants" in tables:
                removed["mcp_oauth_grants"] = db.execute("SELECT count(*) FROM mcp_user_grants WHERE protected IS NOT NULL").fetchone()[0]
                db.execute("UPDATE mcp_user_grants SET protected=NULL,state='unauthorized',generation=generation+1,"
                           "expires_at=0,refresh_owner=NULL,refresh_until=0,message=''")
            db.commit()
            db.execute("VACUUM")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA freelist_count").fetchone() != (0,):
                raise CredentialExportError("凭证排除后的数据库完整性校验未通过")
            return {"removed_records": removed, "secure_delete": True, "vacuum": True}
    except sqlite3.Error:
        raise CredentialExportError("备份副本无法安全排除数据库凭证，原运行数据库未改动") from None
