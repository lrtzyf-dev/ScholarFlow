"""Execution-local models, live policy edits, and child protocol regressions."""
import json

import pytest

from app import approval_store, cancel_store, mcp_policy
from app.agent import loop, subagent
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from app.models import ConversationOptions
from app.runtime_context import bind_runtime_config, current_runtime_config, public_runtime_config


def binding(mode="auto", model="Synthetic-Case-ID"):
    return {"version": 1, "profile_id": "synthetic", "profile_version": 1,
            "model_id": model, "mode": "mock", "credential_ref": "private-reference",
            "approval_mode": mode, "policy_revision": 1}


def tool_run(db, monkeypatch, mode, *, name="mcp__builtin__web_fetch", calls=2):
    monkeypatch.setattr(mcp_policy, "load_policy", lambda: {"enabled_tools": [], "disabled_tools": [], "tool_rules": {}})
    db.add(ConversationOptions(conversation_id="c_policy", approval_mode=mode, revision=1))
    db.commit()
    dispatched = []

    class Model:
        def stream_chat(self, messages, tools=None):
            if not any(m["role"] == "tool" for m in messages):
                yield "final", LLMResponse("", [{"id": f"call{i}", "name": name,
                    "arguments": {"url": f"https://synthetic.invalid/{i}"}} for i in range(calls)])
            else:
                yield "final", LLMResponse("已完成实际执行的操作。")

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "execute_tool", lambda db, name, arguments, **kw:
                        dispatched.append(arguments) or json.dumps({"read": arguments}))
    snapshot = AgentState(phase="model", question="q", conversation_id="c_policy",
                          runtime_config=binding(mode)).snapshot()
    return loop.run_agent(db, "q", conversation_id="c_policy", checkpoint=snapshot,
                          run_id="synthetic_run"), dispatched


@pytest.mark.parametrize("mode,expected", [("always", 2), ("auto", 1), ("full_access", 0)])
def test_conversation_policy_modes_and_run_scoped_grants(db, monkeypatch, mode, expected):
    source, dispatched = tool_run(db, monkeypatch, mode)
    requests = []
    for kind, event in source:
        if kind == "approval_request":
            requests.append(event)
            approval_store.resolve(event["approval_id"], "approve", "session")
    assert len(requests) == expected
    assert len(dispatched) == 2
    assert not approval_store.list_for_run("synthetic_run")


def test_policy_downgrade_after_approval_event_expires_the_grant(db, monkeypatch):
    source, dispatched = tool_run(db, monkeypatch, "auto", calls=1)
    requests = []
    downgraded = False
    for kind, event in source:
        if kind == "approval_request":
            requests.append(event["approval_id"])
            assert not dispatched
            approval_store.resolve(event["approval_id"], "approve", "session")
        elif kind == "approval_result" and event["decision"] == "approve" and not downgraded:
            options = db.get(ConversationOptions, "c_policy")
            options.approval_mode = "always"
            options.revision += 1
            db.commit()
            downgraded = True
    assert len(requests) == 2
    assert len(dispatched) == 1
    assert not approval_store.resolve(requests[0], "approve", "session")


def test_live_full_access_selection_releases_obsolete_wait(db, monkeypatch):
    source, dispatched = tool_run(db, monkeypatch, "always", calls=1)
    requests = []
    superseded = []
    for kind, event in source:
        if kind == "approval_request":
            requests.append(event)
            options = db.get(ConversationOptions, "c_policy")
            options.approval_mode = "full_access"
            options.revision += 1
            db.commit()
        elif kind == "approval_result":
            superseded.append(event["decision"])
    assert len(requests) == 1
    assert superseded == ["superseded"]
    assert len(dispatched) == 1


def test_full_access_respects_disabled_and_force_approval_tools(db, monkeypatch):
    source, dispatched = tool_run(db, monkeypatch, "full_access", calls=1)
    monkeypatch.setattr(mcp_policy, "load_policy", lambda: {"enabled_tools": [],
        "disabled_tools": ["mcp__builtin__web_fetch"], "tool_rules": {}})
    events = list(source)
    assert not dispatched
    assert any(kind == "policy_block" for kind, _ in events)


def test_full_access_cannot_override_tool_forced_approval(db, monkeypatch):
    source, dispatched = tool_run(db, monkeypatch, "full_access", calls=1)
    monkeypatch.setattr(mcp_policy, "load_policy", lambda: {"enabled_tools": [], "disabled_tools": [],
        "tool_rules": {"mcp__builtin__web_fetch": {"approval": "always"}}})
    approvals = []
    for kind, event in source:
        if kind == "approval_request":
            approvals.append(event)
            approval_store.resolve(event["approval_id"], "deny")
    assert len(approvals) == 1
    assert not dispatched


def test_external_search_suffix_is_guarded():
    from app.agent.tools import tool_risk
    assert tool_risk("mcp__external__arxiv_search") == "guarded"


def test_interleaved_generators_restore_their_model_binding(db, monkeypatch):
    observed = []

    class Model:
        def stream_chat(self, messages, tools=None):
            observed.append(current_runtime_config()["model_id"])
            yield "delta", "answer", "begin"
            observed.append(current_runtime_config()["model_id"])
            yield "final", LLMResponse("完成。")

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    sources = [loop.run_agent(db, "q", checkpoint=AgentState(phase="model", question="q",
                runtime_config=binding(model=model)).snapshot()) for model in ("Model-A", "Model-B")]
    active = list(sources)
    while active:
        for source in list(active):
            try:
                next(source)
                assert current_runtime_config() is None
            except StopIteration:
                active.remove(source)
    assert observed == ["Model-A", "Model-B", "Model-A", "Model-B"]
    assert "credential_ref" not in public_runtime_config(binding())


def test_parallel_children_inherit_binding_and_standard_tool_protocol(db, monkeypatch):
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": "list_papers"}])
    monkeypatch.setattr(subagent, "tool_policy_error", lambda name: None)
    monkeypatch.setattr(subagent, "execute_tool", lambda *a, **kw: '{"papers": []}')
    observed = []

    class StrictModel:
        def chat(self, messages, tools=None, **kwargs):
            observed.append(current_runtime_config()["model_id"])
            for message in messages:
                for call in message.get("tool_calls", []):
                    assert call["type"] == "function"
                    assert call["function"]["name"] == "list_papers"
                    assert isinstance(call["function"]["arguments"], str)
            rounds = sum(m["role"] == "tool" for m in messages)
            if tools is not None and rounds < 3:
                return LLMResponse("", [{"id": f"r{rounds}", "name": "list_papers", "arguments": {}}])
            return LLMResponse("完成只读检索。")

    with bind_runtime_config(binding(model="Child-Model")):
        result = subagent.run_subagent(db, StrictModel(), {"topic": "strict protocol"}, max_rounds=3)
        children = subagent.run_subagents(db, StrictModel(), [{"topic": "a"}, {"topic": "b"}])
    assert len(result["evidence"]) == 3
    assert len(children) == 2
    assert set(observed) == {"Child-Model"}
    assert current_runtime_config() is None


def test_cancelled_parent_prevents_child_model_and_tools(db):
    class NeverModel:
        def chat(self, *args, **kwargs):
            pytest.fail("cancelled child must not call the model")
    cancel_store.request_cancel("parent_cancelled")
    try:
        with bind_runtime_config({**binding(), "run_id": "parent_cancelled"}):
            result = subagent.run_subagent(db, NeverModel(), {"topic": "cancel"})
        assert "取消" in result["findings"]
    finally:
        cancel_store.clear("parent_cancelled")


def test_background_queue_freezes_model_before_worker_and_resume_preserves_it(db, monkeypatch):
    from app import task_runner
    from app.models import TaskRecord
    monkeypatch.setattr(loop.settings, "llm_model", "Original-Case-Model")
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(task_runner, "_launch", lambda db, task: task_runner.task_to_dict(task))
    accepted = task_runner.start_agent_task(db, "synthetic task", None, [], "c_background")
    assert accepted["runtime_config"]["model_id"] == "Original-Case-Model"
    assert "credential_ref" not in accepted["runtime_config"]
    monkeypatch.setattr(loop.settings, "llm_model", "New-Default-Model")
    observed = []

    def fake_agent(*args, **kwargs):
        observed.append(kwargs["runtime_config"])
        yield "answer", {"answer": "synthetic completion"}

    monkeypatch.setattr(task_runner, "run_agent", fake_agent)
    monkeypatch.setattr(task_runner.notifier, "notify", lambda *a, **kw: None)
    task_runner._run_in_background(accepted["id"], "synthetic task", None, [], "c_background")
    assert observed[0]["model_id"] == "Original-Case-Model"
    db.expire_all()
    row = db.get(TaskRecord, accepted["id"])
    row.status = "paused"
    row.checkpoint = AgentState(phase="paused", question="synthetic task", conversation_id="c_background",
        max_steps=1, step=1, budget_exhausted=True, runtime_config=row.runtime_config,
        workspace=(row.payload or {}).get("workspace")).snapshot()
    db.commit()
    resumed = task_runner.resume_task(db, row.id, additional_steps=2)
    assert resumed["runtime_config"]["model_id"] == "Original-Case-Model"
    assert db.get(TaskRecord, row.id).checkpoint["runtime_config"]["model_id"] == "Original-Case-Model"


def test_generator_close_restores_outer_binding_and_cleans_pending_approval(db, monkeypatch):
    source, _ = tool_run(db, monkeypatch, "auto", calls=1)
    with bind_runtime_config(binding(model="Outer-Model")):
        for kind, event in source:
            assert current_runtime_config()["model_id"] == "Outer-Model"
            if kind == "approval_request":
                source.close()
                break
        assert current_runtime_config()["model_id"] == "Outer-Model"
    assert not approval_store.list_for_run("synthetic_run")


def test_compaction_uses_bound_model_when_global_mode_is_offline(db, monkeypatch):
    from app import conversation_memory
    from app.models import CompactionCall
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "llm_model", "Wrong-Global-Model")

    class Model:
        def chat(self, *args, **kwargs):
            assert current_runtime_config()["model_id"] == "Summary-Model"
            return LLMResponse("保留输入材料中已核实的任务进度。")

    with bind_runtime_config({**binding(model="Summary-Model"), "mode": "api"}):
        text, mode = conversation_memory._summarize(db.get_bind(), "c_summary", "", [({}, "synthetic history")], Model(), None)
    assert mode == "model"
    assert text
    assert db.query(CompactionCall).one().model == "Summary-Model"


def test_api_client_copies_binding_and_respects_explicit_credential_revocation(db):
    from app import model_profiles as store
    from app.llm import APIClient
    from app.models import ModelCredential
    from app.timeutil import utcnow
    profile = store.save_profile(db, {"name": "Synthetic Provider", "base_url": "https://synthetic.invalid/v1",
        "model_id": "Exact-Model-Case", "api_key": "Synthetic-Key-Only"})
    store.set_default(db, profile.id)
    db.flush()
    frozen = store.capture_run_config(db, "c_revoke")
    db.commit()
    client = APIClient(runtime_config=frozen)
    frozen["model_id"] = "mutated-input"
    assert client._connection(None)[1:3] == ("Synthetic-Key-Only", "Exact-Model-Case")
    row = db.get(ModelCredential, profile.credential_ref)
    row.revoked_at = utcnow()
    db.commit()
    with pytest.raises(store.ModelConfigurationError):
        client._connection(None)
