"""Independent isolated import fault probes; no EXE or external HTTP calls."""
import os
from contextlib import closing, ExitStack
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from app import legacy_import as module
from app.atomic_files import atomic_write_json
from app.release_files import digest
from app.release_info import ReleaseError, read_json
from tests.test_legacy_import import (prepared_import, legacy, setup, inputs, review, run,
    recover, prepare, files)  # noqa: F401 -- real signed synthetic fixtures


def test_transaction_lock_cannot_modify_an_original_asset_through_a_hardlink(prepared_import):
    s = prepared_import
    retained = s["data"] / "independent-empty-retained.txt"
    retained.write_bytes(b"")
    s["destination"] = s["data"].parent / "hardlink-reviewed-preparation"
    s["prepared"] = prepare(s)
    s["options"]["expected_preparation_sha256"] = s["prepared"]["preparation_sha256"]
    plan = review(s)
    state = s["output"].parent / ("." + s["output"].name + "-imports")
    state.mkdir()
    try:
        os.link(retained, state / "import.lock")
    except OSError as exc:
        pytest.skip("physical hardlinks unavailable: " + type(exc).__name__)
    assert os.stat(retained).st_nlink >= 2
    before = files(s["data"])
    # Windows may reject the later read of the now byte-locked original file;
    # either refusal must still leave the retained source bytes untouched.
    with pytest.raises((ReleaseError, PermissionError)):
        module.import_data(*inputs(s), **s["options"], expected_review_sha256=plan["review_sha256"])
    assert retained.read_bytes() == b"", "import lock wrote through to an original retained asset"
    assert files(s["data"]) == before
    assert not s["output"].exists() and not s["new_target"].exists()


def state_paths(s):
    state = s["output"].parent / ("." + s["output"].name + "-imports")
    pointer = read_json(state / "pending.json")
    return state, state / pointer["transaction_id"]


@pytest.mark.parametrize("location", ["preparation", "state", "program_updates"])
def test_cli_never_bootstraps_external_trust_from_import_owned_material(prepared_import, capsys, location):
    s = prepared_import
    folders = {"preparation": s["destination"],
        "state": s["output"].parent / ("." + s["output"].name + "-imports"),
        "program_updates": s["new_target"].parent / ("." + s["new_target"].name + "-updates")}
    folder = folders[location]
    folder.mkdir(exist_ok=True)
    trust = folder / "self-supplied-trust.json"
    atomic_write_json(trust, s["trust"])
    before_old, before_data = files(s["legacy"]), files(s["data"])
    script = Path(__file__).resolve().parents[2] / "desktop/install_tool.py"
    spec = importlib.util.spec_from_file_location("independent_legacy_import_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    common = ["--preparation", str(s["destination"]), "--legacy", str(s["legacy"]),
        "--data", str(s["data"]), "--target", str(s["new_target"]), "--output", str(s["output"]),
        "--trusted-keys", str(trust), "--migration-id", s["options"]["migration_id"],
        "--preparation-sha256", s["options"]["expected_preparation_sha256"]]
    assert cli.main(["review-legacy-import", *common]) == 2
    captured = capsys.readouterr()
    assert "信任库" in captured.err and not captured.out
    assert files(s["legacy"]) == before_old and files(s["data"]) == before_data
    assert not s["output"].exists() and not s["new_target"].exists()


@pytest.mark.parametrize("forgery", ["pointer_traversal", "self_hashed_review_path"])
def test_recovery_journal_cannot_borrow_an_unrelated_directory_identity(prepared_import, forgery):
    s = prepared_import
    plan = review(s)
    assert run(s)["state"] == "committed"
    state, folder = state_paths(s)
    unrelated = s["data"].parent / "independent-retain-unrelated"
    unrelated.mkdir()
    (unrelated / "sentinel.txt").write_text("retain unrelated bytes", encoding="utf-8")
    if forgery == "pointer_traversal":
        atomic_write_json(state / "pending.json", {"transaction_id": "../" + unrelated.name})
    else:
        path = folder / "journal.json"
        journal = read_json(path)
        journal["review"]["new_data"] = str(unrelated)
        journal["review"]["review_sha256"] = digest({k: v for k, v in journal["review"].items() if k != "review_sha256"})
        atomic_write_json(path, journal)
    before = {"old": files(s["legacy"]), "data": files(s["data"]),
        "output": files(s["output"]), "unrelated": files(unrelated)}
    with pytest.raises(ReleaseError):
        recover(s, plan)
    assert files(s["legacy"]) == before["old"] and files(s["data"]) == before["data"]
    assert files(s["output"]) == before["output"] and files(unrelated) == before["unrelated"]


def test_reenabled_checkpoint_after_staging_never_publishes_and_recovery_rebuilds_a_dormant_copy(prepared_import, monkeypatch):
    s = prepared_import
    plan = review(s)
    before_old, before_data = files(s["legacy"]), files(s["data"])
    altered = []
    def mutate(name):
        if name == "staged":
            _, folder = state_paths(s)
            database = folder / "stage/scholarflow.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE run_attempts SET status='interrupted',checkpoint='{}',writer_token='forged-active-writer'")
                connection.commit()
            altered.append(database)
    monkeypatch.setattr(module, "_checkpoint", mutate)
    with pytest.raises(ReleaseError):
        module.import_data(*inputs(s), **s["options"], expected_review_sha256=plan["review_sha256"])
    assert altered and altered[0].is_file() and not s["output"].exists()
    assert files(s["legacy"]) == before_old and files(s["data"]) == before_data
    monkeypatch.setattr(module, "_checkpoint", lambda name: None)
    assert recover(s, plan)["state"] == "committed"
    assert altered[0].is_file(), "recovery must retain the tampered transaction stage for review"
    with closing(sqlite3.connect(s["output"] / "scholarflow.db")) as connection:
        assert connection.execute("SELECT status,checkpoint,writer_token FROM run_attempts").fetchone() == (
            "cancelled", None, "legacy-import-inert")
    assert files(s["legacy"]) == before_old and files(s["data"]) == before_data


def test_original_runtime_restart_at_ready_boundary_prevents_publish_then_explicit_recovery_succeeds(prepared_import, monkeypatch):
    s = prepared_import
    # The simulated external restart below owns creating its runtime lock.
    # Seed that inert file before freezing our preservation baseline.
    (s["data"] / ".runtime.lock").touch(exist_ok=True)
    plan = review(s)
    before_old, before_data = files(s["legacy"]), files(s["data"])
    with ExitStack() as held:
        def restart(name):
            if name == "ready":
                held.enter_context(module._exclusive(s["data"] / ".runtime.lock"))
        monkeypatch.setattr(module, "_checkpoint", restart)
        with pytest.raises(ReleaseError, match="运行实例"):
            module.import_data(*inputs(s), **s["options"], expected_review_sha256=plan["review_sha256"])
        assert not s["output"].exists()
    assert files(s["legacy"]) == before_old and files(s["data"]) == before_data
    monkeypatch.setattr(module, "_checkpoint", lambda name: None)
    assert recover(s, plan)["state"] == "committed"
    assert files(s["legacy"]) == before_old and files(s["data"]) == before_data


def test_dotdot_output_alias_cannot_hide_the_actual_import_state_as_an_external_trust_source(prepared_import, capsys):
    s = prepared_import
    plan = review(s)
    assert run(s)["state"] == "committed"
    state, _ = state_paths(s)
    trust = state / "state-supplied-publisher-trust.json"
    atomic_write_json(trust, s["trust"])
    alias = s["output"] / "nonexistent-alias-component" / ".."
    assert alias.resolve() == s["output"].resolve() and alias.name == ".."
    script = Path(__file__).resolve().parents[2] / "desktop/install_tool.py"
    spec = importlib.util.spec_from_file_location("independent_import_alias_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    args = ["recover-legacy-import", "--preparation", str(s["destination"]),
        "--legacy", str(s["legacy"]), "--data", str(s["data"]), "--target", str(s["new_target"]),
        "--output", str(alias), "--trusted-keys", str(trust),
        "--migration-id", s["options"]["migration_id"],
        "--preparation-sha256", s["options"]["expected_preparation_sha256"],
        "--review-sha256", plan["review_sha256"]]
    assert cli.main(args) == 2, "normalized import-state trust must be rejected before signature verification"
    captured = capsys.readouterr()
    assert "信任库" in captured.err and not captured.out
