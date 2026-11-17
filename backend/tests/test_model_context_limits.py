"""Declared capacity reaches the actual HTTP boundary, including old runs."""
from copy import deepcopy
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import call_ledger, model_profiles as profiles, model_capabilities, runtime_context
from app.agent import subagent
from app.agent.active_context import compact_messages, estimate_tokens, ContextCapacityExceeded
from app.db import SessionLocal
from app.llm import APIClient, get_llm
from app.main import app
from app.model_context_limits import context_limits
from app.models import ModelProfile
from app.providers.chat import adapter_for
from app.runtime_context import bind_runtime_config


client = TestClient(app)
FORM = {"name": "Declared window", "model_id": "Exact-CaSe-Model", "base_url": "http://127.0.0.1:41000/v1",
        "api_key": "", "context_window_tokens": 8192, "max_output_tokens": 512}


def create(**values):
    response = client.post("/api/model-profiles", json={**FORM, **values})
    assert response.status_code == 200, response.text
    return response.json()


def capture(db, profile_id):
    frozen = profiles.capture_run_config(db, None, selected_profile_id=profile_id)
    db.commit()
    return frozen


@pytest.fixture
def transport(monkeypatch):
    calls, real_client = [], httpx.Client
    def dispatch(request):
        body = json.loads(request.content)
        calls.append(body)
        if body.get("stream"):
            payload = {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
                                                       "finish_reason": "stop"}]})
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs:
                        real_client(*args, transport=httpx.MockTransport(dispatch), **kwargs))
    return calls


@pytest.mark.parametrize("field,value", [(key, value) for key in ("context_window_tokens", "max_output_tokens")
                                        for value in (True, False, 0, -1, 1.5, "8192", None)])
def test_invalid_types_and_nonpositive_values_rejected_without_saving(field, value):
    response = client.post("/api/model-profiles", json={**FORM, field: value, "api_key": "test-key-5dd5ad904e08dce6"})
    assert response.status_code == 422 and "test-key-5dd5ad904e08dce6" not in response.text
    with SessionLocal() as db:
        assert db.query(ModelProfile).count() == 0


@pytest.mark.parametrize("values", [{"context_window_tokens": 512, "max_output_tokens": 512},
                                  {"context_window_tokens": 512, "max_output_tokens": 513},
                                  {"context_window_tokens": 16777217}, {"max_output_tokens": 1048577}])
def test_invalid_relations_or_product_bounds_rejected(values):
    assert client.post("/api/model-profiles", json={**FORM, **values}).status_code == 422


def test_create_and_partial_update_validate_pair_atomically():
    profile = create()
    assert profile["context_window_tokens"] == 8192 and profile["max_output_tokens"] == 512
    assert profile["context_limits_source"] == "configured" and profile["context_limits_verified"] is False
    for change in ({"context_window_tokens": 400}, {"max_output_tokens": 8192}, {"max_output_tokens": None}, {"max_output_tokens": True}):
        rejected = client.put("/api/model-profiles/" + profile["id"], json={"expected_version": 1, **change})
        assert rejected.status_code == 422
    saved = next(p for p in client.get("/api/model-profiles").json()["profiles"] if p["id"] == profile["id"])
    assert saved == profile
    updated = client.put("/api/model-profiles/" + profile["id"], json={"expected_version": 1,
                         "context_window_tokens": 32768, "max_output_tokens": 2048})
    assert updated.status_code == 200 and updated.json()["version"] == 2


@pytest.mark.parametrize("window,output", [(8192, 512), (32768, 2048), (131072, 8192)])
def test_frozen_run_uses_configured_wire_limit_and_capacity(db, transport, window, output):
    profile = create(context_window_tokens=window, max_output_tokens=output)
    frozen = capture(db, profile["id"])
    with bind_runtime_config(frozen):
        assert get_llm().chat([{"role": "user", "content": "hello"}]).content == "OK"
        events = list(get_llm().stream_chat([{"role": "user", "content": "hello again"}]))
    assert events[-1][0] == "final"
    assert len(transport) == 2 and all(body["max_tokens"] == output for body in transport)
    assert all(estimate_tokens(body["messages"], body.get("tools")) + output <= window for body in transport)
    for public in (profiles.public_runtime_config(frozen), runtime_context.public_runtime_config(frozen)):
        assert public["context_window_tokens"] == window and public["max_output_tokens"] == output
        assert "credential_ref" not in public


def test_profile_and_default_edits_never_change_frozen_client_or_old_run(db, transport):
    profile = create()
    frozen = capture(db, profile["id"])
    admitted = APIClient(runtime_config=frozen, synthetic=True)
    frozen["max_output_tokens"] = 999  # A caller cannot mutate the admitted copy.
    response = client.put("/api/model-profiles/" + profile["id"], json={"expected_version": 1,
                          "context_window_tokens": 32768, "max_output_tokens": 2048})
    assert response.status_code == 200
    other = create(name="Another model", max_output_tokens=1024)
    assert client.put("/api/model-profiles/default", json={"model_profile_id": other["id"]}).status_code == 200
    admitted.chat([{"role": "user", "content": "old task"}])
    fresh = capture(db, profile["id"])
    APIClient(runtime_config=fresh, synthetic=True).chat([{"role": "user", "content": "new task"}])
    assert [body["max_tokens"] for body in transport] == [512, 2048]
    assert admitted._context_limits == {"context_window_tokens": 8192, "max_output_tokens": 512}


def test_provider_options_cannot_override_frozen_output_budget(db, transport):
    frozen = capture(db, create()["id"])
    APIClient(runtime_config=frozen, synthetic=True, provider_options={"max_output_tokens": 10000}).chat(
        [{"role": "user", "content": "hello"}])
    assert transport[0]["max_tokens"] == 512


def test_oversized_plain_or_signed_input_rejected_before_ledger_or_http(db, transport):
    frozen = capture(db, create(context_window_tokens=1024, max_output_tokens=128)["id"])
    messages = [{"role": "user", "content": "原始材料不可丢失" * 2000}]
    with pytest.raises(ContextCapacityExceeded):
        APIClient(runtime_config=frozen, synthetic=True).chat(messages)
    assert transport == [] and call_ledger.list_calls(include_synthetic=True)["items"] == []
    assert messages[0]["content"] == "原始材料不可丢失" * 2000


def test_real_small_window_task_pauses_with_checkpoint_instead_of_done(db, transport, monkeypatch):
    from app.agent import loop
    from app.agent.checkpoint import AgentState, prepare_resume, recovery_info
    from app.models import QARecord
    frozen = capture(db, create(context_window_tokens=1024, max_output_tokens=128)["id"])
    monkeypatch.setattr(loop, "get_llm", lambda: APIClient(runtime_config=frozen, synthetic=True))
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    states = []
    question = "保留研究目标和导出要求。" * 2000
    events = list(loop.run_agent(db, question, runtime_config=frozen,
                                save_checkpoint=lambda value: states.append(deepcopy(value))))
    assert "paused" in [kind for kind, data in events]
    assert not any(kind in ("done", "answer", "error") for kind, data in events)
    saved = AgentState.model_validate(states[-1])
    assert saved.phase == "model" and saved.resource_pause["kind"] == "context"
    assert saved.question == question and saved.result is None
    assert db.query(QARecord).count() == 0 and transport == []
    assert recovery_info(saved.snapshot())["available"]
    resumed, new_work = prepare_resume(saved.snapshot())
    assert new_work and resumed.question == question and resumed.runtime_config["context_window_tokens"] == 1024


def test_active_compaction_and_actual_request_share_limits_and_keep_archive(db, transport):
    frozen = capture(db, create()["id"])
    messages = [{"role": "user", "content": "保留比较和导出要求"}]
    for index in range(6):
        messages.extend([{"role": "assistant", "tool_calls": [{"id": str(index), "type": "function",
            "function": {"name": "read_project_file", "arguments": json.dumps({"path": f"file{index}"})}}]},
            {"role": "tool", "tool_call_id": str(index), "content": "verified proof " * 350}])
    original, archive = deepcopy(messages), {}
    outgoing, manifest = compact_messages(messages, binding=frozen, archive=archive)
    assert archive and manifest["window_tokens"] == 8192 and manifest["output_reserved_tokens"] == 512
    APIClient(runtime_config=frozen, synthetic=True).chat(outgoing)
    assert transport[0]["max_tokens"] == 512 and messages == original
    assert estimate_tokens(transport[0]["messages"]) + 512 <= 8192


def test_old_profile_and_old_run_missing_fields_retain_compatibility_without_live_lookup(db, transport):
    profile = ModelProfile(name="Migrated", base_url=FORM["base_url"], model_id="old-model")
    db.add(profile); db.commit()
    displayed = profiles.profile_snapshot(profile)
    assert displayed["context_limits_source"] == "compatibility_default"
    old = {"version": 1, "mode": "api", "profile_id": profile.id, "base_url": FORM["base_url"], "model_id": "old-model"}
    profile.context_window_tokens, profile.max_output_tokens = 8192, 64
    db.commit()
    resolved = profiles.resolve_model_config(old)
    assert resolved["context_window_tokens"] == 131072 and resolved["max_output_tokens"] == 8192
    APIClient(runtime_config=old, synthetic=True).chat([{"role": "user", "content": "resume old checkpoint"}])
    assert transport[0]["max_tokens"] == 8192
    assert runtime_context.public_runtime_config(old)["context_limits_source"] == "compatibility_default"


def test_default_named_standalone_and_parallel_children_inherit_frozen_limits(db, transport, monkeypatch):
    profile = create()
    profiles.set_default(db, profile["id"]); db.commit()
    get_llm().chat([{"role": "user", "content": "standalone writing"}])
    frozen = {**capture(db, profile["id"]), "run_id": "parent", "attempt_id": "attempt"}
    from app.models import Conversation
    from app.runtime_models import RuntimeRun, RunAttempt
    from app.goal_ledger import ensure_run_goal
    db.add(Conversation(id="context-parent")); db.flush()
    goal = ensure_run_goal(db, "parent", "context-parent", "Synthetic frozen-limit children")
    db.add(RuntimeRun(id="parent", kind="foreground", conversation_id="context-parent", goal_id=goal["id"], current_attempt_id="attempt"))
    db.add(RunAttempt(id="attempt", run_id="parent", number=1, writer_token="context-parent-writer", status="running"))
    db.commit()
    frozen.update(conversation_id="context-parent", goal_id=goal["id"])
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [])
    with bind_runtime_config(frozen):
        outputs = subagent.run_subagents(db, get_llm(), [{"topic": "A"}, {"topic": "B"}], max_workers=2)
    assert len(outputs) == 2 and all(result["findings"] == "OK" for result in outputs)
    assert len(transport) == 3 and all(body["max_tokens"] == 512 for body in transport)


def test_connection_and_capability_probes_inherit_limits_without_discovering_them(transport):
    profile = create()
    assert client.post(f'/api/model-profiles/{profile["id"]}/test').json()["ok"] is True
    preview = model_capabilities.preview(profile["id"])
    assert preview["context_limits"]["max_output_tokens"] == 512
    assert preview["context_limits"]["context_limits_verified"] is False
    assert model_capabilities.run(profile["id"], "text", preview["expected_config"])["status"] == "supported"
    assert len(transport) == 2 and all(body["max_tokens"] == 512 for body in transport)
    saved = next(p for p in client.get("/api/model-profiles").json()["profiles"] if p["id"] == profile["id"])
    assert saved["context_window_tokens"] == 8192 and saved["context_limits_verified"] is False


def test_limit_change_invalidates_pending_capability_probe_without_http(transport):
    profile = create()
    previous = model_capabilities.preview(profile["id"])
    assert client.put(f'/api/model-profiles/{profile["id"]}', json={"expected_version": 1, "max_output_tokens": 256}).status_code == 200
    with pytest.raises(model_capabilities.ConfigurationChanged):
        model_capabilities.run(profile["id"], "text", previous["expected_config"])
    assert transport == []


@pytest.mark.parametrize("base,field", [("https://api.deepseek.com/v1", "max_tokens"),
                                       ("https://gateway.example.test/v1", "max_tokens"),
                                       ("https://api.openai.com/v1", "max_completion_tokens")])
def test_output_parameter_follows_endpoint_protocol_not_model_name(base, field):
    _, _, body = adapter_for(base, "Any-Case-Model", "").request("", [{"role": "user", "content": "hello"}], max_output_tokens=321)
    assert body[field] == 321
    assert ("max_completion_tokens" if field == "max_tokens" else "max_tokens") not in body


def test_legacy_schema_columns_migrate_null_preserving_original_rows(monkeypatch):
    from app import db as database
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE model_profiles (id TEXT PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO model_profiles VALUES ('old', 'Preserve me')"))
    monkeypatch.setattr(database, "engine", engine)
    database._lightweight_migrations()
    database._lightweight_migrations()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name, context_window_tokens, max_output_tokens FROM model_profiles")).one() == ("Preserve me", None, None)
    engine.dispose()


def test_declared_limits_survive_credential_free_backup_restore_and_clean_startup(tmp_path):
    from contextlib import closing
    import os
    from pathlib import Path
    import sqlite3
    import subprocess
    import sys
    from app import data_backup
    from tests.test_backup_credentials import make_private_data, PROFILE_ID
    source = tmp_path / "source"
    make_private_data(source)
    with closing(sqlite3.connect(source / "scholarflow.db")) as connection:
        connection.execute("UPDATE model_profiles SET context_window_tokens=8192,max_output_tokens=512 WHERE id=?", (PROFILE_ID,))
        connection.commit()
    exported = data_backup.create_backup(source)
    target = tmp_path / "restored"
    preview = data_backup.prepare_restore(target, exported["path"])
    data_backup.schedule_restore(target, preview["id"])
    assert data_backup.apply_pending_restore(target)
    with closing(sqlite3.connect(target / "scholarflow.db")) as restored:
        assert restored.execute("SELECT context_window_tokens,max_output_tokens,credential_ref,enabled FROM model_profiles WHERE id=?", (PROFILE_ID,)).fetchone() == (8192, 512, None, 0)
        assert restored.execute("SELECT COUNT(*) FROM model_credentials").fetchone() == (0,)
    script = '''
import asyncio,json
from app.main import app,lifespan
from app.db import SessionLocal
from app.models import ModelProfile
from app.model_profiles import profile_snapshot
async def verify():
    async with lifespan(app):
        with SessionLocal() as db:
            p=db.query(ModelProfile).one()
            v=profile_snapshot(p)
            print(json.dumps({k:v[k] for k in ('context_window_tokens','max_output_tokens','context_limits_verified','api_key_set','enabled')}))
asyncio.run(verify())
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, "DATA_DIR": str(target), "LLM_API_KEY": "", "EMBEDDING_API_KEY": "",
                                 "LLM_DEMO_MODE": "1", "REDIS_URL": "", "DESKTOP_NOTIFICATIONS": "false"},
                            capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "context_window_tokens": 8192, "max_output_tokens": 512, "context_limits_verified": False,
        "api_key_set": False, "enabled": False}


def test_backup_still_rejects_unknown_secret_slot_next_to_declared_limits(tmp_path):
    from contextlib import closing
    import sqlite3
    from app import data_backup
    from tests.test_backup_credentials import make_private_data
    source = tmp_path / "source"
    make_private_data(source)
    with closing(sqlite3.connect(source / "scholarflow.db")) as connection:
        connection.execute("ALTER TABLE model_profiles ADD COLUMN unknown_model_secret TEXT")
        connection.execute("UPDATE model_profiles SET unknown_model_secret='test-key-9e70a7ce692f1c75'")
        connection.commit()
    with pytest.raises(data_backup.BackupError, match="无法安全识别"):
        data_backup.create_backup(source)


@pytest.mark.parametrize("values", [{"context_window_tokens": True}, {"max_output_tokens": 0},
                                  {"context_window_tokens": 10, "max_output_tokens": 10}])
def test_direct_store_and_compactor_cannot_bypass_strict_validation(db, values):
    with pytest.raises(ValueError):
        profiles.save_profile(db, {**FORM, **values})
    db.rollback()
    with pytest.raises(ContextCapacityExceeded):
        compact_messages([{"role": "user", "content": "q"}], binding={**FORM, **values})

