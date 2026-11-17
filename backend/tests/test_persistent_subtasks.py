from copy import deepcopy
import json

import pytest

from app import subtask_store
from app.agent import subagent
from app.llm import LLMResponse
from app.runtime_context import bind_runtime_config, current_runtime_config
from app.runtime_models import RuntimeRun


def persisted_parent(db, conversation_id, attempt_id):
    """Model-executing fixtures bind a real parent, not a missing run identity."""
    from app.models import Conversation
    from app.goal_ledger import ensure_run_goal
    if db.get(Conversation, conversation_id) is None:
        db.add(Conversation(id=conversation_id, title="Persistent parent")); db.flush()
    goal = ensure_run_goal(db, "parent", conversation_id, "Persisted child test")
    db.add(RuntimeRun(id="parent", kind="foreground", conversation_id=conversation_id,
        goal_id=goal["id"], current_attempt_id=attempt_id))
    db.commit()


def test_completed_child_reused_but_different_scope_is_separate(db, monkeypatch):
    persisted_parent(db, "persistent-parent", "attempt1")
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": "list_papers"}])
    calls = []
    class Model:
        def chat(self, messages, **kwargs):
            calls.append(current_runtime_config()["subtask_id"])
            return LLMResponse("已形成待父任务核验的归纳。")
    with bind_runtime_config({"run_id": "parent", "attempt_id": "attempt1", "model_id": "Model-A", "conversation_id": "persistent-parent"}):
        first = subagent.run_subagents(db, Model(), [{"topic": "A"}, {"topic": "A"}])
        second = subagent.run_subagents(db, Model(), [{"topic": "A"}])
    assert len(calls) == 1 and first[0]["subtask_id"] == first[1]["subtask_id"]
    assert second[0]["reused"] and not second[0]["summary_is_completion_proof"]
    with bind_runtime_config({"run_id": "parent", "attempt_id": "attempt1", "model_id": "Model-B", "conversation_id": "persistent-parent"}):
        third = subagent.run_subagents(db, Model(), [{"topic": "A"}])
    assert third[0]["subtask_id"] != first[0]["subtask_id"] and len(calls) == 2


def test_failed_child_resumes_with_completed_reads_and_preserves_scope(db, monkeypatch):
    from app.models import Conversation
    db.add(Conversation(id="conversation", title="Child scope")); db.commit()
    persisted_parent(db, "conversation", "attempt1")
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": "list_papers"}])
    reads = []
    monkeypatch.setattr(subagent, "execute_tool", lambda db, name, args, **kw: reads.append(kw) or '{"papers": []}')
    class FailAfterRead:
        def chat(self, messages, **kwargs):
            if any(m["role"] == "tool" for m in messages):
                raise RuntimeError("synthetic disconnect")
            return LLMResponse("", [{"id": "read", "name": "list_papers", "arguments": {}}])
    class Resume:
        def chat(self, messages, **kwargs):
            assert any(m["role"] == "tool" for m in messages)
            return LLMResponse("基于此前实际读取结果归纳。")
    with bind_runtime_config({"run_id": "parent", "attempt_id": "attempt1", "conversation_id": "conversation", "resource_budget_id": "shared-budget"}):
        first = subagent.run_subagents(db, FailAfterRead(), [{"topic": "A"}])
        second = subagent.run_subagents(db, Resume(), [{"topic": "A"}])
    assert len(reads) == 1 and reads[0]["conversation_id"] == "conversation"
    assert second[0]["attempt"] == 2 and first[0]["subtask_id"] == second[0]["subtask_id"]
    node = subtask_store.list_children("parent")[0]
    assert node["budget_id"] == "shared-budget" and node["state"] == "completed"


def test_new_parent_attempt_fences_old_child_writer_and_keeps_checkpoint(db):
    db.add(RuntimeRun(id="parent", kind="foreground", conversation_id=None, current_attempt_id="old"))
    db.commit()
    args = {"parent_run_id": "parent", "specification": {"topic": "A"}, "scope": {"revision": "fixed"}}
    old, reused = subtask_store.claim(**args, parent_attempt_id="old")
    saved = {**old["checkpoint"], "messages": [{"role": "tool", "content": "done read"}]}
    subtask_store.checkpoint(old["id"], old["writer_token"], saved)
    with pytest.raises(subtask_store.SubtaskConflict):
        subtask_store.claim(**args, parent_attempt_id="old")
    run = db.get(RuntimeRun, "parent")
    run.current_attempt_id = "new"
    db.commit()
    new, reused = subtask_store.claim(**args, parent_attempt_id="new")
    assert new["checkpoint"]["messages"] == saved["messages"] and new["attempt"] == 2
    with pytest.raises(subtask_store.SubtaskConflict):
        subtask_store.checkpoint(old["id"], old["writer_token"], saved)
    from app.resource_models import PersistentSubtaskAttempt
    attempts = db.query(PersistentSubtaskAttempt).order_by(PersistentSubtaskAttempt.number).all()
    assert [a.state for a in attempts] == ["interrupted", "running"]
    assert attempts[0].checkpoint["messages"] == saved["messages"]


def test_changed_project_file_invalidates_only_its_completed_child(db, monkeypatch, tmp_path):
    from app import workspace
    from app.models import Conversation
    root=tmp_path/'child-project';root.mkdir();(root/'paper.md').write_text('original',encoding='utf-8')
    project=workspace.register(db,str(root));db.add(Conversation(id='file-child',title='File child',project_id=project.id));db.commit()
    scope=workspace.capture(db,'file-child',project_id=project.id)
    persisted_parent(db, "file-child", "a")
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": "read_project_file"}])
    version, read_count = ["hash-a"], []
    monkeypatch.setattr(workspace, "read_text", lambda *a, **k: {"sha256": version[0]})
    monkeypatch.setattr(subagent, "execute_tool", lambda db, name, args, **kw:
        read_count.append(True) or json.dumps({"path": "paper.md", "sha256": version[0], "text": "original"}))
    class Model:
        def chat(self, messages, **kwargs):
            return LLMResponse("归纳") if any(m["role"] == "tool" for m in messages) else LLMResponse("", [
                {"id": "read", "name": "read_project_file", "arguments": {"path": "paper.md"}}])
    with bind_runtime_config({"run_id": "parent", "attempt_id": "a", "conversation_id":"file-child", "workspace":scope}):
        first = subagent.run_subagents(db, Model(), [{"topic": "A"}])[0]
        reused = subagent.run_subagents(db, Model(), [{"topic": "A"}])[0]
        assert reused["reused"] and len(read_count) == 1
        version[0] = "hash-b"
        revised = subagent.run_subagents(db, Model(), [{"topic": "A"}])[0]
    assert revised["subtask_id"] == first["subtask_id"] and revised["attempt"] == 2 and len(read_count) == 2


def test_child_evidence_propagates_exact_tool_quotes_without_promoting_summary():
    item = {"id": "e1", "paper_id": "p", "chunk_id": "c", "quote": "actual"}
    results = [{"findings": "unsupported summary", "evidence": [
        {"tool": "retrieve_evidence", "result": json.dumps({"evidence": [item]})},
        {"tool": "retrieve_evidence", "result": json.dumps({"evidence": [item]})}]}]
    assert subagent.collected_evidence(results) == [item]


def test_cancel_marker_is_persistent_and_does_not_cancel_completed_children():
    node, _ = subtask_store.claim(parent_run_id="parent", parent_attempt_id="a", specification={"topic": "A"}, scope={})
    subtask_store.request_cancel("parent")
    assert subtask_store.cancelled(node["id"])


def test_interrupted_external_call_is_not_blindly_replayed(db, monkeypatch):
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": "mcp__builtin__arxiv_search"}])
    monkeypatch.setattr(subagent, "execute_tool", lambda *a, **kw: pytest.fail("must reconcile before replay"))
    call = {"id": "c", "name": "mcp__builtin__arxiv_search", "arguments": {"query": "A"}}
    node, _ = subtask_store.claim(parent_run_id="parent", parent_attempt_id="a", specification={"topic": "A"}, scope={})
    node["checkpoint"] = {**node["checkpoint"], "calls": [call], "tool_index": 0, "pending_operation": deepcopy(call)}
    result = subagent.run_subagent(db, object(), {"topic": "A"}, node=node)
    assert result["outcome"] == "blocked" and "对账" in result["incomplete"][0]


def test_real_progress_continues_past_old_two_rounds(db, monkeypatch, tmp_path):
    from app import workspace
    from app.models import Conversation
    root=tmp_path/'pages-project';root.mkdir();(root/'p').write_text('page '*2000,encoding='utf-8')
    project=workspace.register(db,str(root));db.add(Conversation(id='pages-child',title='Pages child',project_id=project.id));db.commit()
    scope=workspace.capture(db,'pages-child',project_id=project.id)
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": "read_project_file"}])
    observed = []
    monkeypatch.setattr(subagent, "execute_tool", lambda db, name, args, **kw: observed.append(args["offset"]) or json.dumps({"text": str(args["offset"])}))
    class Pages:
        def chat(self, messages, tools=None, **kw):
            count = sum(m["role"] == "tool" for m in messages)
            return LLMResponse("完成读取") if count == 7 else LLMResponse("", [{"id": str(count), "name": "read_project_file", "arguments": {"path": "p", "offset": count * 100}}])
    with bind_runtime_config({"conversation_id":"pages-child","workspace":scope}):
        result = subagent.run_subagent(db, Pages(), {"topic": "read all"}, max_rounds=2)
    assert len(observed) == 7 and len(result["evidence"]) == 7
    assert result["summary_is_completion_proof"] is False


@pytest.mark.parametrize("tool,safe", [("read_project_file", True), ("mcp__builtin__arxiv_search", False)])
def test_parent_resume_proves_durable_child_scope_and_blocks_unknown_external_reads(db, monkeypatch, tool, safe):
    monkeypatch.setattr(subagent, "_safe_tool_specs", lambda: [{"name": tool}])
    binding = {"run_id": "parent", "model_id": "model", "resource_budget_id": "shared"}
    task = {"topic": "A"}
    scope = subtask_store.child_scope(db, binding)
    node, _ = subtask_store.claim(parent_run_id="parent", parent_attempt_id="a", specification=task,
                                scope=scope, budget_id="shared")
    call = {"id": "read", "name": tool, "arguments": {"path": "paper.md"}}
    subtask_store.checkpoint(node["id"], node["writer_token"], {**node["checkpoint"],
        "calls": [call], "tool_index": 0, "pending_operation": deepcopy(call)}, state="blocked")
    parent = {"runtime_config": binding, "pending_operation": {"key": "tool/1/0", "kind": "tool",
        "name": "spawn_subagent", "arguments": {"tasks": [task]}}}
    review = subtask_store.resume_safety(db, parent_run_id="parent", checkpoint=parent)
    assert review["safe"] is safe
    # Exercise the exact helpers wired into both foreground and background
    # admission. A safe local child must not demand the unknown-write dialog;
    # the MCP case must still retain that dialog and pending receipt.
    from app.agent.checkpoint import AgentState, recovery_info, prepare_resume
    state = AgentState(question="continue child", phase="tools", runtime_config=binding,
        calls=[{"id":"spawn", "name":"spawn_subagent", "arguments":{"tasks":[task]}}],
        pending_operation=parent["pending_operation"])
    info = recovery_info(state.snapshot())
    assert bool(info["automatic_child_recovery"]) is safe
    assert bool(info["pending_operation"]) is not safe
    resumed, _ = prepare_resume(state.snapshot())
    assert (resumed.resolution == "retry") is safe
    assert resumed.pending_operation == state.pending_operation
    altered = deepcopy(parent)
    altered["pending_operation"]["arguments"]["tasks"][0]["topic"] = "different"
    assert not subtask_store.resume_safety(db, parent_run_id="parent", checkpoint=altered)["safe"]
    wrong_budget = deepcopy(parent)
    wrong_budget["runtime_config"]["resource_budget_id"] = "another"
    assert not subtask_store.resume_safety(db, parent_run_id="parent", checkpoint=wrong_budget)["safe"]
    empty = deepcopy(parent)
    empty["pending_operation"]["arguments"]["tasks"] = []
    assert not subtask_store.resume_safety(db, parent_run_id="parent", checkpoint=empty)["safe"]
