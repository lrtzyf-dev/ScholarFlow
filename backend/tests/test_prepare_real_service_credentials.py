"""Authorized-test preparation uses synthetic credentials and no network."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from app import credential_protection
from scripts import prepare_real_service_credentials as prepare
from scripts import verify_real_acceptance as runner


SECRET = "synthetic-authorized-test-credential-6e4344da64d64f54"
OTHER_SECRET = "synthetic-embedding-must-never-copy-f6e62016d217"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    source = tmp_path / "authorized-source"; source.mkdir()
    (source / "config.json").write_text(json.dumps({"llm_api_key": SECRET,
        "llm_base_url": "https://api.deepseek.com/v1", "llm_model": "DeepSeek-Exact-ID",
        "embedding_api_key": OTHER_SECRET, "user_documents": ["private-file-must-not-copy"],
        "protected_credentials": {"embedding_api_key": "unrelated-protected-setting"}}), encoding="utf-8")
    with sqlite3.connect(source / "scholarflow.db") as database:
        database.execute("CREATE TABLE private_documents (content TEXT)")
        database.execute("INSERT INTO private_documents VALUES ('private-original-must-not-copy')")
    (source / "private.txt").write_text("private-original-must-not-copy", encoding="utf-8")
    root = tmp_path / "verification" / "test-temp-current"; root.parent.mkdir()
    monkeypatch.setattr(prepare, "PREPARATION_ROOT", root)
    protected = {}
    def protect(value):
        opaque = "dpapi:opaque-synthetic-" + uuid4().hex
        protected[opaque] = value
        return opaque
    def unprotect(value):
        if value not in protected:
            raise ValueError("synthetic-invalid-protected-value")
        return protected[value]
    monkeypatch.setattr(credential_protection, "protect", protect)
    monkeypatch.setattr(credential_protection, "unprotect", unprotect)
    def no_network(*args, **kwargs):
        pytest.fail("Credential preparation must not dispatch network requests")
    import httpx
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    return {"source": source, "root": root, "destination": root / "new-credentials", "before": before,
        "protected": protected, "protect": protect, "unprotect": unprotect}


def create(item):
    return prepare.prepare_credentials(item["source"], item["destination"], authorized=True)


def test_default_mode_reads_no_credentials_or_protection_and_writes_nothing(isolated, monkeypatch, capsys):
    def forbidden(*a, **k): pytest.fail("Missing opt-in attempted preparation")
    monkeypatch.setattr(runner, "model_configuration", forbidden)
    monkeypatch.setattr(credential_protection, "protect", forbidden)
    assert prepare.main(["--credentials-dir", str(isolated["source"]), "--destination", str(isolated["destination"])]) == 0
    result = json.loads(capsys.readouterr().out)
    assert not result["credentials_read"] and not result["prepared"] and result["real_provider_calls"] == 0
    assert not isolated["root"].exists()
    with pytest.raises(prepare.PreparationError, match="ExplicitAuthorizedTestFlag"):
        prepare.prepare_credentials(isolated["source"], isolated["destination"])


def test_authorized_preparation_roundtrips_through_real_runner_contract_without_copying_user_data(isolated, monkeypatch):
    source = isolated["source"]
    original_connect = sqlite3.connect
    def only_new_database(database, *a, **k):
        assert str(source / "scholarflow.db") not in str(database), "Preparation opened the original user DB"
        return original_connect(database, *a, **k)
    monkeypatch.setattr(prepare.sqlite3, "connect", only_new_database)
    result = create(isolated); destination = isolated["destination"]
    assert result["source_storage_kind"] == "legacy_plaintext_read_only_memory_fallback_pending_migration"
    assert result["target_storage_kind"] == "windows_user_dpapi" and result["real_provider_calls"] == 0
    assert sorted(p.name for p in destination.iterdir()) == ["config.json", "prepared.json", "service-tools.db"]
    model = runner.model_configuration(destination, isolated["unprotect"])
    service = runner.protected_service_configuration(destination / "service-tools.db", destination)
    assert model["api_key"] == SECRET and model["model_id"] == "DeepSeek-Exact-ID"
    assert model["base_url"] == "https://api.deepseek.com/v1" and model["storage_kind"] == "protected_configuration"
    assert service["enabled_services"] == ["fetch", "ocr", "search"]
    assert isolated["unprotect"](service["protected_value"]) == SECRET
    config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
    assert config["llm_api_key"] == config["embedding_api_key"] == ""
    assert set(config["protected_credentials"]) == {"llm_api_key"}
    for path in destination.iterdir():
        raw = path.read_bytes()
        assert SECRET.encode() not in raw and OTHER_SECRET.encode() not in raw
        assert b"private-original-must-not-copy" not in raw and b"private-file-must-not-copy" not in raw
    assert {p.name: p.read_bytes() for p in source.iterdir()} == isolated["before"]


def test_two_table_schema_matches_actual_production_models(isolated):
    from app.service_tool_models import ServiceToolCredential, ServiceToolConfiguration
    create(isolated)
    with sqlite3.connect(isolated["destination"] / "service-tools.db") as database:
        names = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == {"service_tool_credentials", "service_tool_configurations"}
        for model in (ServiceToolCredential, ServiceToolConfiguration):
            actual = {r[1]: (r[2], bool(r[3]), bool(r[5])) for r in database.execute("PRAGMA table_info(" + model.__tablename__ + ")")}
            expected = {c.name: (str(c.type), not c.nullable, c.primary_key) for c in model.__table__.columns}
            assert actual == expected


@pytest.mark.parametrize("bad", [None, "malformed", {"llm_api_key": ""}, {"llm_api_key": None}, {"llm_api_key": "dpapi:unreadable"}])
def test_bad_present_protected_configuration_cannot_fall_back_to_legacy(isolated, bad):
    path = isolated["source"] / "config.json"; fields = json.loads(path.read_text(encoding="utf-8"))
    fields["protected_credentials"] = bad; path.write_text(json.dumps(fields), encoding="utf-8"); before = path.read_bytes()
    with pytest.raises(prepare.PreparationError): create(isolated)
    assert not isolated["root"].exists() and path.read_bytes() == before


def test_valid_protected_model_key_wins_over_legacy(isolated):
    path = isolated["source"] / "config.json"; fields = json.loads(path.read_text(encoding="utf-8"))
    fields["protected_credentials"] = {"llm_api_key": isolated["protect"](SECRET)}
    fields["llm_api_key"] = "synthetic-stale-legacy-must-not-use"
    path.write_text(json.dumps(fields), encoding="utf-8"); before = path.read_bytes()
    result = create(isolated)
    assert result["source_storage_kind"] == "protected_configuration"
    assert runner.model_configuration(isolated["destination"], isolated["unprotect"])["api_key"] == SECRET
    assert path.read_bytes() == before


def test_duplicate_protected_field_is_rejected_before_legacy_fallback(isolated):
    path = isolated["source"] / "config.json"
    path.write_text(json.dumps({"llm_api_key": SECRET})[:-1] + ',"protected_credentials":null,"protected_credentials":{}}', encoding="utf-8")
    with pytest.raises(prepare.PreparationError): create(isolated)
    assert not isolated["root"].exists()


@pytest.mark.parametrize("case", ["existing", "outside", "nested", "source_parent", "source_inside_root"])
def test_destination_is_new_and_strictly_within_separate_test_root(isolated, case):
    item = deepcopy(isolated)
    if case == "existing": item["destination"].mkdir(parents=True)
    elif case == "outside": item["destination"] = item["root"].parent / "outside"
    elif case == "nested": item["destination"] = item["root"] / "nested" / "leaf"
    elif case == "source_parent": item["source"] = item["root"].parent
    elif case == "source_inside_root": item["source"] = item["root"] / "source"
    with pytest.raises(prepare.PreparationError): create(item)
    assert {p.name: p.read_bytes() for p in isolated["source"].iterdir()} == isolated["before"]


def test_protection_failure_is_sanitized_and_writes_no_target(isolated, monkeypatch, capsys):
    def failed(value): raise ValueError("PRIVATE-EXCEPTION-" + value)
    monkeypatch.setattr(credential_protection, "protect", failed)
    assert prepare.main(["--prepare-for-authorized-tests", "--credentials-dir", str(isolated["source"]),
        "--destination", str(isolated["destination"])]) == 1
    output = capsys.readouterr().out
    assert SECRET not in output and "PRIVATE-EXCEPTION" not in output
    assert json.loads(output)["error_kind"] == "PreparationError" and not isolated["root"].exists()


def test_nonopaque_protection_or_wrong_roundtrip_never_writes_target(isolated, monkeypatch):
    monkeypatch.setattr(credential_protection, "protect", lambda value: "dpapi:" + value)
    with pytest.raises(prepare.PreparationError): create(isolated)
    assert not isolated["root"].exists()


def test_mid_write_failure_leaves_no_plaintext_or_success_receipt(isolated, monkeypatch):
    def failure(*args): raise OSError("synthetic-disk-failure")
    monkeypatch.setattr(prepare, "_service_database", failure)
    with pytest.raises(OSError): create(isolated)
    assert not (isolated["destination"] / "prepared.json").exists()
    assert SECRET.encode() not in (isolated["destination"] / "config.json").read_bytes()
    assert {p.name: p.read_bytes() for p in isolated["source"].iterdir()} == isolated["before"]


@pytest.mark.parametrize("base", ["http://api.deepseek.com/v1", "https://example.test/v1",
    "https://api.deepseek.com:8443/v1", "https://api.deepseek.com:invalid/v1",
    "https://user:password@api.deepseek.com/v1", "https://api.deepseek.com/v1?api_key=forbidden", "https://api.deepseek.com/v1#fragment"])
def test_only_configured_https_deepseek_origin_without_credential_metadata_is_accepted(isolated, base):
    path = isolated["source"] / "config.json"; fields = json.loads(path.read_text(encoding="utf-8"))
    fields["llm_base_url"] = base; path.write_text(json.dumps(fields), encoding="utf-8")
    with pytest.raises(prepare.PreparationError): create(isolated)
    assert not isolated["root"].exists()


def test_source_change_during_memory_read_is_not_saved_to_new_test_directory(isolated, monkeypatch):
    original = runner.model_configuration; path = isolated["source"] / "config.json"
    def change(*args):
        result = original(*args)
        fields = json.loads(path.read_text(encoding="utf-8")); fields["llm_model"] = "externally-changed"
        path.write_text(json.dumps(fields), encoding="utf-8")
        return result
    monkeypatch.setattr(runner, "model_configuration", change)
    with pytest.raises(prepare.PreparationError, match="SourceConfigurationChanged"): create(isolated)
    assert not isolated["root"].exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction ancestry check")
def test_junction_in_higher_target_ancestor_cannot_redirect_preparation(isolated, monkeypatch):
    anchor = isolated["root"].parent.parent
    real = anchor / "real-target"; real.mkdir()
    junction = anchor / "linked-ancestor"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)], capture_output=True, timeout=15)
    assert result.returncode == 0
    verification = junction / "verification"; verification.mkdir()
    monkeypatch.setattr(prepare, "PREPARATION_ROOT", verification / "test-temp-current")
    with pytest.raises(prepare.PreparationError, match="LinkedPreparationPathRejected"):
        prepare.prepare_credentials(isolated["source"], verification / "test-temp-current" / "new", authorized=True)
    assert not (real / "verification" / "test-temp-current").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Cold interpreter Windows DPAPI preparation")
def test_cold_process_preparation_never_imports_application_configuration_or_database(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    (source / "config.json").write_text(json.dumps({"llm_api_key": SECRET,
        "llm_base_url": "https://api.deepseek.com/v1", "llm_model": "deepseek-flash"}), encoding="utf-8")
    root = tmp_path / "verification" / "test-temp-current"; root.parent.mkdir()
    destination = root / "cold-protected"
    code = """import sys,importlib.util,socket,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
def blocked(*args,**kwargs):raise AssertionError('NetworkForbidden')
socket.socket.connect=blocked
spec=importlib.util.spec_from_file_location('prepare_isolated',Path(sys.argv[1])/'scripts/prepare_real_service_credentials.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module.PREPARATION_ROOT=Path(sys.argv[3]).parent
assert module.main(['--prepare-for-authorized-tests','--credentials-dir',sys.argv[2],'--destination',sys.argv[3]])==0
assert 'app.config'not in sys.modules and 'app.db'not in sys.modules and 'app.main'not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code, str(prepare.BACKEND), str(source), str(destination)],
        capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["real_provider_calls"] == 0 and SECRET not in result.stdout + result.stderr
    assert runner.model_configuration(destination, credential_protection.unprotect)["api_key"] == SECRET


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI user protection integration")
def test_real_windows_dpapi_with_synthetic_key_and_legacy_source(tmp_path, monkeypatch):
    source = tmp_path / "original"; source.mkdir()
    (source / "config.json").write_text(json.dumps({"llm_api_key": SECRET,
        "llm_base_url": "https://api.deepseek.com/v1", "llm_model": "deepseek-flash"}), encoding="utf-8")
    before = (source / "config.json").read_bytes()
    root = tmp_path / "verification" / "test-temp-current"; root.parent.mkdir()
    monkeypatch.setattr(prepare, "PREPARATION_ROOT", root)
    result = prepare.prepare_credentials(source, root / "real-dpapi-synthetic", authorized=True)
    assert runner.model_configuration(Path(result["credentials_dir"]), credential_protection.unprotect)["api_key"] == SECRET
    assert (source / "config.json").read_bytes() == before
    assert all(SECRET.encode() not in p.read_bytes() for p in Path(result["credentials_dir"]).iterdir())
