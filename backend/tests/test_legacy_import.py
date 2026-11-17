"""Actual isolated data, signed synthetic bytes; no executable or HTTP dispatch."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from app import legacy_import as module, legacy_migrations as registry, data_backup
from app.atomic_files import atomic_write_json
from app.release_files import digest
from app.release_info import ReleaseError, read_json
from app.release_schema import schema_snapshot
from .test_legacy_install import legacy, setup, observe, prepare, files
from .test_release_install import PowerLoss


@pytest.fixture
def prepared_import(legacy, db, monkeypatch):
    from app.models import Conversation, ForegroundRun, TaskRecord, QARecord, ConversationOptions, Subscription
    from app.runtime_models import RuntimeRun, RunAttempt
    from app.resource_models import PersistentSubtask
    from app.service_tool_models import ServiceToolCredential
    db.add(Conversation(id="import-conversation", title="Retain old research"))
    db.add(ConversationOptions(conversation_id="import-conversation", approval_mode="full_access"))
    db.add(RuntimeRun(id="history-run", kind="foreground", conversation_id="import-conversation",
                      status="paused", current_attempt_id="a" * 32, outcome="incomplete"))
    db.add(RunAttempt(id="a" * 32, run_id="history-run", number=1, writer_token="old-writer",
                      status="interrupted", checkpoint={"phase": "paused", "old_protocol": "never resume"}))
    db.add(ForegroundRun(id="history-run", conversation_id="import-conversation", status="paused",
        question="Preserve question", partial_answer="Preserve partial output", agent_checkpoint={"phase": "paused"}))
    db.add(TaskRecord(id="old-background", type="agent", conversation_id="import-conversation",
        status="paused", title="Retain old task", checkpoint={"phase": "paused"}, result={"answer": "historical result"}))
    db.add(QARecord(id="q" * 32, conversation_id="import-conversation", question="question", answer="fixed answer",
                    provider_context={"old-provider-context": "not reusable"}))
    db.add(Subscription(id="s" * 32, topic="synthetic research", active=1))
    db.add(PersistentSubtask(id="old-child", parent_run_id="history-run", conversation_id="import-conversation",
        fingerprint="b" * 64, specification={"question": "history"}, state="pending", checkpoint={"old": "checkpoint"}))
    db.add(ServiceToolCredential(id="c" * 32, provider="deepseek", protected_value="synthetic-protected-never-export"))
    db.commit()
    source_database = Path(db.get_bind().url.database)
    s = legacy
    data_backup._database_snapshot(source_database, s["data"] / "scholarflow.db")
    schema = schema_snapshot(s["data"] / "scholarflow.db")
    assert schema["sha256"] == registry.CURRENT_SCHEMA
    manifest = read_json(s["candidate"] / "install-manifest.json")
    manifest["target_schema"] = schema
    manifest["candidate_id"] = digest({key: value for key, value in manifest.items() if key != "candidate_id"})
    atomic_write_json(s["candidate"] / "install-manifest.json", manifest)
    s["sign"](s["candidate"], manifest)
    (s["data"] / "hooks.json").write_text('{"post_tool_use":[{"command":"must never run"}]}', encoding="utf-8")
    s["prepared"] = prepare(s)
    s["output"] = s["data"].parent / "new-independent-data"
    s["options"] = {"trusted_keys": s["trust"], "migration_id": registry.MIGRATION_ID,
        "expected_preparation_sha256": s["prepared"]["preparation_sha256"]}
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("candidate or command executed"))
    monkeypatch.setattr("httpx.Client.send", lambda *a, **k: pytest.fail("HTTP dispatched"))
    return s


def inputs(s):
    return s["destination"], s["legacy"], s["data"], s["new_target"], s["output"]


def review(s):
    return module.review_import(*inputs(s), **s["options"])


def run(s, **extra):
    plan = review(s)
    return module.import_data(*inputs(s), **s["options"], expected_review_sha256=plan["review_sha256"], **extra)


def recover(s, plan):
    return module.recover_import(*inputs(s), **s["options"], expected_review_sha256=plan["review_sha256"])


def test_review_is_read_only_and_success_imports_dormant_history_without_touching_originals(prepared_import):
    s = prepared_import
    old, data, prepared = files(s["legacy"]), files(s["data"]), files(s["destination"])
    siblings = set(s["output"].parent.iterdir())
    plan = review(s)
    assert set(s["output"].parent.iterdir()) == siblings
    assert not plan["cross_schema_migration_performed"] and not plan["old_checkpoint_resume_allowed"]
    result = run(s)
    assert result["state"] == "committed" and not result["program_installation_performed"]
    assert not s["new_target"].exists() and set(module.BASE_BLOCKERS) <= set(result["legacy_blockers"])
    assert files(s["legacy"]) == old and files(s["data"]) == data and files(s["destination"]) == prepared
    with closing(sqlite3.connect(s["output"] / "scholarflow.db")) as connection:
        assert connection.execute("SELECT answer,provider_context FROM qa_records").fetchone() == ("fixed answer", "{}")
        assert connection.execute("SELECT partial_answer,agent_checkpoint,status FROM foreground_runs").fetchone() == ("Preserve partial output", None, "cancelled")
        assert connection.execute("SELECT checkpoint,status FROM tasks").fetchone() == (None, "cancelled")
        assert connection.execute("SELECT checkpoint,status,writer_token FROM run_attempts").fetchone() == (None, "cancelled", "legacy-import-inert")
        assert connection.execute("SELECT checkpoint,state FROM persistent_subtasks").fetchone() == ("{}", "blocked")
        assert connection.execute("SELECT current_attempt_id,status FROM runtime_runs").fetchone() == (None, "cancelled")
        assert connection.execute("SELECT active FROM subscriptions").fetchone() == (0,)
        assert connection.execute("SELECT approval_mode FROM conversation_options").fetchone() == ("read_only",)
        assert connection.execute("SELECT count(*) FROM service_tool_credentials").fetchone() == (0,)
    assert json.loads((s["output"] / "hooks.json").read_text()) == {}
    assert (s["output"] / "paper.txt").read_text() == "fixed source"
    assert "synthetic-never-export" not in (s["output"] / "config.json").read_text()
    assert schema_snapshot(s["output"] / "scholarflow.db")["sha256"] == registry.CURRENT_SCHEMA
    assert recover(s, plan)["state"] == "committed"


@pytest.mark.parametrize("boundary", ["preparing", "staged", "ready", "before_publish", "published", "committed"])
def test_power_loss_at_each_boundary_recovers_from_archive_without_overwriting_old_data(prepared_import, monkeypatch, boundary):
    s = prepared_import
    plan, old, data = review(s), files(s["legacy"]), files(s["data"])
    monkeypatch.setattr(module, "_checkpoint", lambda name: (_ for _ in ()).throw(PowerLoss()) if name == boundary else None)
    with pytest.raises(PowerLoss):
        module.import_data(*inputs(s), **s["options"], expected_review_sha256=plan["review_sha256"])
    assert files(s["legacy"]) == old and files(s["data"]) == data
    monkeypatch.setattr(module, "_checkpoint", lambda name: None)
    assert recover(s, plan)["state"] == "committed"
    assert files(s["legacy"]) == old and files(s["data"]) == data and not s["new_target"].exists()


@pytest.mark.parametrize("bad", ["unknown_id", "schema", "target_schema", "contract", "review", "prep", "existing", "nested"])
def test_unknown_migration_or_unreviewed_inputs_never_publish(prepared_import, bad):
    s = prepared_import
    plan = review(s)
    if bad == "unknown_id":
        s["options"]["migration_id"] = "path/to/unreviewed-python.py"
    elif bad == "schema":
        with closing(sqlite3.connect(s["data"] / "scholarflow.db")) as connection:
            connection.execute("CREATE TABLE unexpected(value TEXT)"); connection.commit()
    elif bad in ("target_schema", "contract"):
        candidate = s["destination"] / "candidate"
        value = read_json(candidate / "install-manifest.json")
        if bad == "target_schema":
            value["target_schema"]["objects"].append({"type": "table", "name": "unexpected", "table": "unexpected", "sql": "CREATE TABLE unexpected(value TEXT)"})
            value["target_schema"]["sha256"] = digest({key: value["target_schema"][key] for key in ("format", "version", "objects")})
        else:
            value["definition"]["data_contract"]["checkpoint_protocol"] += 1
        value["candidate_id"] = digest({key: item for key, item in value.items() if key != "candidate_id"})
        atomic_write_json(candidate / "install-manifest.json", value); s["sign"](candidate, value)
    elif bad == "prep":
        s["options"]["expected_preparation_sha256"] = "0" * 64
    elif bad == "existing":
        s["output"].mkdir(); (s["output"] / "mine.txt").write_text("retain unrelated")
    elif bad == "nested":
        s["output"] = s["data"] / "forbidden"
    before, old = files(s["data"]), files(s["legacy"])
    with pytest.raises(ReleaseError):
        module.import_data(*inputs(s), **s["options"], expected_review_sha256="0" * 64 if bad == "review" else plan["review_sha256"])
    assert files(s["data"]) == before and files(s["legacy"]) == old and not s["new_target"].exists()


@pytest.mark.parametrize("change", ["result", "journal_hash", "source"])
def test_recovery_rebuilds_independently_and_retains_unrelated_or_changed_output(prepared_import, monkeypatch, change):
    s = prepared_import
    plan = review(s)
    run(s)
    if change in ("result", "journal_hash"):
        (s["output"] / "paper.txt").write_text("external edit preserved")
        if change == "journal_hash":
            state = s["output"].parent / ("." + s["output"].name + "-imports")
            pointer = read_json(state / "pending.json")
            path = state / pointer["transaction_id"] / "journal.json"
            value = read_json(path)
            value["evidence"]["files_sha256"] = digest(module._files(s["output"]))
            atomic_write_json(path, value)
    else:
        (s["data"] / "paper.txt").write_text("old source edited later")
    before, original = files(s["output"]), files(s["data"])
    with pytest.raises(ReleaseError):
        recover(s, plan)
    assert files(s["output"]) == before and files(s["data"]) == original


def test_registry_rejects_unknown_precise_schema_even_if_target_and_source_agree():
    with pytest.raises(ReleaseError):
        registry.resolve(registry.MIGRATION_ID, "f" * 64, "f" * 64, registry.CONTRACT)


@pytest.mark.parametrize("kind", ["target", "contract"])
def test_registry_rejects_changed_exact_target_or_checkpoint_contract(kind):
    with pytest.raises(ReleaseError):
        registry.resolve(registry.MIGRATION_ID, registry.CURRENT_SCHEMA,
            "e" * 64 if kind == "target" else registry.CURRENT_SCHEMA,
            {**registry.CONTRACT, "checkpoint_protocol": 2} if kind == "contract" else registry.CONTRACT)


@pytest.mark.parametrize("boundary", ["ready", "before_publish"])
def test_original_file_changed_at_publish_boundary_never_publishes_stale_review(prepared_import, monkeypatch, boundary):
    s = prepared_import
    def change(name):
        if name == boundary:
            (s["data"] / "paper.txt").write_text("external source edit")
    monkeypatch.setattr(module, "_checkpoint", change)
    with pytest.raises(ReleaseError):
        run(s)
    assert not s["output"].exists()
    assert (s["data"] / "paper.txt").read_text() == "external source edit"


def test_low_space_retains_interrupted_transaction_then_recovers(prepared_import, monkeypatch):
    from types import SimpleNamespace
    s = prepared_import
    plan, old, data = review(s), files(s["legacy"]), files(s["data"])
    real = module.shutil.disk_usage
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(ReleaseError, match="磁盘空间"):
        module.import_data(*inputs(s), **s["options"], expected_review_sha256=plan["review_sha256"])
    assert not s["output"].exists() and files(s["legacy"]) == old and files(s["data"]) == data
    monkeypatch.setattr(module.shutil, "disk_usage", real)
    assert recover(s, plan)["state"] == "committed"


def test_other_importer_os_lock_is_respected(prepared_import):
    s = prepared_import
    state = s["output"].parent / ("." + s["output"].name + "-imports")
    state.mkdir()
    with module._exclusive(state / "import.lock"), pytest.raises(ReleaseError):
        run(s)
    assert not s["output"].exists()


def test_cli_review_import_and_recover_are_offline_and_exclude_local_trust(prepared_import, capsys):
    s = prepared_import
    script = Path(__file__).resolve().parents[2] / "desktop/install_tool.py"
    spec = importlib.util.spec_from_file_location("legacy_import_cli_test", script)
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    trust = s["data"].parent / "outside-import-trust.json"
    atomic_write_json(trust, s["trust"])
    common = ["--preparation", str(s["destination"]), "--legacy", str(s["legacy"]),
        "--data", str(s["data"]), "--target", str(s["new_target"]), "--output", str(s["output"]),
        "--trusted-keys", str(trust), "--migration-id", registry.MIGRATION_ID,
        "--preparation-sha256", s["options"]["expected_preparation_sha256"]]
    assert cli.main(["review-legacy-import", *common]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert cli.main(["import-legacy-data", *common, "--review-sha256", plan["review_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "committed"
    assert cli.main(["recover-legacy-import", *common, "--review-sha256", plan["review_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "committed"
    poisoned = s["output"] / "candidate-supplied-trust.json"
    atomic_write_json(poisoned, s["trust"])
    common[common.index("--trusted-keys") + 1] = str(poisoned)
    assert cli.main(["recover-legacy-import", *common, "--review-sha256", plan["review_sha256"]]) == 2
    assert "信任库" in json.loads(capsys.readouterr().err)["error"]


def test_source_crash_wal_frames_are_retained_and_copied_into_new_history(prepared_import):
    from app.release_database import database_files
    s = prepared_import
    with closing(sqlite3.connect(s["data"] / "scholarflow.db")) as connection:
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("UPDATE qa_records SET answer='answer committed only in WAL'")
        connection.commit()
    s["destination"] = s["data"].parent / "wal-reviewed-preparation"
    s["prepared"] = prepare(s)
    s["options"]["expected_preparation_sha256"] = s["prepared"]["preparation_sha256"]
    before = database_files(s["data"] / "scholarflow.db")
    assert any(row["path"].endswith("-wal") and row["size"] > 0 for row in before)
    assert run(s)["state"] == "committed"
    assert database_files(s["data"] / "scholarflow.db") == before
    with closing(sqlite3.connect(s["output"] / "scholarflow.db")) as connection:
        assert connection.execute("SELECT answer FROM qa_records").fetchone() == ("answer committed only in WAL",)


def test_unreviewed_history_column_mutation_is_detected_and_partial_stage_is_retained(prepared_import, monkeypatch):
    s = prepared_import
    old, data = files(s["legacy"]), files(s["data"])
    real = registry.transform
    def changed(database, migration_id):
        real(database, migration_id)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE qa_records SET answer='unexpected replacement of retained history'")
            connection.commit()
    monkeypatch.setattr(registry, "transform", changed)
    with pytest.raises(ReleaseError, match="历史行"):
        run(s)
    assert not s["output"].exists() and files(s["legacy"]) == old and files(s["data"]) == data
    state = s["output"].parent / ("." + s["output"].name + "-imports")
    pointer = read_json(state / "pending.json")
    assert (state / pointer["transaction_id"] / "stage/scholarflow.db").is_file()


def test_proposed_program_update_state_cannot_be_used_as_import_data(prepared_import):
    s = prepared_import
    s["output"] = s["new_target"].parent / ("." + s["new_target"].name + "-updates")
    with pytest.raises(ReleaseError):
        review(s)


def test_restarted_original_runtime_is_refused_even_when_preparation_bytes_still_match(prepared_import):
    s = prepared_import
    with module._exclusive(s["data"] / ".runtime.lock"), pytest.raises(ReleaseError, match="运行实例"):
        review(s)
    assert not s["output"].exists()
