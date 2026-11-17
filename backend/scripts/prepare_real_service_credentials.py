"""Prepare protected, isolated credentials for explicitly authorized tool tests.

This opt-in helper uses the already authorized model key for Search/Fetch/OCR
test configuration only. It never changes product defaults, reads user database
tables, copies documents, enables a provider automatically, or sends a request.
The normal runner still requires --run-real and --allow-paid-tools separately.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
PREPARATION_ROOT = BACKEND.parent / "verification" / "test-temp-current"
SERVICES = ["fetch", "ocr", "search"]


class PreparationError(ValueError):
    """Stable non-secret failure codes; never include supplied config values."""


def _no_link(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise PreparationError("LinkedPreparationPathRejected")
    return path


def _no_link_ancestors(path):
    for parent in (path, *path.parents):
        _no_link(parent)


def _destination(source, destination):
    root = PREPARATION_ROOT.absolute()
    if not root.parent.is_dir():
        raise PreparationError("PreparationParentMustExist")
    _no_link_ancestors(root)
    root = root.resolve()
    target = Path(destination).absolute() if destination is not None else root / ("deepseek-services-" + uuid4().hex)
    _no_link(target)
    if target.exists():
        raise PreparationError("PreparationDirectoryMustBeNew")
    if target.parent.resolve() != root or target.name in ("", ".", ".."):
        raise PreparationError("PreparationMustBeImmediateChildOfTestRoot")
    source = Path(source).resolve()
    if source == root or source.is_relative_to(root) or root.is_relative_to(source):
        raise PreparationError("SourceAndTestPreparationMustBeSeparate")
    return source, root, root / target.name


def _write_new(path, payload):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _service_database(path, encrypted):
    # Full columns of the two production service-tool models, deliberately
    # without importing app.db/app.config (which can restore/create app data).
    # A schema contract test compares these tables to actual ORM metadata.
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" ")
    credential_id = uuid4().hex
    with path.open("xb"):
        pass
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection, connection:
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript("""
CREATE TABLE service_tool_configurations (
 provider VARCHAR(32) NOT NULL PRIMARY KEY,
 version INTEGER NOT NULL,
 enabled_services JSON NOT NULL,
 credential_ref VARCHAR(32),
 updated_at DATETIME NOT NULL
);
CREATE TABLE service_tool_credentials (
 id VARCHAR(32) NOT NULL PRIMARY KEY,
 provider VARCHAR(32) NOT NULL,
 protected_value TEXT NOT NULL,
 revoked_at DATETIME,
 created_at DATETIME NOT NULL
);
CREATE INDEX ix_service_tool_credentials_provider ON service_tool_credentials (provider);
""")
        connection.execute("INSERT INTO service_tool_credentials VALUES (?,?,?,?,?)",
            (credential_id, "deepseek", encrypted, None, stamp))
        connection.execute("INSERT INTO service_tool_configurations VALUES (?,?,?,?,?)",
            ("deepseek", 1, json.dumps(SERVICES), credential_id, stamp))


def prepare_credentials(credential_directory, destination=None, *, authorized=False):
    if authorized is not True:
        raise PreparationError("ExplicitAuthorizedTestFlagRequired")
    source, root, target = _destination(credential_directory, destination)
    if not source.is_dir():
        raise PreparationError("SourceCredentialDirectoryRequired")
    source_config = _no_link(source / "config.json")
    if not source_config.is_file() or source_config.stat().st_size > 1024 * 1024:
        raise PreparationError("SourceModelConfigurationRequired")
    before = source_config.read_bytes()
    def unique_object(pairs):
        value = {}
        for name, item in pairs:
            if name in value:
                raise PreparationError("DuplicateSourceConfigurationField")
            value[name] = item
        return value
    try:
        source_fields = json.loads(before.decode("utf-8"), object_pairs_hook=unique_object)
    except Exception:
        raise PreparationError("SourceModelConfigurationUnavailable") from None
    if (not isinstance(source_fields, dict) or ("protected_credentials" in source_fields
            and not isinstance(source_fields["protected_credentials"], dict))):
        raise PreparationError("InvalidPresentProtectedConfiguration")
    # Both imports are side-effect free. In particular do not import settings,
    # ORM modules, the application, or any database from the source directory.
    from app.credential_protection import protect, unprotect
    from scripts.verify_real_acceptance import model_configuration, protected_service_configuration
    try:
        configuration = model_configuration(source, unprotect)
    except Exception:
        raise PreparationError("SourceModelConfigurationUnavailable") from None
    secret = configuration["api_key"].strip()
    if (not secret or len(secret) > 4096 or not secret.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in secret)):
        raise PreparationError("SingleLineServiceCredentialRequired")
    base, model = configuration["base_url"], configuration["model_id"]
    if not isinstance(base, str) or not isinstance(model, str) or not model.strip():
        raise PreparationError("ConfiguredDeepSeekModelRequired")
    try:
        endpoint = urlsplit(base)
        port = endpoint.port
    except ValueError:
        raise PreparationError("ConfiguredHTTPSDeepSeekOriginRequired") from None
    if (endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com"
            or port not in (None, 443) or endpoint.username is not None or endpoint.password is not None or endpoint.query or endpoint.fragment):
        raise PreparationError("ConfiguredHTTPSDeepSeekOriginRequired")
    try:
        model_protected, service_protected = protect(secret), protect(secret)
        if any(not isinstance(value, str) or not value.startswith("dpapi:") or secret in value
               or unprotect(value) != secret for value in (model_protected, service_protected)):
            raise PreparationError("ProtectedCredentialRoundtripRequired")
    except Exception:
        raise PreparationError("ProtectedCredentialRoundtripRequired") from None
    config = {"config_schema_version": 2, "llm_api_key": "", "embedding_api_key": "",
        "llm_base_url": base, "llm_model": model,
        "protected_credentials": {"llm_api_key": model_protected}}
    public = {"kind": "authorized_test_credentials_prepared", "prepared": True,
        "credentials_dir": str(target), "model_config": str(target / "config.json"),
        "service_config_db": str(target / "service-tools.db"),
        "source_storage_kind": configuration["storage_kind"], "target_storage_kind": "windows_user_dpapi",
        "enabled_services": list(SERVICES), "real_provider_calls": 0,
        "source_modified": False, "product_default_modified": False}
    if secret in json.dumps(config, ensure_ascii=False) or secret in json.dumps(public, ensure_ascii=False):
        raise PreparationError("CredentialInNonSecretMetadataRejected")
    if source_config.read_bytes() != before:
        raise PreparationError("SourceConfigurationChangedDuringRead")
    root.mkdir(exist_ok=True)
    _no_link(root)
    target.mkdir()  # Must be absent, even if another invocation raced us.
    _no_link(target)
    if target.resolve() != target:
        raise PreparationError("PreparationPathChanged")
    _write_new(target / "config.json", config)
    _service_database(target / "service-tools.db", service_protected)
    prepared_model = model_configuration(target, unprotect)
    prepared_service = protected_service_configuration(target / "service-tools.db", target)
    if (prepared_model["api_key"] != secret or prepared_service is None
            or prepared_service["enabled_services"] != SERVICES or unprotect(prepared_service["protected_value"]) != secret):
        raise PreparationError("PreparedRunnerCredentialContractFailed")
    _write_new(target / "prepared.json", public)
    return public


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-for-authorized-tests", action="store_true")
    parser.add_argument("--credentials-dir", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    if not args.prepare_for_authorized_tests:
        print(json.dumps({"kind": "authorized_test_credentials_plan", "prepared": False,
            "credentials_read": False, "real_provider_calls": 0}))
        return 0
    if args.credentials_dir is None:
        parser.error("Authorized preparation requires --credentials-dir")
    try:
        result = prepare_credentials(args.credentials_dir, args.destination, authorized=True)
    except Exception as error:
        print(json.dumps({"kind": "authorized_test_credentials_failed", "prepared": False,
            "error_kind": type(error).__name__, "real_provider_calls": 0}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(BACKEND))
    raise SystemExit(main())
