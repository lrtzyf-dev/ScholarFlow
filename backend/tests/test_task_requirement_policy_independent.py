"""Independent actual executor/child exits; no external tool or real model."""
from copy import deepcopy
from contextlib import contextmanager
import asyncio
import hashlib
import json
from types import SimpleNamespace
import pytest
import httpx

from app import goal_ledger as ledger, source_runtime, source_tools  # Import aliases before the scoped probe patch.
from app.agent import loop, subagent, execution
from app.agent.checkpoint import AgentState
from app.agent.active_context import scope_fingerprint, CONTEXT_TOOL
from app.db import SessionLocal
from app.llm import LLMResponse
from app.runtime_context import bind_runtime_config
from tests.test_task_requirements import base, change


def bound(goal):
    return {"goal_id": goal["id"], "run_id": "criteria-run", "conversation_id": "criteria-a"}


def fake_bridge(monkeypatch):
    from app.mcp_bridge import MCPBridge, _Conn
    bridge = MCPBridge.__new__(MCPBridge)
    connection = _Conn(config={"enabled": True})
    bridge._conns = {"independent": connection}
    monkeypatch.setattr(bridge, "_authorization_available", lambda _conn: True)
    return bridge, connection


def test_main_context_archive_cannot_refill_file_after_explicit_no_file_read(db, monkeypatch):
    goal = change(db, base(db), {"kind": "policy", "rule": "no_file_read"})
    db.commit()
    binding = {"conversation_id": "criteria-a", "run_id": "criteria-run", "goal_id": goal["id"]}
    secret = "INDEPENDENT_ARCHIVE_FILE_BODY"
    entry = {"content": secret, "sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "scope_fingerprint": scope_fingerprint(binding), "tool_name": "read_attached_source",
        "arguments": {"source_revision_id": "authorized-synthetic-source"}}
    call = {"id": "independent-archive-call", "name": CONTEXT_TOOL, "arguments": {"reference": "old-body"}}
    state = AgentState(question="请解释方法并保留局限。", conversation_id="criteria-a", goal_id=goal["id"],
        phase="tools", calls=[call], context_archive={"old-body": entry}, runtime_config=binding,
        messages=[{"role": "user", "content": "请解释方法并保留局限。"}])
    monkeypatch.setattr(source_runtime, "require_attached_revision", lambda *a, **k: {"revision_id": "authorized-synthetic-source"})
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop, "get_llm", lambda: SimpleNamespace())
    monkeypatch.setattr(execution, "ensure_memory_current", lambda *a, **k: False)
    snapshots = []
    source = loop.run_agent(db, state.question, conversation_id="criteria-a", run_id="criteria-run",
        runtime_config=binding, checkpoint=state.snapshot(), save_checkpoint=lambda row: snapshots.append(deepcopy(row)))
    seen = []
    try:
        for kind, event in source:
            seen.append((kind, event))
            if kind == "tool_result" and event.get("name") == CONTEXT_TOOL:
                break
    finally:
        source.close()
    delivered = [m["content"] for m in snapshots[-1]["messages"] if m.get("role") == "tool"]
    assert delivered and all(secret not in body for body in delivered), seen
    assert any("denied" in body or "blocked_by_goal_constraint" in body for body in delivered)


def test_already_running_child_honors_cross_session_no_external_before_mcp_dispatch(db, monkeypatch):
    goal = base(db); db.commit()
    name = "mcp__builtin__web_fetch"
    spec = {"name": name, "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [spec])
    monkeypatch.setattr(subagent, "approval_for", lambda _name: "never")
    monkeypatch.setattr(subagent, "tool_policy_error", lambda _name: None)
    monkeypatch.setattr(subagent, "assert_request_memory", lambda binding: None)
    calls = []
    monkeypatch.setattr(subagent, "execute_tool", lambda *a, **k: calls.append(a[1]) or '{"text":"synthetic transport"}')
    requests = []
    class Model:
        count = 0
        def chat(self, messages, **kwargs):
            self.count += 1
            requests.append((deepcopy(messages), deepcopy(kwargs.get("tools"))))
            if self.count == 1:
                with SessionLocal() as other:
                    fresh = ledger.snapshot_goal(other, goal["id"])
                    change(other, fresh, {"kind": "policy", "rule": "no_external_tools"})
                    other.commit()
                return LLMResponse("", [{"id": "external-after-update", "name": name, "arguments": {"url": "https://example.test"}}])
            return LLMResponse("已有资料不足，保留缺项。")
    with bind_runtime_config({"goal_id": goal["id"], "run_id": "criteria-run", "conversation_id": "criteria-a"}):
        result = subagent.run_subagent(db, Model(), {"topic": "独立出口探针"})
    assert calls == []
    assert any("blocked_by_goal_constraint" in item["result"] for item in result["evidence"])
    assert "no_external_tools" in json.dumps(requests[-1][0])
    assert name not in {item["name"] for item in requests[-1][1]}


def test_actual_mcp_dispatch_rechecks_policy_after_connection_wait(db, monkeypatch):
    goal = base(db); db.commit()
    bridge, connection = fake_bridge(monkeypatch)
    calls = []
    async def remote(tool, arguments):
        calls.append((tool, arguments))
        return SimpleNamespace(content=[], is_error=False)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def connect(server, **kwargs):
            entered.set(); await release.wait()
            connection.client = SimpleNamespace(call_tool=remote)
        monkeypatch.setattr(bridge, "_connect", connect)
        dispatched = asyncio.create_task(bridge._call_tool("independent", "read", {}))
        await entered.wait()
        with SessionLocal() as other:
            fresh = ledger.snapshot_goal(other, goal["id"])
            change(other, fresh, {"kind": "policy", "rule": "no_external_tools"}); other.commit()
        release.set()
        return json.loads(await dispatched)
    with bind_runtime_config(bound(goal)):
        payload = asyncio.run(scenario())
    assert payload["blocked_by_goal_constraint"] and payload["not_executed"] and calls == []


def test_interactive_mcp_without_runtime_binding_does_not_borrow_another_goal(db, monkeypatch):
    goal = change(db, base(db), {"kind": "policy", "rule": "no_external_tools"}); db.commit()
    bridge, connection = fake_bridge(monkeypatch)
    calls = []
    async def remote(tool, arguments):
        calls.append(tool)
        return SimpleNamespace(content=[], is_error=False)
    connection.client = SimpleNamespace(call_tool=remote)
    with bind_runtime_config(None):
        payload = json.loads(asyncio.run(bridge._call_tool("independent", "interactive-read", {})))
    assert calls == ["interactive-read"] and not payload["is_error"]


@pytest.mark.parametrize("invalid", ["missing_goal", "missing_run", "deleted_conversation", "missing_attempt"])
def test_bound_invalid_parent_never_dispatches_mcp(db, monkeypatch, invalid):
    from app.models import Conversation
    goal = base(db); db.commit()
    binding = bound(goal)
    if invalid == "missing_goal": binding["goal_id"] = "missing"
    elif invalid == "missing_run": binding["run_id"] = "missing"
    elif invalid == "missing_attempt":
        from app.runtime_models import RuntimeRun
        db.get(RuntimeRun, "criteria-run").current_attempt_id = "managed-attempt"; db.commit()
    else: db.delete(db.get(Conversation, "criteria-a")); db.commit()
    bridge, connection = fake_bridge(monkeypatch)
    async def forbidden(*args): pytest.fail("Invalid bound parent dispatched MCP")
    connection.client = SimpleNamespace(call_tool=forbidden)
    with bind_runtime_config(binding):
        result = json.loads(asyncio.run(bridge._call_tool("independent", "read", {})))
    assert result["blocked_by_goal_constraint"] and result["not_executed"]


def test_mcp_discovery_cannot_start_process_under_no_commands(db, monkeypatch):
    goal = change(db, base(db), {"kind": "policy", "rule": "no_commands"}); db.commit()
    bridge, connection = fake_bridge(monkeypatch)
    monkeypatch.setattr(bridge, "_ensure_loop", lambda: pytest.fail("Discovery scheduled a process"))
    with bind_runtime_config(bound(goal)):
        bridge._schedule_discovery("independent", connection)


def test_mcp_connect_lock_wait_rechecks_new_no_commands_before_open(db, monkeypatch):
    goal = base(db); db.commit()
    bridge, connection = fake_bridge(monkeypatch)
    async def forbidden(*args): pytest.fail("Restricted connection opened")
    monkeypatch.setattr(bridge, "_open_connection", forbidden)
    async def scenario():
        connection.connect_lock = asyncio.Lock()
        await connection.connect_lock.acquire()
        waiting = asyncio.create_task(bridge._connect("independent"))
        await asyncio.sleep(.01)
        with SessionLocal() as other:
            fresh = ledger.snapshot_goal(other, goal["id"])
            change(other, fresh, {"kind": "policy", "rule": "no_commands"}); other.commit()
        connection.connect_lock.release()
        await waiting
    with bind_runtime_config(bound(goal)):
        asyncio.run(scenario())
    assert connection.client is None


@pytest.mark.parametrize("rule", ["no_commands", "no_external_tools", "no_file_read", "no_file_write"])
def test_actual_hook_process_boundary_honors_each_constraint(db, monkeypatch, rule):
    from app import hooks_store
    goal = change(db, base(db), {"kind": "policy", "rule": rule}); db.commit()
    monkeypatch.setattr(hooks_store.subprocess, "run", lambda *a, **k: pytest.fail("Restricted hook started"))
    with bind_runtime_config(bound(goal)):
        result = hooks_store._run_one("synthetic hook", {}, 1)
    assert result["not_executed"] and result["blocked_by_goal_constraint"]


def test_two_hooks_refresh_between_processes_and_only_count_actual_dispatch(db, monkeypatch):
    from app import hooks_store
    goal = base(db); db.commit()
    calls = []
    monkeypatch.setattr(hooks_store, "load_hooks", lambda: {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "first"}, {"type": "command", "command": "second"}]}]})
    def process(command, **kwargs):
        calls.append(command)
        with SessionLocal() as other:
            fresh = ledger.snapshot_goal(other, goal["id"])
            change(other, fresh, {"kind": "policy", "rule": "no_commands"}); other.commit()
        return SimpleNamespace(returncode=0, stdout="done", stderr="")
    monkeypatch.setattr(hooks_store.subprocess, "run", process)
    with bind_runtime_config(bound(goal)):
        result = hooks_store.run_hooks("PreToolUse")
    assert len(calls) == result["ran"] == 1
    assert result["results"][-1]["not_executed"]


def test_paused_parent_stops_child_before_next_model_request(db, monkeypatch):
    from app.goal_models import Goal
    goal = base(db)
    db.get(Goal, goal["id"]).control_state = "paused"; db.commit()
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [])
    monkeypatch.setattr(subagent, "assert_request_memory", lambda binding: None)
    class Model:
        def chat(self, *args, **kwargs): pytest.fail("Paused parent made a new model request")
    with bind_runtime_config(bound(goal)):
        result = subagent.run_subagent(db, Model(), {"topic": "paused child"})
    assert result["outcome"] == "blocked"


def test_child_context_archive_reauthorizes_original_tool_against_parent_goal(db, monkeypatch):
    goal = change(db, base(db), {"kind": "policy", "rule": "no_file_read"}); db.commit()
    binding = bound(goal)
    secret = "INDEPENDENT_CHILD_ARCHIVE_FILE_BODY"
    entry = {"content": secret, "sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "scope_fingerprint": scope_fingerprint(binding), "tool_name": "read_attached_source",
        "arguments": {"source_revision_id": "authorized-synthetic-source"}}
    checkpoint = {"calls": [{"id": "archive-read", "name": CONTEXT_TOOL, "arguments": {"reference": "body"}}],
        "context_archive": {"body": entry}}
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": "read_attached_source"}])
    monkeypatch.setattr(subagent, "assert_request_memory", lambda binding: None)
    monkeypatch.setattr(source_runtime, "require_attached_revision", lambda *a, **k: {"revision_id": "authorized-synthetic-source"})
    class Model:
        def chat(self, messages, **kwargs): return LLMResponse("缺少可用资料，保留待核对项。")
    node, _ = subagent.subtask_store.claim(parent_run_id="criteria-run", parent_attempt_id=None,
        specification={"topic": "archive child"}, scope={}, conversation_id="criteria-a")
    node["checkpoint"] = checkpoint
    with bind_runtime_config(binding):
        result = subagent.run_subagent(db, Model(), {"topic": "archive child"}, node=node)
    assert secret not in json.dumps(result["evidence"])
    assert "context_access_denied" in json.dumps(result["evidence"])


def test_new_parent_attempt_during_mcp_connection_never_dispatches_old_call(db, monkeypatch):
    from app.runtime_models import RuntimeRun
    goal = base(db)
    db.get(RuntimeRun, "criteria-run").current_attempt_id = "old-attempt"; db.commit()
    bridge, connection = fake_bridge(monkeypatch)
    async def forbidden(*args): pytest.fail("Old parent attempt dispatched MCP")
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def connect(server, **kwargs):
            entered.set(); await release.wait()
            connection.client = SimpleNamespace(call_tool=forbidden)
        monkeypatch.setattr(bridge, "_connect", connect)
        waiting = asyncio.create_task(bridge._call_tool("independent", "read", {}))
        await entered.wait()
        with SessionLocal() as other:
            other.get(RuntimeRun, "criteria-run").current_attempt_id = "new-attempt"; other.commit()
        release.set()
        return json.loads(await waiting)
    with bind_runtime_config({**bound(goal), "attempt_id": "old-attempt"}):
        result = asyncio.run(scenario())
    assert result["not_executed"] and "新尝试" in result["error"]


def test_child_explicit_policy_cancel_restores_legal_tool_next_request(db, monkeypatch):
    goal = change(db, base(db), {"kind": "policy", "rule": "no_external_tools"}); db.commit()
    name = "mcp__builtin__web_fetch"
    spec = {"name": name, "parameters": {"type": "object", "properties": {}}}
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [spec])
    monkeypatch.setattr(subagent, "approval_for", lambda _name: "never")
    monkeypatch.setattr(subagent, "tool_policy_error", lambda _name: None)
    monkeypatch.setattr(subagent, "assert_request_memory", lambda binding: None)
    dispatched, requests = [], []
    monkeypatch.setattr(subagent, "execute_tool", lambda *a, **k: dispatched.append(a[1]) or '{"text":"real tool stub"}')
    class Model:
        def chat(self, messages, **kwargs):
            requests.append(deepcopy(kwargs.get("tools")))
            if len(requests) == 1:
                with SessionLocal() as other:
                    current = ledger.snapshot_goal(other, goal["id"])
                    row = next(row for row in current["requirements"] if row["origin"] == "user")
                    change(other, current, action="cancel", requirement_id=row["id"]); other.commit()
                return LLMResponse("", [{"id": "after-cancel", "name": name, "arguments": {}}])
            return LLMResponse("已恢复合法读取，结论仍需父任务核验。")
    with bind_runtime_config(bound(goal)):
        result = subagent.run_subagent(db, Model(), {"topic": "cancel constraint"})
    assert name not in {item["name"] for item in requests[0]}
    assert name in {item["name"] for item in requests[1]}
    assert dispatched == [name] and result["evidence"]


def test_interactive_hook_without_binding_ignores_unrelated_goal(db, monkeypatch):
    from app import hooks_store
    change(db, base(db), {"kind": "policy", "rule": "no_commands"}); db.commit()
    calls = []
    def process(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="synthetic success", stderr="")
    monkeypatch.setattr(hooks_store.subprocess, "run", process)
    with bind_runtime_config(None):
        result = hooks_store._run_one("interactive hook", {}, 1)
    assert len(calls) == 1 and result["exit_code"] == 0


def test_sync_mcp_cross_thread_submission_keeps_parent_policy_binding(db, monkeypatch):
    import threading
    from app.mcp_bridge import MCPBridge
    goal = change(db, base(db), {"kind": "policy", "rule": "no_external_tools"}); db.commit()
    bridge, connection = fake_bridge(monkeypatch)
    async def forbidden(*args): pytest.fail("Cross-thread MCP lost parent context")
    connection.client = SimpleNamespace(call_tool=forbidden)
    background = asyncio.new_event_loop()
    thread = threading.Thread(target=background.run_forever, daemon=True); thread.start()
    monkeypatch.setattr(bridge, "_ensure_loop", lambda: background)
    try:
        with bind_runtime_config(bound(goal)):
            result = json.loads(bridge.call_tool("mcp__independent__read", {}))
        assert result["not_executed"] and result["blocked_by_goal_constraint"]
    finally:
        background.call_soon_threadsafe(background.stop); thread.join(5); background.close()


def model_transport(monkeypatch, action=None):
    from app.llm import APIClient
    class SyntheticModel(APIClient):
        def _connection(self, _effort):
            return "https://parent-policy.example.test/v1", "synthetic-policy-only", "synthetic-model", ""
    original = httpx.Client
    calls = []
    def remote(request):
        calls.append(request)
        if action: action()
        payload = {"id": "synthetic-policy-receipt", "choices": [{"message": {"role": "assistant", "content": "accepted"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        if json.loads(request.content).get("stream"):
            payload["choices"] = [{"delta": {"content": "accepted"}, "finish_reason": "stop"}]
            return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=payload)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: original(*a, transport=httpx.MockTransport(remote), **k))
    return SyntheticModel(synthetic=True), calls


def model_call(model, stream):
    if stream: return list(model.stream_chat([{"role": "user", "content": "synthetic request"}]))
    return model.chat([{"role": "user", "content": "synthetic request"}])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("update", ["pause", "attempt", "delete_conversation"])
def test_model_actual_transport_rechecks_parent_after_admission(db, monkeypatch, stream, update):
    from app import request_resources
    from app.runtime_models import RuntimeRun, RunAttempt
    from app.goal_models import Goal
    from app.models import Conversation
    from app.goal_policy import GoalPolicyChanged
    from app.run_lifecycle import StaleAttempt
    from app.ledger_models import CallRecord
    goal = base(db)
    db.get(RuntimeRun, "criteria-run").current_attempt_id = "first"
    db.add(RunAttempt(id="first", run_id="criteria-run", number=1, writer_token="policy-test-writer", status="running"))
    db.commit()
    admitted = request_resources.request_admission
    @contextmanager
    def changed_during_queue(**kwargs):
        with admitted(**kwargs) as receipt:
            with SessionLocal() as other:
                if update == "pause": other.get(Goal, goal["id"]).control_state = "paused"
                elif update == "attempt": other.get(RuntimeRun, "criteria-run").current_attempt_id = "replacement"
                else: other.delete(other.get(Conversation, "criteria-a"))
                other.commit()
            yield receipt
    monkeypatch.setattr(request_resources, "request_admission", changed_during_queue)
    model, calls = model_transport(monkeypatch)
    with bind_runtime_config({**bound(goal), "attempt_id": "first"}):
        with pytest.raises((StaleAttempt, GoalPolicyChanged) if update == "attempt" else GoalPolicyChanged): model_call(model, stream)
    assert calls == []
    db.expire_all()
    rows = db.query(CallRecord).all()
    assert len(rows) == 1 and rows[0].state == "failed"
    assert rows[0].error_kind in (("StaleAttempt", "GoalPolicyChanged") if update == "attempt" else ("GoalPolicyChanged",))


@pytest.mark.parametrize("stream", [False, True])
def test_parent_pause_after_model_dispatch_keeps_known_result_and_usage(db, monkeypatch, stream):
    from app import resource_budget
    from app.goal_models import Goal
    from app.ledger_models import CallRecord, CallUsageReceipt
    goal = base(db); db.commit()
    budget = resource_budget.create_budget(owner_run_id="criteria-run", amount_limit="1", request_estimate="0.4", price_version="synthetic-policy-estimate")
    def pause_after_dispatch():
        with SessionLocal() as other:
            other.get(Goal, goal["id"]).control_state = "paused"; other.commit()
    model, calls = model_transport(monkeypatch, pause_after_dispatch)
    with bind_runtime_config({**bound(goal), "resource_budget_id": budget}): model_call(model, stream)
    db.expire_all()
    rows = db.query(CallRecord).all()
    assert len(calls) == len(rows) == 1 and rows[0].state == "completed"
    assert db.query(CallUsageReceipt).count() >= 1
    assert rows[0].estimated_usage["total_tokens"] == 5
    # Known token receipt is preserved; no tariff or supplier charge is invented.
    assert resource_budget.budget_snapshot(budget)["reserved_amount"] == "0.4"


@pytest.mark.parametrize("interactive", [False, True])
def test_no_external_policy_allows_selected_model_and_unbound_interactive_model(db, monkeypatch, interactive):
    goal = change(db, base(db), {"kind": "policy", "rule": "no_external_tools"}); db.commit()
    model, calls = model_transport(monkeypatch)
    with bind_runtime_config(None if interactive else bound(goal)):
        response = model_call(model, False)
    assert response.content == "accepted" and len(calls) == 1


def test_legacy_checkpoint_resumed_by_lifecycle_binds_attempt_before_actual_model(db, monkeypatch):
    from app.models import Conversation
    from app.runtime_models import RunAttempt
    from app import run_lifecycle
    db.add(Conversation(id="legacy-policy-conversation")); db.commit()
    original = AgentState(question="hello", conversation_id="legacy-policy-conversation", phase="model",
        messages=[{"role": "user", "content": "hello"}],
        runtime_config={"mode": "api", "model_id": "synthetic-model"}).snapshot()
    assert original["attempt_id"] is None
    attempt, new = run_lifecycle.admit_attempt(db, "legacy-policy-run", "legacy-policy-conversation", "hello",
        checkpoint=original, resume=True, request_id="legacy-policy-resume")
    db.commit()
    assert new and db.query(RunAttempt).filter_by(run_id="legacy-policy-run").count() == 2
    from app.runtime_context import current_runtime_config
    sent_bindings = []
    model, calls = model_transport(monkeypatch, lambda: sent_bindings.append(current_runtime_config()))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    snapshots = []
    events = list(loop.run_agent(db, "hello", conversation_id="legacy-policy-conversation", run_id="legacy-policy-run",
        checkpoint=attempt.checkpoint, save_checkpoint=lambda row: snapshots.append(deepcopy(row))))
    assert calls and "answer" in [kind for kind, _event in events], events
    assert all(row["attempt_id"] == attempt.id for row in sent_bindings)
    assert snapshots and all(row["attempt_id"] == attempt.id for row in snapshots)
    assert all((row["runtime_config"] or {}).get("attempt_id") == attempt.id for row in snapshots)


@pytest.mark.parametrize("stream", [False, True])
def test_pre_dispatch_goal_rejection_releases_budget_even_after_call_binding(db, monkeypatch, stream):
    from app import resource_budget, request_resources
    from app.goal_models import Goal
    from app.goal_policy import GoalPolicyChanged
    from app.ledger_models import CallRecord
    goal = base(db); db.commit()
    budget = resource_budget.create_budget(owner_run_id="criteria-run", amount_limit="1", request_estimate="0.4", price_version="synthetic-policy-estimate")
    admitted = request_resources.request_admission
    @contextmanager
    def pause_after_queue(**kwargs):
        with admitted(**kwargs) as receipt:
            with SessionLocal() as other:
                other.get(Goal, goal["id"]).control_state = "paused"; other.commit()
            yield receipt
    monkeypatch.setattr(request_resources, "request_admission", pause_after_queue)
    model, calls = model_transport(monkeypatch)
    with bind_runtime_config({**bound(goal), "resource_budget_id": budget}):
        with pytest.raises(GoalPolicyChanged): model_call(model, stream)
    assert calls == []
    snapshot = resource_budget.budget_snapshot(budget)
    assert snapshot["reserved_amount"] == snapshot["spent_amount"] == "0"
    assert snapshot["reservations"][0]["state"] == "released"
    db.expire_all()
    assert [row.state for row in db.query(CallRecord)] == ["failed"]


def test_paused_parent_cannot_reuse_completed_child_with_empty_tool_evidence(db, monkeypatch):
    from app.goal_models import Goal
    goal = base(db); db.commit()
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [])
    monkeypatch.setattr(subagent, "assert_request_memory", lambda binding: None)
    calls = []
    class Model:
        def chat(self, messages, **kwargs):
            calls.append(1)
            return LLMResponse("Synthetic child observation, not completion proof.")
    with bind_runtime_config(bound(goal)):
        first = subagent.run_subagents(db, Model(), [{"topic": "empty evidence"}])
        db.get(Goal, goal["id"]).control_state = "paused"; db.commit()
        second = subagent.run_subagents(db, Model(), [{"topic": "empty evidence"}])
    assert first[0]["evidence"] == [] and len(calls) == 1
    assert second[0]["outcome"] == "blocked" and not second[0].get("reused")
