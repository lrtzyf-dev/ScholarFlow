"""Independent synthetic offline-entry audit; never runs an executable or network."""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app import release_install as core
from app.atomic_files import atomic_write_json
from app.release_files import digest
from app.release_info import ReleaseError, canonical_json
from app.release_schema import schema_snapshot
from desktop import install_gui as gui, install_shortcuts as shortcuts, install_tool as adapter


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("independent offline audit forbids network")
    for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                         (socket, "create_connection"), (socket, "getaddrinfo")):
        monkeypatch.setattr(target, name, forbidden)


@pytest.fixture
def signed(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    trust = {"independent-offline": base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}
    trust_path = tmp_path / "external-trust.json"
    atomic_write_json(trust_path, trust)
    data = tmp_path / "data"
    data.mkdir()
    with closing(sqlite3.connect(data / "scholarflow.db")) as db:
        db.executescript("CREATE TABLE samples(value TEXT); INSERT INTO samples VALUES ('unchanged'); CREATE TABLE runtime_runs(status TEXT);")
    (data / "source.txt").write_text("synthetic source; must survive", encoding="utf-8")
    schema = schema_snapshot(data / "scholarflow.db")
    definition_input = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))

    def create(name, build, payload):
        path = tmp_path / name
        (path / "payload").mkdir(parents=True)
        (path / "payload/ScholarFlow.exe").write_bytes(payload)
        definition = json.loads(json.dumps(definition_input))
        definition["build_number"] = build
        manifest = {"format": core.FORMAT, "schema_version": 1, "definition": definition,
                    "input_build_id": "c" * 64, "target_schema": schema,
                    "entrypoint": "ScholarFlow.exe", "files": core._files(path / "payload"),
                    "qualification": "assembled_candidate_not_public_release"}
        manifest["candidate_id"] = digest(manifest)
        atomic_write_json(path / core.MANIFEST, manifest)
        atomic_write_json(path / "release-signature.json", {"algorithm": "Ed25519", "key_id": "independent-offline",
                          "signature": base64.b64encode(private.sign(canonical_json(manifest))).decode()})
        assert core.verify_candidate(path, trust)["candidate_id"] == manifest["candidate_id"]
        return path, manifest

    old, old_manifest = create("old-source", 1, b"synthetic old executable; never execute")
    candidate, preview = create("candidate", 2, b"synthetic preview A; never execute")
    alternative, replacement = create("alternative-B", 2, b"synthetic replacement B; never execute")
    target = tmp_path / "installed"
    shutil.copytree(old, target)
    health_calls = []

    def no_execution_health(program, expected, stage, receipt):
        actual = core.verify_candidate(program, trust)
        # These are the exact identity fields used by the native health protocol.
        # A and B deliberately share them but have different signed file bytes.
        assert actual["definition"] == expected["definition"]
        assert actual["input_build_id"] == expected["input_build_id"]
        assert actual["target_schema"] == expected["target_schema"]
        assert stage != data and stage.is_dir()
        assert schema_snapshot(stage / "scholarflow.db") == schema
        health_calls.append(actual["candidate_id"])

    monkeypatch.setattr(core, "_health", no_execution_health)
    return dict(data=data, target=target, candidate=candidate, alternative=alternative,
                preview=preview, replacement=replacement, old=old_manifest,
                trust=trust, trust_path=trust_path, health_calls=health_calls, create=create)


def request(s, **changes):
    values = dict(candidate=s["candidate"], target=s["target"], data=s["data"],
                  trusted_keys=s["trust_path"], closed_confirmed=True)
    values.update(changes)
    return gui.InstallRequest(**values)


def event(controller, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = controller.poll()
        if result is not None:
            return result
        time.sleep(0.005)
    raise AssertionError("worker did not produce a completion")


def observe(label, s, result=None, error=None):
    state = s["target"].parent / ("." + s["target"].name + "-updates")
    pending = state / "pending.json"
    journal = None
    if pending.exists():
        transaction = json.loads(pending.read_text(encoding="utf-8"))["transaction_id"]
        journal = json.loads((state / transaction / "journal.json").read_text(encoding="utf-8"))
    value = {"case": label, "preview_candidate_id": s["preview"]["candidate_id"],
             "replacement_candidate_id": s["replacement"]["candidate_id"],
             "actual_target_candidate_id": core.verify_candidate(s["target"], s["trust"])["candidate_id"],
             "old_candidate_id": s["old"]["candidate_id"], "result": result,
             "error": error, "journal": journal, "health_candidate_ids": s["health_calls"],
             "barrier_exists": core.barrier_path(s["data"]).exists()}
    destination = os.environ.get("SF_INDEPENDENT_CASE_OUTPUT")
    if destination:
        path = Path(destination) / (label + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return value


def test_gui_preview_binding_rejects_replacement_after_adapter_preflight(signed, monkeypatch):
    s = signed
    controller = gui.InstallController()
    req = request(s)
    controller.submit("verify", req)
    preview_event = event(controller)
    assert preview_event.ok and preview_event.value["candidate_id"] == s["preview"]["candidate_id"]
    original = adapter.verify_candidate
    switched = False

    def verify_then_replace(folder, trust):
        nonlocal switched
        verified = original(folder, trust)
        if Path(folder) == s["candidate"] and not switched:
            switched = True
            s["candidate"].rename(s["candidate"].with_name("preview-A-retained"))
            shutil.copytree(s["alternative"], s["candidate"])
        return verified

    monkeypatch.setattr(adapter, "verify_candidate", verify_then_replace)
    try:
        controller.submit("install", req)
        completion = event(controller)
        observed = observe("gui-preview-A-to-core-B", s,
                           result={"ok": completion.ok, "value": completion.value}, error=completion.error)
    finally:
        controller.close()
    assert switched
    assert not completion.ok, "verified A was replaced by valid B before core verification yet installation reported success"
    assert observed["actual_target_candidate_id"] == s["old"]["candidate_id"]
    assert not s["health_calls"]


def test_core_binding_rejects_replacement_before_stage_copy(signed, monkeypatch):
    s = signed
    copytree = core.shutil.copytree
    switched = False

    def switch_before_copy(source, destination, *args, **kwargs):
        nonlocal switched
        if Path(source) == s["candidate"] and Path(destination).name == "stage" and not switched:
            switched = True
            s["candidate"].rename(s["candidate"].with_name("preview-A-retained"))
            copytree(s["alternative"], s["candidate"])
        return copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(core.shutil, "copytree", switch_before_copy)
    result, error = None, None
    try:
        result = adapter.run_offline_action("install", **request(s).action_options(),
                                            expected_candidate_id=s["preview"]["candidate_id"])
    except (ValueError, OSError, sqlite3.Error) as exc:
        error = str(exc)
    observed = observe("core-preview-A-to-stage-B", s, result=result, error=error)
    assert switched
    assert error is not None, "core verified A, staged valid B and committed despite the preview identity binding"
    assert observed["actual_target_candidate_id"] == s["old"]["candidate_id"]
    assert not s["health_calls"]


@pytest.mark.parametrize("boundary", ["ready", "candidate_activated"])
def test_changed_signed_stage_or_activation_never_enters_health(signed, monkeypatch, boundary):
    s = signed
    state = s["target"].parent / ("." + s["target"].name + "-updates")

    def replace_at_checkpoint(name):
        if name != boundary:
            return
        pointer = json.loads((state / "pending.json").read_text(encoding="utf-8"))
        folder = state / pointer["transaction_id"]
        destination = folder / "stage" if name == "ready" else s["target"]
        destination.rename(folder / "original-A-retained-by-test")
        shutil.copytree(s["alternative"], destination)

    monkeypatch.setattr(core, "_checkpoint", replace_at_checkpoint)
    with pytest.raises(ReleaseError):
        adapter.run_offline_action("install", **request(s).action_options(),
                                   expected_candidate_id=s["preview"]["candidate_id"])
    observed = observe("signed-substitution-" + boundary, s)
    assert not s["health_calls"]
    assert observed["journal"]["state"] != "committed"
    # The external replacement is unknown to recovery: preserve it and the old
    # signed slot rather than delete files that do not match the journal.
    folder = state / observed["journal"]["transaction_id"]
    assert core.verify_candidate(folder / "previous", s["trust"])["candidate_id"] == s["old"]["candidate_id"]
    assert observed["barrier_exists"]
    with pytest.raises(ReleaseError):
        adapter.run_offline_action("recover", **request(s).action_options())
    assert core.verify_candidate(s["target"], s["trust"])["candidate_id"] == s["replacement"]["candidate_id"]


def test_signed_substitution_at_before_commit_cannot_produce_wrong_committed_receipt(signed, monkeypatch):
    s = signed
    state = s["target"].parent / ("." + s["target"].name + "-updates")

    def replace_at_commit(name):
        if name != "before_commit":
            return
        pointer = json.loads((state / "pending.json").read_text(encoding="utf-8"))
        folder = state / pointer["transaction_id"]
        s["target"].rename(folder / "health-checked-A-retained-by-test")
        shutil.copytree(s["alternative"], s["target"])

    monkeypatch.setattr(core, "_checkpoint", replace_at_commit)
    result, error = None, None
    try:
        result = adapter.run_offline_action("install", **request(s).action_options(),
                                            expected_candidate_id=s["preview"]["candidate_id"])
    except (ValueError, OSError, sqlite3.Error) as exc:
        error = str(exc)
    observed = observe("signed-substitution-before-commit", s, result=result, error=error)
    assert s["health_calls"] == [s["preview"]["candidate_id"]]
    assert error is not None, "health checked A, but commit boundary replacement B was recorded as committed A"
    assert observed["journal"]["state"] != "committed"
    assert observed["barrier_exists"]


@pytest.mark.parametrize("lock_name", ["runtime", "installer"])
def test_real_os_lock_blocks_gui_install_without_replacement(signed, lock_name):
    s = signed
    controller = gui.InstallController()
    req = request(s)
    controller.submit("verify", req)
    assert event(controller).ok
    state = s["target"].parent / ("." + s["target"].name + "-updates")
    state.mkdir(exist_ok=True)
    lock = s["data"] / ".runtime.lock" if lock_name == "runtime" else state / "install.lock"
    try:
        with core._exclusive(lock):
            controller.submit("install", req)
            completion = event(controller)
        assert not completion.ok and "占用" in completion.error
        assert core.verify_candidate(s["target"], s["trust"])["candidate_id"] == s["old"]["candidate_id"]
        assert not s["health_calls"]
        with pytest.raises(ValueError, match="校验"):
            controller.submit("install", req)
    finally:
        controller.close()


@pytest.mark.parametrize("status", ["running", "queued"])
def test_real_active_task_refuses_adapter_transaction(signed, status):
    s = signed
    with closing(sqlite3.connect(s["data"] / "scholarflow.db")) as db:
        db.execute("INSERT INTO runtime_runs VALUES (?)", (status,))
        db.commit()
    with pytest.raises(ReleaseError, match="活动或排队"):
        adapter.run_offline_action("install", **request(s).action_options(),
                                   expected_candidate_id=s["preview"]["candidate_id"])
    assert core.verify_candidate(s["target"], s["trust"])["candidate_id"] == s["old"]["candidate_id"]
    assert not s["health_calls"]


class SimulatedPowerLoss(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["ready", "previous_moved", "candidate_activated", "before_commit", "committed"])
def test_gui_recovery_reads_durable_committed_or_rolled_back_state(signed, monkeypatch, boundary):
    s = signed
    before = {name: (s["data"] / name).read_bytes() for name in ["scholarflow.db", "source.txt"]}

    def crash(name):
        if name == boundary:
            raise SimulatedPowerLoss()

    monkeypatch.setattr(core, "_checkpoint", crash)
    with pytest.raises(SimulatedPowerLoss):
        adapter.run_offline_action("install", **request(s).action_options(),
                                   expected_candidate_id=s["preview"]["candidate_id"])
    controller = gui.InstallController()
    req = request(s, candidate=None)
    try:
        controller.submit("recover", req)
        completion = event(controller)
        assert completion.ok
        expected = "committed" if boundary == "committed" else "rolled_back"
        assert completion.value["state"] == expected
        controller.submit("recover", req)
        repeated = event(controller)
        assert repeated.ok and repeated.value["state"] == expected
    finally:
        controller.close()
    expected_id = s["preview"]["candidate_id"] if boundary == "committed" else s["old"]["candidate_id"]
    assert core.verify_candidate(s["target"], s["trust"])["candidate_id"] == expected_id
    assert not core.barrier_path(s["data"]).exists()
    for name, content in before.items():
        assert (s["data"] / name).read_bytes() == content


def test_postcommit_exception_is_unknown_then_recovery_reports_committed_without_retry(signed, monkeypatch):
    s = signed
    calls = []

    def after_commit_failure(name):
        calls.append(name)
        if name == "committed":
            raise RuntimeError("synthetic confidential worker internals")

    monkeypatch.setattr(core, "_checkpoint", after_commit_failure)
    controller = gui.InstallController()
    req = request(s)
    try:
        controller.submit("verify", req)
        assert event(controller).ok
        controller.submit("install", req)
        completion = event(controller)
        assert not completion.ok and "未能确认" in completion.error and "恢复" in completion.error
        assert "confidential" not in completion.error
        assert core.verify_candidate(s["target"], s["trust"])["candidate_id"] == s["preview"]["candidate_id"]
        with pytest.raises(ValueError, match="校验"):
            controller.submit("install", req)
        controller.submit("recover", req)
        recovered = event(controller)
        assert recovered.ok and recovered.value["state"] == "committed"
        assert calls.count("committed") == 1
        assert len(s["health_calls"]) == 1
    finally:
        controller.close()


def simple_request(tmp_path):
    return gui.InstallRequest(tmp_path / "candidate", tmp_path / "target", tmp_path / "data",
                              tmp_path / "trust.json", closed_confirmed=True)


def verified_result():
    return dict(candidate_id="a" * 64, integrity="verified", signature="trusted")


def test_controller_busy_close_single_worker_and_main_thread_poll(tmp_path):
    started, release = threading.Event(), threading.Event()
    main = threading.get_ident()
    worker_ids, calls = [], []

    def runner(action, **options):
        worker_ids.append(threading.get_ident())
        calls.append((action, options))
        if action == "verify":
            started.set()
            assert release.wait(5)
            return verified_result()
        return dict(state="committed", candidate_id=options["expected_candidate_id"])

    controller = gui.InstallController(action_runner=runner)
    req = simple_request(tmp_path)
    try:
        controller.submit("verify", req)
        assert started.wait(3)
        assert not controller.can_close and controller.poll() is None
        with pytest.raises(ValueError, match="正在运行"):
            controller.submit("recover", req)
        with pytest.raises(ValueError, match="正在运行"):
            controller.close()
        assert len(calls) == 1
        release.set()
        assert event(controller).ok
        assert controller.can_close and threading.get_ident() == main
        controller.submit("install", req)
        assert event(controller).ok
        assert len(worker_ids) == 2 and len(set(worker_ids)) == 1 and worker_ids[0] != main
        assert calls[-1][1]["expected_candidate_id"] == "a" * 64
    finally:
        release.set()
        controller.close()


def test_inflight_invalidation_prevents_stale_verification_restore(tmp_path):
    started, release = threading.Event(), threading.Event()

    def runner(action, **kwargs):
        started.set()
        assert release.wait(5)
        return verified_result()

    controller = gui.InstallController(action_runner=runner)
    req = simple_request(tmp_path)
    try:
        controller.submit("verify", req)
        assert started.wait(3)
        controller.invalidate()
        release.set()
        assert event(controller).ok
        with pytest.raises(ValueError, match="校验"):
            controller.submit("install", req)
    finally:
        release.set()
        controller.close()


@pytest.mark.parametrize("field", ["candidate", "target", "data", "trusted_keys", "direction", "shortcut_directory"])
def test_changed_request_requires_new_verification(tmp_path, field):
    controller = gui.InstallController(action_runner=lambda *args, **kwargs: verified_result())
    req = simple_request(tmp_path)
    try:
        controller.submit("verify", req)
        assert event(controller).ok
        altered = replace(req, **{field: "rollback" if field == "direction" else tmp_path / ("changed-" + field)})
        with pytest.raises(ValueError, match="校验"):
            controller.submit("install", altered)
        assert controller.can_close
    finally:
        controller.close()


def test_optional_shortcut_failure_keeps_committed_success_and_consumes_receipt(tmp_path):
    calls = []

    def runner(action, **options):
        calls.append(action)
        return verified_result() if action == "verify" else dict(state="committed", candidate_id="a" * 64)

    def fail_shortcut(*args):
        raise PermissionError("synthetic shortcut denial")

    controller = gui.InstallController(action_runner=runner, shortcut_writer=fail_shortcut)
    req = replace(simple_request(tmp_path), shortcut_directory=tmp_path / "chosen")
    try:
        controller.submit("verify", req)
        assert event(controller).ok
        controller.submit("install", req)
        result = event(controller)
        assert result.ok and result.value["state"] == "committed"
        assert result.value["shortcut"]["state"] == "failed"
        with pytest.raises(ValueError, match="校验"):
            controller.submit("install", req)
        assert calls == ["verify", "install"]
    finally:
        controller.close()


@pytest.mark.parametrize("action", ["install", "recover"])
def test_close_confirmation_required_before_destructive_actions(tmp_path, action):
    calls = []
    controller = gui.InstallController(action_runner=lambda *args, **kwargs: calls.append(args))
    try:
        with pytest.raises(ValueError, match="关闭确认"):
            controller.submit(action, replace(simple_request(tmp_path), closed_confirmed=False))
        assert calls == [] and controller.can_close
    finally:
        controller.close()


def old_receipt(s):
    return dict(state="committed", candidate_id=s["old"]["candidate_id"])


def test_shortcut_can_publish_fixed_sibling_name_in_parent_directory(signed):
    s = signed
    calls = []
    directory = s["target"].parent

    def writer(link, executable, working):
        calls.append((link, executable, working))
        assert link.parent == directory and link.name.startswith(".scholarflow-shortcut-")
        assert executable == s["target"] / "payload/ScholarFlow.exe"
        assert working == executable.parent
        link.write_bytes(b"synthetic shortcut; native format not exercised")

    result = shortcuts.create_shortcut(s["target"], directory, s["trust"], old_receipt(s),
                                       forbidden_roots=[s["candidate"], s["data"]], native_writer=writer)
    assert result["state"] == "created" and Path(result["path"]) == directory / "ScholarFlow.lnk"
    assert len(calls) == 1 and not list(directory.glob(".scholarflow-shortcut-*"))
    assert core.verify_candidate(s["target"], s["trust"])["candidate_id"] == s["old"]["candidate_id"]


@pytest.mark.parametrize("race", [False, True])
def test_shortcut_existing_or_raced_destination_is_never_overwritten(signed, race):
    s = signed
    directory = s["data"].parent / "chosen shortcuts ` $() &"
    directory.mkdir()
    destination = directory / "ScholarFlow.lnk"
    sentinel = b"existing or race-winner shortcut; preserve exactly"
    if not race:
        destination.write_bytes(sentinel)
    calls = []

    def writer(link, *args):
        calls.append(link)
        destination.write_bytes(sentinel)
        link.write_bytes(b"temporary new synthetic shortcut")

    with pytest.raises((ValueError, FileExistsError)):
        shortcuts.create_shortcut(s["target"], directory, s["trust"], old_receipt(s), native_writer=writer)
    assert destination.read_bytes() == sentinel
    assert len(calls) == (1 if race else 0)
    assert sorted(p.name for p in directory.iterdir()) == ["ScholarFlow.lnk"]


@pytest.mark.parametrize("protected", ["target", "candidate", "data", "transaction", "recovery"])
def test_shortcut_refuses_protected_subdirectories_before_native_writer(signed, protected):
    s = signed
    from app.data_backup import recovery_root
    transaction = s["target"].parent / ("." + s["target"].name + "-updates")
    recovery = recovery_root(s["data"])
    transaction.mkdir(exist_ok=True)
    recovery.mkdir(exist_ok=True)
    directory = {"target": s["target"], "candidate": s["candidate"], "data": s["data"],
                 "transaction": transaction, "recovery": recovery}[protected]
    called = []
    with pytest.raises(ValueError, match="独立"):
        shortcuts.create_shortcut(s["target"], directory, s["trust"], old_receipt(s),
                                   forbidden_roots=[s["candidate"], s["data"], transaction, recovery],
                                   native_writer=lambda *args: called.append(args))
    assert called == [] and not (directory / "ScholarFlow.lnk").exists()


def test_custom_data_shortcut_is_refused_before_trust_or_write(signed, monkeypatch):
    s = signed
    called = []
    monkeypatch.setattr(gui, "load_action_trust", lambda *args, **kwargs: called.append("trust"))
    monkeypatch.setattr(gui, "create_shortcut", lambda *args, **kwargs: called.append("write"))
    with pytest.raises(ValueError, match="自定义数据"):
        gui._shortcut(request(s, shortcut_directory=s["target"].parent), old_receipt(s))
    assert called == []


def test_shortcut_receipt_must_match_signed_installed_identity(signed):
    s = signed
    directory = s["data"].parent / "shortcuts"
    directory.mkdir()
    called = []
    with pytest.raises(ValueError, match="身份"):
        shortcuts.create_shortcut(s["target"], directory, s["trust"],
                                   dict(state="committed", candidate_id=s["preview"]["candidate_id"]),
                                   native_writer=lambda *args: called.append(args))
    assert called == [] and list(directory.iterdir()) == []


def test_native_shortcut_writer_uses_fixed_script_and_environment_paths(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(shortcuts.subprocess, "run", lambda command, **kwargs: captured.append((command, kwargs)))
    link = tmp_path / "name ` $() & ' .lnk"
    executable = tmp_path / "payload ` $() & '" / "ScholarFlow.exe"
    shortcuts._native_writer(link, executable, executable.parent)
    assert len(captured) == 1
    command, options = captured[0]
    assert command[-1] == shortcuts._SCRIPT
    assert str(link) not in command[-1] and str(executable) not in command[-1]
    assert options.get("shell", False) is False
    assert options["env"]["SF_SHORTCUT_FILE"] == str(link)
    assert options["env"]["SF_SHORTCUT_TARGET"] == str(executable)
    assert options["env"]["SF_SHORTCUT_WORKDIR"] == str(executable.parent)
    assert options["check"] and options["timeout"] == 30
