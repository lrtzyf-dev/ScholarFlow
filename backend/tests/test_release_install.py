"""Real filesystem/SQLite/OS-lock tests; candidates are synthetic, never user installs."""
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from app import release_install as module
from app.atomic_files import atomic_write_json
from app.release_files import digest
from app.release_info import ReleaseError, canonical_json
from app.release_schema import schema_snapshot, create_target_schema

ROOT = Path(__file__).resolve().parents[2]


class PowerLoss(BaseException):
    pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    private = Ed25519PrivateKey.generate()
    trust = {"test-publisher": base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}
    data = tmp_path / "data"; data.mkdir()
    with closing(sqlite3.connect(data / "scholarflow.db")) as db:
        db.executescript("CREATE TABLE samples(value TEXT); INSERT INTO samples VALUES ('original'); CREATE TABLE runtime_runs(status TEXT);")
    (data / "paper.txt").write_text("fixed source", encoding="utf-8")
    (data / "config.json").write_text('{"llm_model":"old","llm_api_key":"synthetic-never-export"}', encoding="utf-8")
    schema = schema_snapshot(data / "scholarflow.db")
    target = tmp_path / "installed"
    def candidate(name, build=2):
        folder = tmp_path / name; (folder / "payload").mkdir(parents=True)
        (folder / "payload/ScholarFlow.exe").write_bytes(("synthetic-executable-" + str(build)).encode())
        definition = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
        definition["build_number"] = build
        value = {"format": module.FORMAT, "schema_version": 1, "definition": definition,
            "input_build_id": "a" * 64, "target_schema": schema, "entrypoint": "ScholarFlow.exe",
            "files": module._files(folder / "payload"), "qualification": "assembled_candidate_not_public_release"}
        value["candidate_id"] = digest(value)
        atomic_write_json(folder / module.MANIFEST, value)
        sign(folder, value)
        return folder
    def sign(folder, value):
        atomic_write_json(folder / "release-signature.json", {"algorithm": "Ed25519", "key_id": "test-publisher",
            "signature": base64.b64encode(private.sign(canonical_json(value))).decode()})
    current = candidate("old", 1)
    shutil.copytree(current, target)
    new = candidate("new", 2)
    health = []
    def healthy(program, manifest, stage, receipt):
        health.append((program, manifest, stage, receipt))
        assert stage != data and stage.is_dir()
        raw = (stage / "config.json").read_text(encoding="utf-8")
        assert "synthetic-never-export" not in raw
        assert schema_snapshot(stage / "scholarflow.db") == schema
        assert (stage / "paper.txt").read_text(encoding="utf-8") == "fixed source"
    monkeypatch.setattr(module, "_health", healthy)
    return {"data": data, "target": target, "candidate": new, "old": current, "trust": trust,
        "create": candidate, "sign": sign, "health": health}


def install(s, **kwargs):
    return module.install(s["candidate"], s["target"], s["data"], trusted_keys=s["trust"], **kwargs)


def test_signed_upgrade_keeps_data_and_previous_with_sanitized_backup(setup):
    s = setup
    before = {p.name: p.read_bytes() for p in s["data"].iterdir()}
    result = install(s)
    assert result["state"] == "committed" and not result["data_modified"]
    assert result["backup_retained"] and not result["public_release_ready"]
    assert len(s["health"]) == 1
    assert module.verify_candidate(s["target"], s["trust"])["definition"]["build_number"] == 2
    folder = s["target"].parent / ".installed-updates" / result["transaction_id"]
    assert module.verify_candidate(folder / "previous", s["trust"])["definition"]["build_number"] == 1
    for name, content in before.items(): assert (s["data"] / name).read_bytes() == content
    assert module.recover(s["target"], s["data"], trusted_keys=s["trust"])["state"] == "committed"


@pytest.mark.parametrize("boundary", ["ready", "previous_moved", "candidate_activated", "before_commit", "committed"])
def test_interruption_recovered_from_durable_filesystem_state(setup, monkeypatch, boundary):
    s = setup
    before = module._tree_fingerprint(s["target"])
    def crash(name):
        if name == boundary: raise PowerLoss()
    monkeypatch.setattr(module, "_checkpoint", crash)
    with pytest.raises(PowerLoss): install(s)
    if boundary != "ready":
        from app.runtime_owner import RuntimeOwner, RuntimeInUse
        owner = RuntimeOwner(s["data"])
        with pytest.raises(RuntimeInUse, match="恢复"): owner._acquire("service")
    result = module.recover(s["target"], s["data"], trusted_keys=s["trust"])
    if boundary == "committed":
        assert result["state"] == "committed"
    else:
        assert result["state"] == "rolled_back"
        assert module._tree_fingerprint(s["target"]) == before
        assert module.recover(s["target"], s["data"], trusted_keys=s["trust"])["state"] == "rolled_back"
    assert not module.barrier_path(s["data"]).exists()


def test_health_failure_restores_old_program_and_preserves_all_data(setup, monkeypatch):
    s = setup
    before = module._tree_fingerprint(s["target"])
    def fail(*args): raise ReleaseError("synthetic health failure")
    monkeypatch.setattr(module, "_health", fail)
    with pytest.raises(ReleaseError, match="synthetic health"): install(s)
    assert module._tree_fingerprint(s["target"]) == before
    assert (s["data"] / "paper.txt").read_text(encoding="utf-8") == "fixed source"


def test_space_exhaustion_refuses_before_replacement(setup, monkeypatch):
    s = setup; before = module._tree_fingerprint(s["target"])
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 1})())
    with pytest.raises(ReleaseError, match="空间不足"): install(s)
    assert module._tree_fingerprint(s["target"]) == before


def test_running_backend_and_other_updater_are_excluded_by_real_os_lock(setup):
    s = setup
    state = s["target"].parent / ".installed-updates"; state.mkdir()
    for path in [s["data"] / ".runtime.lock", state / "install.lock"]:
        with module._exclusive(path):
            with pytest.raises(ReleaseError, match="占用"): install(s)


@pytest.mark.parametrize("bad", ["unsigned", "wrong_trust", "tamper", "extra", "nested_secret", "changed_contract", "new_schema", "wrong_direction", "active_task"])
def test_reject_untrusted_incompatible_or_active_update(setup, bad):
    s = setup; before = module._tree_fingerprint(s["target"])
    value = json.loads((s["candidate"] / module.MANIFEST).read_text(encoding="utf-8"))
    if bad == "unsigned": (s["candidate"] / "release-signature.json").unlink()
    elif bad == "wrong_trust": s["trust"] = {}
    elif bad == "tamper": (s["candidate"] / "payload/ScholarFlow.exe").write_bytes(b"tampered")
    elif bad in {"extra", "nested_secret"}:
        path = s["candidate"] / (".hidden.exe" if bad == "extra" else "payload/.env")
        path.write_text("unlisted")
    elif bad == "active_task":
        with closing(sqlite3.connect(s["data"] / "scholarflow.db")) as db:
            db.execute("INSERT INTO runtime_runs VALUES ('running')"); db.commit()
    else:
        if bad == "changed_contract": value["definition"]["data_contract"]["checkpoint_protocol"] += 1
        elif bad == "new_schema":
            value["target_schema"]["objects"].append({"type": "table", "name": "future", "table": "future", "sql": "CREATE TABLE future(x)"})
            value["target_schema"]["sha256"] = digest({k: v for k, v in value["target_schema"].items() if k != "sha256"})
        else: value["definition"]["build_number"] = 0
        value["candidate_id"] = digest({k: v for k, v in value.items() if k != "candidate_id"})
        atomic_write_json(s["candidate"] / module.MANIFEST, value); s["sign"](s["candidate"], value)
    with pytest.raises(ReleaseError): install(s)
    assert module._tree_fingerprint(s["target"]) == before
    assert not s["health"]


def test_rollback_requires_explicit_direction_and_same_schema(setup):
    s = setup; install(s)
    s["candidate"] = s["old"]
    with pytest.raises(ReleaseError, match="方向"): install(s)
    assert install(s, direction="rollback")["state"] == "committed"
    assert module.verify_candidate(s["target"], s["trust"])["definition"]["build_number"] == 1


def test_file_occupation_refuses_and_recovers_without_erasing_previous(setup, monkeypatch):
    s = setup; before = module._tree_fingerprint(s["target"])
    original = Path.rename
    def occupied(path, target):
        if path == s["target"]: raise PermissionError("synthetic occupied executable")
        return original(path, target)
    monkeypatch.setattr(Path, "rename", occupied)
    with pytest.raises(PermissionError): install(s)
    assert module._tree_fingerprint(s["target"]) == before


def test_recovery_refuses_unrelated_replacement(setup, monkeypatch):
    s = setup
    def crash(name):
        if name == "candidate_activated": raise PowerLoss()
    monkeypatch.setattr(module, "_checkpoint", crash)
    with pytest.raises(PowerLoss): install(s)
    (s["target"] / "unrelated.txt").write_text("do not overwrite")
    with pytest.raises(ReleaseError, match="候选不符"):
        module.recover(s["target"], s["data"], trusted_keys=s["trust"])
    assert (s["target"] / "unrelated.txt").read_text() == "do not overwrite"


@pytest.mark.parametrize("overlap", ["data_in_target", "target_in_data", "candidate_is_target"])
def test_reject_overlapping_paths(setup, overlap):
    s = setup
    if overlap == "data_in_target":
        s["data"] = s["target"] / "user-data"; s["data"].mkdir()
    elif overlap == "target_in_data": s["target"] = s["data"] / "app"
    else: s["candidate"] = s["target"]
    with pytest.raises(ReleaseError, match="互不包含"): install(s)


def test_health_protocol_is_fixed_and_binds_nonce_identity_and_schema(setup, monkeypatch):
    s = setup
    # Restore the actual subprocess adapter; no executable is invoked in this probe.
    from importlib import reload
    health = reload(module)._health
    stage = s["target"].parent / "health-data"; stage.mkdir()
    receipt = stage.parent / "receipt.json"
    manifest = module.verify_candidate(s["candidate"], s["trust"])
    def child(command, **kwargs):
        assert command[1] == "--upgrade-health-check" and kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["cwd"] == s["candidate"] / "payload"
        nonce = command[command.index("--nonce") + 1]
        atomic_write_json(receipt, {"format": "scholarflow-install-health", "version": 1, "nonce": nonce,
            "definition": manifest["definition"], "schema_sha256": manifest["target_schema"]["sha256"],
            "input_build_id": manifest["input_build_id"],
            "scope": "disposable_database_only", "model_requests": 0})
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(module.subprocess, "run", child)
    health(s["candidate"], manifest, stage, receipt)
    def forged(command, **kwargs):
        child(command, **kwargs)
        value = json.loads(receipt.read_text()); value["nonce"] = "0" * 64; atomic_write_json(receipt, value)
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(module.subprocess, "run", forged)
    (stage.parent / "health-authority.json").unlink()
    receipt.unlink()
    with pytest.raises(ReleaseError, match="回执不匹配"): health(s["candidate"], manifest, stage, receipt)


@pytest.mark.parametrize("identity", ["valid", "missing", "tampered"])
def test_real_launcher_health_initializes_only_explicit_disposable_data(tmp_path, identity):
    stage = tmp_path / "health-data"; stage.mkdir()
    receipt = tmp_path / "receipt.json"
    atomic_write_json(tmp_path / "health-authority.json", {"format": "scholarflow-health-authority", "version": 1,
        "nonce": "1" * 64, "stage": stage.name, "receipt": receipt.name})
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "DATA_DIR"}}
    env["PYTHONUTF8"] = "1"
    embedded = tmp_path / "embedded"; embedded.mkdir()
    definition = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    atomic_write_json(embedded / "release.json", definition)
    target_schema = create_target_schema(ROOT)
    build = {"format": "scholarflow-release-inputs", "schema_version": 1, "artifact_state": "prepared_inputs",
        **{key: definition[key] for key in ("product_id", "version", "channel", "build_number", "data_contract")},
        "definition_sha256": digest(definition), "source_sha256": "a" * 64, "schema_sha256": target_schema["sha256"],
        "files": [], "created_at": "2026-11-16T00:00:00Z", "coverage": {}, "release_blockers": []}
    build["build_id"] = digest(build)
    if identity != "missing":
        if identity == "tampered": build["source_sha256"] = "b" * 64
        atomic_write_json(embedded / "release-manifest.json", build)
    # Only substitute packaged resources, not health/runtime behavior. This is
    # a source-process probe, not a compiled EXE or final build certification.
    code = """import sys,runpy\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom app import release_info\nresources=Path(sys.argv[2]);release_info.resource_root=lambda:resources\npath=sys.argv[3];sys.argv=[path,*sys.argv[4:]]\ntry:runpy.run_path(path,run_name='__main__')\nfinally:\n assert 'webview' not in sys.modules\n assert 'app.llm' not in sys.modules\n"""
    result = subprocess.run([sys.executable, "-c", code, str(ROOT / "backend"), str(embedded), str(ROOT / "desktop/launcher.py"), "--upgrade-health-check",
        "--stage", str(stage), "--receipt", str(receipt), "--nonce", "1" * 64],
        cwd=tmp_path, env=env, capture_output=True, timeout=90)
    if identity != "valid":
        assert result.returncode != 0 and not receipt.exists()
        assert not (stage / "scholarflow.db").exists()
        return
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["nonce"] == "1" * 64 and value["model_requests"] == 0
    assert value["scope"] == "disposable_database_only"
    assert value["schema_sha256"] == target_schema["sha256"]
    assert value["input_build_id"] == build["build_id"]
    assert not (stage / ".runtime.lock").exists()
    assert not (stage / "config.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-mode filesystem boundary")
def test_real_windows_open_executable_prevents_replacement(setup):
    import ctypes
    from ctypes import wintypes
    s = setup
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(s["target"] / "payload/ScholarFlow.exe"), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    before = module._tree_fingerprint(s["target"])
    try:
        with pytest.raises(OSError): install(s)
    finally:
        kernel.CloseHandle(handle)
    assert module._tree_fingerprint(s["target"]) == before
    assert module.recover(s["target"], s["data"], trusted_keys=s["trust"])["state"] == "rolled_back"


def test_fresh_install_keeps_live_empty_data_uninitialized_until_normal_startup(setup, monkeypatch):
    s = setup
    s["target"] = s["target"].parent / "fresh-install"
    s["data"] = s["data"].parent / "fresh-data"; s["data"].mkdir()
    observed = []
    def health(program, manifest, stage, receipt):
        observed.append(stage)
        assert stage.is_dir() and not list(stage.iterdir())
    monkeypatch.setattr(module, "_health", health)
    result = install(s)
    assert result["state"] == "committed" and not result["backup_retained"]
    assert observed and not (s["data"] / "scholarflow.db").exists()
    assert {p.name for p in s["data"].iterdir()} == {".runtime.lock"}


def test_unrelated_barrier_refuses_recovery_before_any_program_move(setup, monkeypatch):
    s = setup
    def interrupt(name):
        if name == "candidate_activated": raise PowerLoss()
    monkeypatch.setattr(module, "_checkpoint", interrupt)
    with pytest.raises(PowerLoss): install(s)
    before = module._tree_fingerprint(s["target"])
    atomic_write_json(module.barrier_path(s["data"]), {"transaction_id": "unrelated"})
    with pytest.raises(ReleaseError, match="其它事务"):
        module.recover(s["target"], s["data"], trusted_keys=s["trust"])
    assert module._tree_fingerprint(s["target"]) == before


def test_external_file_change_during_health_rolls_back_program_and_keeps_external_edit(setup, monkeypatch):
    s = setup; before = module._tree_fingerprint(s["target"])
    monkeypatch.setattr(module, "_health", lambda *args: (s["data"] / "paper.txt").write_text("external edit", encoding="utf-8"))
    with pytest.raises(ReleaseError, match="数据在健康检查期间变化"): install(s)
    assert module._tree_fingerprint(s["target"]) == before
    assert (s["data"] / "paper.txt").read_text(encoding="utf-8") == "external edit"


def test_partial_candidate_copy_failure_keeps_old_install_and_data(setup, monkeypatch):
    s = setup; before = module._tree_fingerprint(s["target"])
    def fail(*args, **kwargs): raise OSError("synthetic no space during copy")
    monkeypatch.setattr(module.shutil, "copytree", fail)
    with pytest.raises(OSError, match="synthetic"): install(s)
    assert module._tree_fingerprint(s["target"]) == before
    assert not module.barrier_path(s["data"]).exists()


def test_candidate_description_is_unsigned_and_not_final_release(setup, monkeypatch):
    s = setup
    value = module.verify_candidate(s["candidate"], s["trust"])
    folder = s["candidate"].parent / "assembled"; folder.mkdir()
    shutil.copytree(s["candidate"] / "payload", folder / "payload")
    prepared = folder.parent / "prepared-inputs"; prepared.mkdir()
    atomic_write_json(prepared / "release.json", value["definition"])
    atomic_write_json(prepared / "schema.json", value["target_schema"])
    monkeypatch.setattr(module, "verify_inputs", lambda _: {"build_id": "a" * 64})
    result = module.describe_candidate(folder, prepared)
    assert result["signature"] == "unsigned" and not result["public_release_ready"]
    with pytest.raises(ReleaseError, match="发布者签名"): module.verify_candidate(folder, s["trust"])
    s["sign"](folder, json.loads((folder / module.MANIFEST).read_text(encoding="utf-8")))
    assert module.verify_candidate(folder, s["trust"])["candidate_id"] == result["candidate_id"]


def test_malformed_startup_barrier_blocks_runtime_and_releases_os_lock(setup):
    from app.runtime_owner import RuntimeOwner, RuntimeInUse
    s = setup
    path = module.barrier_path(s["data"]); path.write_bytes(b"not json")
    owner = RuntimeOwner(s["data"])
    with pytest.raises(RuntimeInUse, match="恢复"): owner._acquire("service")
    path.unlink()
    assert owner._acquire("service")
    owner.finish_shutdown(idle=True)


@pytest.mark.parametrize("change", ["schema_changed", "database_missing"])
def test_recovery_rechecks_data_compatibility_after_interrupted_process(setup, monkeypatch, change):
    s = setup
    def interrupt(name):
        if name == "candidate_activated": raise PowerLoss()
    monkeypatch.setattr(module, "_checkpoint", interrupt)
    with pytest.raises(PowerLoss): install(s)
    activated = module._tree_fingerprint(s["target"])
    database = s["data"] / "scholarflow.db"
    if change == "schema_changed":
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE future_schema(value TEXT)"); db.commit()
    else:
        database.rename(s["data"] / "external-move.db")
    with pytest.raises(ReleaseError, match="数据"):
        module.recover(s["target"], s["data"], trusted_keys=s["trust"])
    assert module._tree_fingerprint(s["target"]) == activated
    assert module.barrier_path(s["data"]).exists()
