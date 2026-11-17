"""Independent side-effect uncertainty probes; no real provider or shell calls."""
import asyncio
from copy import deepcopy
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app import hooks_store
from app.agent import loop, tools
from app.agent.checkpoint import AgentState, CheckpointNeedsReview
from app.llm import LLMResponse
from app.models import Conversation
from app.runtime_context import bind_runtime_config


@pytest.fixture
def uncertain_mcp(monkeypatch):
    from app.mcp_bridge import MCPBridge, _Conn
    bridge = MCPBridge.__new__(MCPBridge)
    effects = []
    async def remote(name, arguments):
        effects.append(deepcopy(arguments))
        # The controlled remote operation happened, but its reply was lost.
        raise TimeoutError("synthetic reply loss after an externally visible effect")
    bridge._conns = {"independent": _Conn(config={"enabled": True}, client=SimpleNamespace(call_tool=remote))}
    monkeypatch.setattr(bridge, "_authorization_available", lambda conn: True)
    background = asyncio.new_event_loop()
    thread = threading.Thread(target=background.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bridge, "_ensure_loop", lambda: background)
    yield bridge, effects
    background.call_soon_threadsafe(background.stop)
    thread.join(timeout=5)
    background.close()


def test_dispatched_mcp_reply_loss_is_explicitly_unknown(uncertain_mcp):
    bridge, effects = uncertain_mcp
    with bind_runtime_config(None):
        response = json.loads(bridge.call_tool("mcp__independent__write_once", {"record": "only once"}))
    assert len(effects) == 1
    assert response.get("execution_unknown") is True
    assert response.get("automatic_retry") is False


@pytest.mark.parametrize("adapter", ["mcp_reply_loss", "generic_exception_after_effect"])
def test_new_model_tool_id_cannot_automatically_repeat_unknown_side_effect(db, monkeypatch, uncertain_mcp, adapter):
    bridge, remote_effects = uncertain_mcp
    name = "mcp__independent__write_once"
    db.add(Conversation(id="independent-effect", title="Disposable unknown operation")); db.commit()
    decisions, effects = [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            decisions.append(deepcopy(messages))
            if len(decisions) <= 2:
                yield "final", LLMResponse("", tool_calls=[{"id": "new-" + str(len(decisions)),
                    "name": name, "arguments": {"record": "only once"}}])
            else:
                yield "final", LLMResponse("实际写入结果未确认，需要核对。")
    def execute(*args, **kwargs):
        if adapter == "mcp_reply_loss":
            return bridge.call_tool(name, args[2])
        effects.append(deepcopy(args[2]))
        raise RuntimeError("synthetic local receipt failure after side effect")
    monkeypatch.setattr(loop.settings, "approval_mode", "full_access")
    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": name, "parameters": {"type": "object"}}])
    monkeypatch.setattr(tools, "tool_policy_error", lambda name: None)
    monkeypatch.setattr(loop, "execute_tool", execute)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    observed = []
    source = loop.run_agent(db, "只执行一次写入；结果未知时先核对，不能重复发送。", conversation_id="independent-effect")
    try:
        for kind, payload in source:
            if kind == "tool_result":
                observed.append(payload)
                if len(observed) >= 2:
                    break
    except CheckpointNeedsReview:
        pass
    finally:
        source.close()
    actual = remote_effects if adapter == "mcp_reply_loss" else effects
    assert len(actual) == 1, "A new model decision silently repeated an operation whose reply was lost"
    assert observed or len(decisions) == 1


def test_hook_timeout_cannot_be_reported_as_a_settled_nonblocking_hook(monkeypatch):
    effects = []
    def run(*args, **kwargs):
        effects.append("synthetic externally visible effect")
        raise subprocess.TimeoutExpired("independent hook", .01)
    monkeypatch.setattr(hooks_store.subprocess, "run", run)
    monkeypatch.setattr(hooks_store, "load_hooks", lambda: {"PreToolUse": [{"hooks": [{"command": "independent hook"}]}]})
    with bind_runtime_config(None):
        result = hooks_store.run_hooks("PreToolUse", {}, timeout=.01)
    assert len(effects) == 1
    assert result.get("execution_unknown") is True
    assert result.get("blocked") is True
    assert result["results"][0].get("execution_unknown") is True


def test_timeout_before_loop_dispatch_cannot_start_a_late_mcp_write(uncertain_mcp, monkeypatch):
    bridge, effects = uncertain_mcp
    delayed = []
    def timeout_before_loop(coroutine, **kwargs):
        delayed.append(coroutine)
        raise TimeoutError("synthetic scheduling timeout")
    monkeypatch.setattr(bridge, "_run", timeout_before_loop)
    with bind_runtime_config(None):
        response = json.loads(bridge.call_tool("mcp__independent__write_once", {"record": "only once"}))
        late = json.loads(asyncio.run(delayed.pop()))
    assert response["not_executed"] is True and not response["execution_unknown"]
    assert late["not_executed"] is True and effects == []


def test_builtin_read_failure_remains_legally_retryable(uncertain_mcp):
    bridge, reads = uncertain_mcp
    bridge._conns["builtin"] = bridge._conns.pop("independent")
    with bind_runtime_config(None):
        for _ in range(2):
            response = json.loads(bridge.call_tool("mcp__builtin__crossref_search", {"query": "read only"}))
            assert response["execution_unknown"] is False and response["automatic_retry"] is True
    assert len(reads) == 2


def test_custom_mcp_error_result_does_not_claim_its_partial_writes_were_rolled_back(uncertain_mcp):
    bridge, effects = uncertain_mcp
    async def remote(name, arguments):
        effects.append("partial effect")
        return SimpleNamespace(content=[SimpleNamespace(text="Remote operation failed after its first step")], is_error=True)
    bridge._conns["independent"].client = SimpleNamespace(call_tool=remote)
    with bind_runtime_config(None):
        response = json.loads(bridge.call_tool("mcp__independent__write_once", {}))
    assert effects == ["partial effect"]
    assert response["error"] and response["execution_unknown"] is True and response["automatic_retry"] is False


def setup_repeated_model(db, monkeypatch, *, name, execute, arguments=None):
    cid = "independent-repeat"
    db.add(Conversation(id=cid, title="Disposable repeated operation")); db.commit()
    decisions = []
    class Model:
        def stream_chat(self, messages, tools=None):
            decisions.append(deepcopy(messages))
            if len(decisions) <= 2:
                yield "final", LLMResponse("", tool_calls=[{"id": "distinct-" + str(len(decisions)),
                    "name": name, "arguments": arguments or {"query": "same target"}}])
            else:
                yield "final", LLMResponse("保留已有结果。")
    monkeypatch.setattr(loop.settings, "approval_mode", "full_access")
    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": name, "parameters": {"type": "object"}}])
    monkeypatch.setattr(tools, "tool_policy_error", lambda name: None)
    monkeypatch.setattr(loop, "execute_tool", execute)
    return cid, decisions


def take_results(source, count=2):
    results = []
    try:
        for kind, payload in source:
            if kind == "tool_result":
                results.append(payload)
                if len(results) == count:
                    break
    finally:
        source.close()
    return results


@pytest.mark.parametrize("kind", ["safe_read", "known_not_dispatched"])
def test_safe_reads_and_proven_predispatch_failures_are_not_poisoned_as_unknown(db, monkeypatch, kind):
    attempts = []
    class Undispatched(Exception):
        tool_request_not_dispatched = True
    def execute(*args, **kwargs):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise (RuntimeError("synthetic read failure") if kind == "safe_read" else Undispatched())
        return '{"status":"completed"}'
    name = "retrieve_evidence" if kind == "safe_read" else "mcp__independent__write_once"
    cid, _ = setup_repeated_model(db, monkeypatch, name=name, execute=execute)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    results = take_results(loop.run_agent(db, "先尝试当前操作，明确未执行或只读失败时允许修正。", conversation_id=cid))
    assert len(attempts) == 2
    assert all(not json.loads(result["preview"]).get("execution_unknown") for result in results)


def test_unknown_guard_survives_saved_checkpoint_and_new_model_decision(db, monkeypatch):
    effects, snapshots = [], []
    def execute(*args, **kwargs):
        effects.append("effect")
        raise RuntimeError("synthetic receipt loss")
    cid, _ = setup_repeated_model(db, monkeypatch, name="mcp__independent__write_once", execute=execute)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    question = "只写入一次，重启后仍先核对未知结果。"
    persist = lambda snapshot: snapshots.append(deepcopy(snapshot))
    first = take_results(loop.run_agent(db, question, conversation_id=cid, save_checkpoint=persist), 1)
    assert len(first) == 1 and len(effects) == 1
    restored = deepcopy(snapshots[-1])
    assert any(item.get("execution_unknown") for item in restored["memory_side_effects"].values())
    second = take_results(loop.run_agent(db, question, conversation_id=cid, checkpoint=restored, save_checkpoint=persist), 1)
    assert len(effects) == 1
    assert json.loads(second[0]["preview"])["not_replayed"] is True


def test_managed_hook_unknown_keeps_pending_and_skip_does_not_reexecute_it_under_new_tool_id(db, monkeypatch):
    effects, snapshots, tool_effects = [], [], []
    def run(*args, **kwargs):
        effects.append("hook effect")
        raise subprocess.TimeoutExpired("independent hook", .01)
    cid, _ = setup_repeated_model(db, monkeypatch, name="mcp__independent__write_once",
        execute=lambda *a, **k: tool_effects.append("tool effect") or '{"status":"completed"}')
    monkeypatch.setattr(hooks_store.subprocess, "run", run)
    monkeypatch.setattr(hooks_store, "load_hooks", lambda: {"PreToolUse": [{"hooks": [{"command": "independent hook"}]}]})
    persist = lambda snapshot: snapshots.append(deepcopy(snapshot))
    question = "先核对钩子，不能重发未知写入。"
    with pytest.raises(CheckpointNeedsReview):
        list(loop.run_agent(db, question, conversation_id=cid, save_checkpoint=persist))
    assert effects == ["hook effect"] and tool_effects == []
    restored = deepcopy(snapshots[-1])
    assert restored["pending_operation"]["kind"] == "hook"
    with pytest.raises(CheckpointNeedsReview):
        list(loop.run_agent(db, question, conversation_id=cid, checkpoint=restored, save_checkpoint=persist))
    assert effects == ["hook effect"]
    restored["resolution"] = "skip"
    with pytest.raises(CheckpointNeedsReview):
        list(loop.run_agent(db, question, conversation_id=cid, checkpoint=restored, save_checkpoint=persist))
    assert effects == ["hook effect"] and tool_effects == ["tool effect"]


@pytest.mark.parametrize("failure", ["timeout", "process_crash"])
def test_hook_event_retry_reuses_saved_successful_commands_before_the_uncertain_one(db, monkeypatch, failure):
    effects, snapshots = [], []
    class SyntheticPowerLoss(BaseException):
        pass
    def run(command, **kwargs):
        effects.append(command)
        if command == "second hook" and effects.count(command) == 1:
            if failure == "process_crash":
                raise SyntheticPowerLoss()
            raise subprocess.TimeoutExpired(command, .01)
        return subprocess.CompletedProcess(command, 0, "completed", "")
    cid, _ = setup_repeated_model(db, monkeypatch, name="mcp__independent__write_once", execute=lambda *a, **k: '{"status":"completed"}')
    monkeypatch.setattr(hooks_store.subprocess, "run", run)
    monkeypatch.setattr(hooks_store, "load_hooks", lambda: {"PreToolUse": [{"hooks": [{"command": "first hook"}, {"command": "second hook"}]}]})
    question = "核对后只重试未确认的钩子，不要重复已经成功的命令。"
    persist = lambda snapshot: snapshots.append(deepcopy(snapshot))
    with pytest.raises(CheckpointNeedsReview if failure == "timeout" else SyntheticPowerLoss):
        list(loop.run_agent(db, question, conversation_id=cid, save_checkpoint=persist))
    assert effects == ["first hook", "second hook"]
    restored = deepcopy(snapshots[-1])
    restored["resolution"] = "retry"
    assert take_results(loop.run_agent(db, question, conversation_id=cid, checkpoint=restored, save_checkpoint=persist), 1)
    assert effects == ["first hook", "second hook", "second hook"], "Retrying one uncertain hook repeated a known successful command"


def test_unknown_later_hook_blocks_entire_new_event_before_an_earlier_command_is_repeated(db, monkeypatch):
    effects, snapshots, tool_effects = [], [], []
    def run(command, **kwargs):
        effects.append(command)
        if command == "second hook":
            raise subprocess.TimeoutExpired(command, .01)
        return subprocess.CompletedProcess(command, 0, "completed", "")
    cid, _ = setup_repeated_model(db, monkeypatch, name="mcp__independent__write_once", execute=lambda *a, **k: tool_effects.append("tool") or '{"status":"completed"}')
    monkeypatch.setattr(hooks_store.subprocess, "run", run)
    monkeypatch.setattr(hooks_store, "load_hooks", lambda: {"PreToolUse": [{"hooks": [{"command": "first hook"}, {"command": "second hook"}]}]})
    persist = lambda snapshot: snapshots.append(deepcopy(snapshot))
    question = "只跳过未确认事件，不要用新事件编号重发该批钩子。"
    with pytest.raises(CheckpointNeedsReview):
        list(loop.run_agent(db, question, conversation_id=cid, save_checkpoint=persist))
    restored = deepcopy(snapshots[-1]); restored["resolution"] = "skip"
    with pytest.raises(CheckpointNeedsReview):
        list(loop.run_agent(db, question, conversation_id=cid, checkpoint=restored, save_checkpoint=persist))
    assert effects == ["first hook", "second hook"] and tool_effects == ["tool"]


def test_real_local_import_record_after_synthetic_download_failure_is_not_created_again_under_new_call_id(db, monkeypatch):
    from app.pipeline import importer
    from app.models import Paper
    actual_execute = tools.execute_tool
    downloads = []
    class DownloadFailure:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url):
            downloads.append(url)
            raise TimeoutError("synthetic download failure after the local import record was committed")
    cid, _ = setup_repeated_model(db, monkeypatch, name="import_paper_from_url",
        execute=actual_execute, arguments={"pdf_url": "https://example.test/disposable.pdf", "title": "Disposable import record"})
    monkeypatch.setattr(importer, "assert_public_url", lambda url: None)
    monkeypatch.setattr(importer.httpx, "Client", DownloadFailure)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    results = take_results(loop.run_agent(db, "入库这个指定PDF，失败时保留原记录检查，不要重新新建。", conversation_id=cid))
    first, blocked = [json.loads(item["preview"]) for item in results]
    assert len(downloads) == 1 and db.query(Paper).count() == 1
    assert first["execution_unknown"] is True and first["paper_id"] == db.query(Paper).one().id
    assert blocked["not_replayed"] is True and blocked["execution_unknown"] is True


def test_hook_checks_cancellation_before_each_command_and_recovery_keeps_first_receipt(db, monkeypatch):
    effects, snapshots, cancelled = [], [], [False]
    def run(command, **kwargs):
        effects.append(command)
        cancelled[0] = command == "first hook"
        return subprocess.CompletedProcess(command, 0, "completed", "")
    cid, _ = setup_repeated_model(db, monkeypatch, name="mcp__independent__write_once", execute=lambda *a, **k: '{"status":"completed"}')
    monkeypatch.setattr(hooks_store.subprocess, "run", run)
    monkeypatch.setattr(hooks_store, "load_hooks", lambda: {"PreToolUse": [{"hooks": [{"command": "first hook"}, {"command": "second hook"}]}]})
    monkeypatch.setattr(loop.cancel_store, "is_cancelled", lambda run_id: cancelled[0])
    persist = lambda snapshot: snapshots.append(deepcopy(snapshot))
    question = "执行钩子时取消后停止后续命令。"
    events = list(loop.run_agent(db, question, conversation_id=cid, save_checkpoint=persist))
    assert any(kind == "cancelled" for kind, _ in events)
    assert effects == ["first hook"]
    cancelled[0] = False
    restored = deepcopy(snapshots[-1]); restored["resolution"] = "retry"
    assert take_results(loop.run_agent(db, question, conversation_id=cid, checkpoint=restored, save_checkpoint=persist), 1)
    assert effects == ["first hook", "second hook"]


@pytest.mark.parametrize("status", ["interrupted", "timed_out", "failed", "cancelled", "output_limit"])
def test_started_command_uncertainty_uses_existing_journal_and_blocks_automatic_new_step(db, monkeypatch, tmp_path, status):
    from app import project_commands as commands, workspace
    from app.timeutil import utcnow
    cid, _ = setup_repeated_model(db, monkeypatch, name="run_project_command", execute=lambda *a, **k: pytest.fail("command bypassed its adapter"),
        arguments={"command": "Write-Output 'synthetic command, never spawned'", "summary": "Disposable command result"})
    folder = tmp_path / "project"; folder.mkdir()
    project = workspace.register(db, str(folder))
    db.get(Conversation, cid).project_id = project.id; db.commit()
    scope = workspace.project_scope(project)
    scope.update(files=[], rules=[])
    effects = []
    def stopped(command_id, *, run_id):
        effects.append(command_id)
        with commands.SessionLocal() as other:
            row = commands.get(other, command_id)
            commands._set(other, row, status="running", started_at=utcnow())
        return commands._finish(command_id, status, "synthetic stopped process after a possible effect", "partial output", None, False)
    monkeypatch.setattr(commands, "run", stopped)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    snapshots = []
    results = take_results(loop.run_agent(db, "执行当前命令，结果未知先核对。", conversation_id=cid, workspace=scope,
        save_checkpoint=lambda value: snapshots.append(deepcopy(value))))
    assert len(effects) == 1
    outcome = snapshots[-1]["operations"][results[0]["operation_key"]]
    assert json.loads(outcome["result"])["execution_unknown"] is True
    assert json.loads(results[1]["preview"])["not_replayed"] is True


@pytest.mark.parametrize("name", ["create_project_worktree", "remove_project_worktree", "archive_project_worktree",
    "restore_project_worktree", "reconcile_project_worktree"])
def test_existing_interrupted_worktree_receipt_blocks_new_model_id(db, monkeypatch, name):
    attempts = []
    def adapter(*args, **kwargs):
        attempts.append("existing worktree adapter")
        return '{"id":"controlled-worktree","status":"interrupted","error":"actual state needs reconciliation"}'
    cid, _ = setup_repeated_model(db, monkeypatch, name=name, execute=adapter)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    results = take_results(loop.run_agent(db, "核对工作树实际状态，不重发未知变更。", conversation_id=cid))
    assert len(attempts) == 1
    assert json.loads(results[0]["preview"])["execution_unknown"] is True
    assert json.loads(results[1]["preview"])["not_replayed"] is True


def test_single_use_human_retry_is_committed_before_real_adapter_entry_and_not_reissued_after_reply_loss(db, monkeypatch):
    effects, snapshots = [], []
    name = "mcp__independent__write_once"
    def adapter(*args, **kwargs):
        assert snapshots[-1]["pending_operation"]
        assert snapshots[-1]["tool_failures"][0]["retry_consumptions"], "Retry permit was not committed before dispatch"
        effects.append("one new dispatch")
        raise RuntimeError("synthetic reply loss after explicit one-time retry")
    cid, _ = setup_repeated_model(db, monkeypatch, name=name, execute=adapter)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    sig = __import__("app.operation_receipts", fromlist=["signature"]).signature(name, {"query": "same target"})
    base = AgentState(question="核对原操作后重试一次。", conversation_id=cid, phase="model",
        memory_side_effects={sig: {"operation_key": "tool/old/0", "execution_unknown": True, "user_input_count": 0}},
        tool_failures=[{"id": "failure/tool/old/0", "operation_key": "tool/old/0", "tool_name": name,
            "arguments": {"query": "same target"}, "signature": sig, "execution_unknown": True, "phase": "write",
            "status": "open", "error": "old reply loss", "reconciliations": [{"id": "human-reviewed-old",
                "attempt_id": None, "action": "not_executed", "provider_verified": False}]}])
    # Initialize with the normal setup first, then graft the earlier returned
    # unknown receipt into this controlled checkpoint. No fake transport result
    # or goal success is introduced.
    from app.model_profiles import capture_run_config
    base.runtime_config = capture_run_config(db, cid)
    db.commit()
    from app.conversation_memory import load_history
    history, info = load_history(db, cid, None)
    base.messages = [{"role": "system", "content": "controlled checkpoint"},
        {"role": "user", "content": base.question, "_task_root": True, "_memory_fingerprint": info["fingerprint"]}]
    base.memory_fingerprint = info["fingerprint"]
    results = take_results(loop.run_agent(db, base.question, conversation_id=cid, checkpoint=base.snapshot(),
        save_checkpoint=lambda value: snapshots.append(deepcopy(value))))
    assert effects == ["one new dispatch"] and len(results) == 2
    assert json.loads(results[1]["preview"])["not_replayed"]
