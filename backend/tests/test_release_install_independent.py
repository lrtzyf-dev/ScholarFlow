"""Independent signed-update failures in disposable folders, never user installs."""
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from app import release_install as updater
from app.atomic_files import atomic_write_json
from app.release_files import digest
from app.release_info import ReleaseError, canonical_json
from app.release_schema import schema_snapshot

ROOT = Path(__file__).resolve().parents[2]


def kill_probe(proc):
    # Windows venv python.exe can be a redirector. Terminate this owned helper
    # tree so the actual interpreter cannot keep the installer lock alive.
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, creationflags=0x08000000)
    else: proc.kill()
    proc.wait(timeout=10)


class Interrupted(BaseException):
    pass


@pytest.fixture
def isolated_update(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    private = Ed25519PrivateKey.generate()
    trust = {"independent-publisher": base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}
    trust_path = tmp_path / "external-trust.json"; atomic_write_json(trust_path, trust)
    data = tmp_path / "original-data"; data.mkdir()
    with closing(sqlite3.connect(data / "scholarflow.db")) as db:
        db.executescript("CREATE TABLE independent_rows(value TEXT); INSERT INTO independent_rows VALUES('original-row'); CREATE TABLE runtime_runs(status TEXT);")
    (data / "original-paper.md").write_text("independent fixed source", encoding="utf-8")
    schema = schema_snapshot(data / "scholarflow.db")
    def seal(folder, build):
        value = {"format": updater.FORMAT, "schema_version": 1,
            "definition": {**json.loads((ROOT / "release.json").read_text(encoding="utf-8")), "build_number": build},
            "input_build_id": "c" * 64, "target_schema": schema, "entrypoint": "ScholarFlow.exe",
            "files": updater._files(folder / "payload"), "qualification": "assembled_candidate_not_public_release"}
        value["candidate_id"] = digest(value)
        atomic_write_json(folder / updater.MANIFEST, value)
        atomic_write_json(folder / "release-signature.json", {"algorithm": "Ed25519", "key_id": "independent-publisher",
            "signature": base64.b64encode(private.sign(canonical_json(value))).decode()})
    def candidate(name, build):
        folder = tmp_path / name; (folder / "payload").mkdir(parents=True)
        (folder / "payload/ScholarFlow.exe").write_bytes(b"independent synthetic exe " + str(build).encode())
        seal(folder, build)
        return folder
    old = candidate("old-candidate", 1)
    target = tmp_path / "program"; shutil.copytree(old, target)
    new = candidate("next-candidate", 2)
    monkeypatch.setattr(updater, "_health", lambda *a: None)
    return {"data": data, "target": target, "candidate": new, "trust": trust, "trust_path": trust_path,
        "seal": seal, "create": candidate, "old_fingerprint": updater._tree_fingerprint(target)}


def install(s, **kw):
    return updater.install(s["candidate"], s["target"], s["data"], trusted_keys=s["trust"], **kw)


def interrupt_at(monkeypatch, point):
    def interrupt(name):
        if name == point: raise Interrupted()
    monkeypatch.setattr(updater, "_checkpoint", interrupt)


@pytest.mark.parametrize("phase", ["active_rollback", "terminal_rollback"])
def test_recovery_revalidates_publisher_instead_of_trusting_edited_journal_fingerprint(isolated_update, monkeypatch, phase):
    s = isolated_update
    interrupt_at(monkeypatch, "candidate_activated")
    with pytest.raises(Interrupted): install(s)
    state = s["target"].parent / ".program-updates"
    folder = updater._pending(state)
    if phase == "terminal_rollback":
        updater.recover(s["target"], s["data"], trusted_keys=s["trust"])
        old = s["target"]
    else: old = folder / "previous"
    (old / "payload/ScholarFlow.exe").write_bytes(b"unsigned replacement, deliberately not resealed")
    journal = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
    journal["previous_fingerprint"] = updater._tree_fingerprint(old)
    atomic_write_json(folder / "journal.json", journal)
    with pytest.raises(ReleaseError): updater.recover(s["target"], s["data"], trusted_keys=s["trust"])


def test_cli_refuses_trust_store_supplied_inside_signed_candidate_payload(isolated_update):
    s = isolated_update
    inside = s["candidate"] / "payload/candidate-trust.json"
    atomic_write_json(inside, s["trust"])
    s["seal"](s["candidate"], 2)
    # The payload and signature are internally consistent; only external trust
    # provenance distinguishes this from trusting the candidate's own key.
    result = subprocess.run([sys.executable, str(ROOT / "desktop/install_tool.py"), "verify", str(s["candidate"]),
        "--trusted-keys", str(inside)], cwd=ROOT / "backend", capture_output=True, timeout=30)
    assert result.returncode == 2, result.stdout.decode(errors="replace")


def test_other_data_directory_cannot_orphan_committed_transaction_startup_barrier(isolated_update, monkeypatch):
    s = isolated_update
    interrupt_at(monkeypatch, "committed")
    with pytest.raises(Interrupted): install(s)
    state = s["target"].parent / ".program-updates"
    pending = updater._pending(state)
    assert updater.barrier_path(s["data"]).exists()
    another = s["data"].parent / "another-data"; shutil.copytree(s["data"], another)
    next_candidate = s["create"]("third-candidate", 3)
    monkeypatch.setattr(updater, "_checkpoint", lambda _name: None)
    with pytest.raises(ReleaseError): updater.install(next_candidate, s["target"], another, trusted_keys=s["trust"])
    assert updater._pending(state) == pending
    assert updater.recover(s["target"], s["data"], trusted_keys=s["trust"])["state"] == "committed"


@pytest.mark.parametrize("point", ["previous_moved", "candidate_activated", "before_commit"])
def test_real_process_kill_requires_recovery_and_preserves_old_program_and_data(isolated_update, tmp_path, point):
    s = isolated_update
    marker = tmp_path / "paused-marker"
    script = tmp_path / "killed-updater.py"
    script.write_text("""import json,sys,time\nfrom pathlib import Path\nfrom app import release_install as u\npoint,marker,candidate,target,data,trust=sys.argv[1:]\ndef pause(name):\n if name==point:\n  Path(marker).write_text('paused')\n  time.sleep(600)\nu._checkpoint=pause\nu._health=lambda *args:None\nu.install(candidate,target,data,trusted_keys=json.loads(Path(trust).read_text()))\n""", encoding="utf-8")
    env = {key:value for key,value in os.environ.items() if not any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PYTHONPATH", "DATA_DIR"))}
    env.update(DATA_DIR=str(tmp_path / "process-config"), PYTHONPATH=str(ROOT / "backend"), PYTHONUTF8="1")
    before_data = (s["data"] / "scholarflow.db").read_bytes()
    proc = subprocess.Popen([sys.executable, str(script), point, str(marker), str(s["candidate"]), str(s["target"]),
        str(s["data"]), str(s["trust_path"])], cwd=ROOT / "backend", env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline: time.sleep(.03)
        assert marker.exists(), proc.communicate(timeout=2)
        kill_probe(proc)
    finally:
        if proc.poll() is None: kill_probe(proc)
    assert updater.barrier_path(s["data"]).exists()
    from app.runtime_owner import RuntimeOwner, RuntimeInUse
    owner = RuntimeOwner(s["data"])
    try: acquired = owner._acquire("service")
    except RuntimeInUse: acquired = False
    if acquired: owner.finish_shutdown(idle=True)
    assert not acquired
    result = updater.recover(s["target"], s["data"], trusted_keys=s["trust"])
    assert result["state"] == "rolled_back"
    assert updater._tree_fingerprint(s["target"]) == s["old_fingerprint"]
    assert (s["data"] / "scholarflow.db").read_bytes() == before_data
    assert (s["data"] / "original-paper.md").read_text(encoding="utf-8") == "independent fixed source"
    assert owner._acquire("service")
    owner.finish_shutdown(idle=True)
