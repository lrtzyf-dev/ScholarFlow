"""Explicit user criteria, isolated real DB/API, no provider or shell execution."""
from copy import deepcopy
import hashlib
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import goal_ledger as ledger, goal_contracts as contracts, goal_receipts, artifact_store
from app.goal_candidates import tool_constraint
from app.goal_models import GoalRequirement, GoalRevision
from app.runtime_models import RuntimeRun
from app.models import Conversation, ProjectCommand
from app.artifact_models import ArtifactRevision
from app.main import app
from app.timeutil import utcnow


def base(db):
    db.add(Conversation(id="criteria-a", title="Criteria")); db.flush()
    goal = ledger.ensure_run_goal(db, "criteria-run", "criteria-a", "请解释方法并保留局限。")
    db.add(RuntimeRun(id="criteria-run", kind="foreground", conversation_id="criteria-a", goal_id=goal["id"]))
    db.commit()
    return goal


def change(db, goal, spec=None, *, action="add", **extra):
    values = {"expected_revision": goal["revision"], "request_id": "criteria-" + uuid4().hex,
        "action": action, **extra}
    if action in ("add", "replace"):
        values.update(title=extra.get("title", "Explicit acceptance"), contract=spec or {"kind": "manual"})
    return contracts.user_change(db, goal["id"], **values)


def active(goal):
    return [r for r in goal["requirements"] if r["origin"] == "user" and r["status"] not in ("superseded", "cancelled")]


def test_candidate_promotion_edit_isolated_revision_and_manual_evidence(db):
    goal = base(db)
    candidate = goal["input_coverage"]["candidates"][0]
    goal = change(db, goal, candidate_id=candidate["candidate_id"])
    first = active(goal)[0]
    goal = change(db, goal, title="Another requirement")
    other = active(goal)[1]
    goal = change(db, goal, {"kind": "answer_terms", "contains": ["局限"], "excludes": []},
        action="replace", requirement_id=first["id"], title="New acceptance")
    assert next(r for r in goal["requirements"] if r["id"] == first["id"])["status"] == "superseded"
    assert next(r for r in goal["requirements"] if r["id"] == other["id"]) == other
    selected = next(r for r in active(goal) if r["title"] == "New acceptance")
    goal = change(db, goal, action="blocked", requirement_id=selected["id"], reason="缺少原始材料")
    assert active(goal)[-1]["evidence"][-1]["reference"] == "缺少原始材料"
    goal = change(db, goal, action="confirm", requirement_id=selected["id"], reason="已对照原件核对")
    assert next(r for r in active(goal) if r["id"] == selected["id"])["evidence"][-1]["semantic_verification"] == "user_confirmed"
    assert goal["revisions"][-1]["source"] == "user"


def test_idempotent_lost_response_and_stale_cas_never_overwrites(db):
    goal = base(db)
    values = {"expected_revision": goal["revision"], "request_id": "criteria-idempotent-1", "action": "add",
        "title": "First", "contract": {"kind": "manual"}}
    result = contracts.user_change(db, goal["id"], **values)
    assert contracts.user_change(db, goal["id"], **values)["revision"] == result["revision"]
    with pytest.raises(ledger.GoalConflict): contracts.user_change(db, goal["id"], **{**values, "title": "Different"})
    with pytest.raises(ledger.GoalConflict): change(db, goal)
    assert len(active(ledger.snapshot_goal(db, goal["id"]))) == 1


@pytest.mark.parametrize("spec", [
    {"kind": "answer_terms", "contains": [], "excludes": []},
    {"kind": "answer_terms", "contains": ["x"], "excludes": ["x"]},
    {"kind": "answer_terms", "contains": "not a list"},
    {"kind": "policy", "rule": "all_permissions"},
    {"kind": "command_result", "command": "echo x", "path": "../outside"},
    {"kind": "artifact_result", "min_fixed_sources": True},
    {"kind": "manual", "passed": True},
])
def test_invalid_criteria_are_not_registered(db, spec):
    goal = base(db)
    with pytest.raises(ledger.GoalError): change(db, goal, spec)
    assert ledger.snapshot_goal(db, goal["id"])["revision"] == goal["revision"]


@pytest.mark.parametrize("rule,blocked,allowed", [
    ("no_file_read", ["read_project_file", "mcp__builtin__web_fetch", "run_project_command"], ["update_plan"]),
    ("no_file_write", ["edit_project_file", "run_project_command", "mcp__other__unknown"], ["read_project_file"]),
    ("no_commands", ["run_project_command", "spawn_subagent", "git_project_write", "search_project_files", "unknown"], ["list_papers"]),
    ("no_external_tools", ["deepseek_search", "retrieve_evidence", "run_project_command", "mcp__builtin__arxiv_search"], ["read_attached_source"]),
])
def test_policy_fails_closed_until_explicit_user_cancel(db, rule, blocked, allowed):
    goal = change(db, base(db), {"kind": "policy", "rule": rule})
    selected = active(goal)[0]
    assert not selected["required"]
    for name in blocked: assert tool_constraint(goal, name)["requirement_id"] == selected["id"]
    for name in allowed: assert tool_constraint(goal, name) is None
    with pytest.raises(ledger.GoalError): change(db, goal, action="confirm", requirement_id=selected["id"], reason="Do not lift it this way")
    goal = change(db, goal, action="cancel", requirement_id=selected["id"])
    assert tool_constraint(goal, blocked[0]) is None


def command(db, **extra):
    row = ProjectCommand(project_id="synthetic-project", conversation_id="criteria-a", run_id="criteria-run",
        operation_key="test-operation", request_hash="a" * 64, scope={}, command="pytest tests/test_result.py",
        path="", cwd="C:/synthetic", status="succeeded", exit_code=0, output="3 passed", **extra)
    db.add(row); db.flush(); return row


def test_command_receipt_uses_db_identity_full_output_and_not_fake_model_result(db):
    goal = change(db, base(db), {"kind": "command_result", "command": "pytest tests/test_result.py", "path": "",
        "contains": ["3 passed"], "excludes": ["failed"]})
    row = command(db)
    ledger.record_contract_tool_result(db, goal["id"], "run_project_command", {},
        {"command_id": row.id, "output": "fake", "exit_code": 99}, "actual-operation")
    saved = ledger.snapshot_goal(db, goal["id"])
    req = active(saved)[0]
    assert req["status"] == "completed" and req["evidence"][-1]["semantic_verification"] == "not_assessed"
    assert req["evidence"][-1]["output_sha256"] == hashlib.sha256(b"3 passed").hexdigest()
    row.output, row.output_limited, row.revision = "3 passed", 1, row.revision + 1
    db.flush(); goal_receipts.evaluate(db, goal["id"], "Done")
    req = active(ledger.snapshot_goal(db, goal["id"]))[0]
    assert req["status"] == "open" and not req["evidence"][-1]["checks"]["complete_output"]


def test_zero_exit_different_command_or_other_run_cannot_pass(db):
    goal = change(db, base(db), {"kind": "command_result", "command": "pytest tests/test_result.py", "path": "",
        "contains": ["3 passed"]})
    row = command(db); row.command = "echo 3 passed"; db.flush()
    ledger.record_contract_tool_result(db, goal["id"], "run_project_command", {}, {"command_id": row.id}, "op")
    assert active(ledger.snapshot_goal(db, goal["id"]))[0]["status"] == "open"
    row.run_id = "another-run"; db.flush()
    assert goal_receipts.command_receipt(db, ledger._goal(db, goal["id"]), row.id,
        contracts.normalize({"kind": "command_result", "command": row.command})) is None


def test_answer_structural_checks_and_manual_unknown_remain_separate(db):
    goal = change(db, base(db), {"kind": "answer_terms", "contains": ["局限"], "excludes": ["绝对证明"]})
    goal = change(db, goal, title="解释证据是否充分")
    checked = ledger.evaluate_input_contracts(db, goal["id"], "局限：样本少。")
    assert active(checked)[0]["status"] == "completed" and active(checked)[1]["status"] == "open"
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"
    checked = ledger.evaluate_input_contracts(db, goal["id"], "绝对证明。")
    assert active(checked)[0]["status"] == "open"


def test_artifact_receipt_checks_fixed_manifest_instead_of_claimed_text(db):
    goal = change(db, base(db), {"kind": "artifact_result", "contains": ["局限"], "min_fixed_sources": 0})
    artifact = artifact_store.create(db, request_id="criteria-artifact-1", title="Result", kind="document",
        content="局限：样本少。", evidence=[], analysis={}, conversation_id="criteria-a")
    ledger.record_contract_tool_result(db, goal["id"], "save_research_artifact", {}, {
        "artifact_id": artifact["id"], "revision_id": artifact["revision"]["id"]}, "op")
    assert active(ledger.snapshot_goal(db, goal["id"]))[0]["status"] == "completed"
    db.execute(update(ArtifactRevision).where(ArtifactRevision.id == artifact["revision"]["id"]).values(content="tampered"))
    db.expire_all(); goal_receipts.evaluate(db, goal["id"], "Done")
    assert active(ledger.snapshot_goal(db, goal["id"]))[0]["status"] == "open"


def test_independent_command_requirements_do_not_overwrite_each_other_or_user_confirmation(db):
    goal = change(db, base(db), {"kind": "command_result", "command": "pytest tests/test_result.py", "contains": ["3 passed"]}, title="Command A")
    goal = change(db, goal, {"kind": "command_result", "command": "node --test", "contains": ["passed"]}, title="Command B")
    first = command(db)
    ledger.record_contract_tool_result(db, goal["id"], "run_project_command", {}, {"command_id": first.id}, "op-A")
    second = command(db); second.command, second.output = "node --test", "passed"; db.flush()
    ledger.record_contract_tool_result(db, goal["id"], "run_project_command", {}, {"command_id": second.id}, "op-B")
    saved = ledger.evaluate_input_contracts(db, goal["id"], "Done")
    assert [r["status"] for r in active(saved)] == ["completed", "completed"]
    saved = change(db, saved, action="reopen", requirement_id=active(saved)[0]["id"])
    saved = change(db, saved, action="confirm", requirement_id=active(saved)[0]["id"], reason="已按实际记录人工核对")
    first.output_limited = 1; db.flush()
    ledger.record_contract_tool_result(db, goal["id"], "run_project_command", {}, {"command_id": first.id}, "op-later")
    saved = ledger.evaluate_input_contracts(db, goal["id"], "Done")
    assert active(saved)[0]["status"] == "completed"
    assert active(saved)[0]["evidence"][-1]["kind"] == "user_confirmation"


def test_actual_artifact_tool_and_unaccepted_draft_and_unrelated_artifact(db):
    from app.artifact_tools import execute
    goal = change(db, base(db), {"kind": "artifact_result", "contains": ["局限"]})
    saved = execute(db, "save_research_artifact", {"request_id": "criteria-tool-artifact-1", "title": "Result",
        "kind": "document", "content": "局限：样本少。", "evidence": []}, conversation_id="criteria-a")
    ledger.record_contract_tool_result(db, goal["id"], "save_research_artifact", {}, saved, "op-A")
    assert active(ledger.snapshot_goal(db, goal["id"]))[0]["status"] == "completed"
    other = execute(db, "save_research_artifact", {"request_id": "criteria-tool-artifact-2", "title": "Other",
        "kind": "document", "content": "Unrelated content", "evidence": []}, conversation_id="criteria-a")
    ledger.record_contract_tool_result(db, goal["id"], "save_research_artifact", {}, other, "op-B")
    assert active(ledger.evaluate_input_contracts(db, goal["id"], "Done"))[0]["status"] == "completed"
    replacement = change(db, ledger.snapshot_goal(db, goal["id"]), {"kind": "artifact_result", "contains": ["NEW REQUIREMENT"]},
        action="replace", requirement_id=active(ledger.snapshot_goal(db, goal["id"]))[0]["id"])
    ledger.record_contract_tool_result(db, goal["id"], "propose_research_edit", {}, {
        "status": "draft_pending_review", "artifact_id": saved["artifact_id"], "revision_id": saved["revision_id"], "change_id": "synthetic"}, "op-C")
    assert active(ledger.evaluate_input_contracts(db, goal["id"], "Done"))[0]["status"] == "open"
    assert active(replacement)[0]["evidence"] == []


def test_constraint_cancellation_is_refreshed_even_with_old_orm_references(db):
    from app.db import SessionLocal
    goal = change(db, base(db), {"kind": "policy", "rule": "no_file_write"})
    old_goal = ledger._goal(db, goal["id"])
    old_rule = db.get(GoalRequirement, active(goal)[0]["id"])
    db.commit()
    # Load before the other session commits, preserving genuinely stale ORM
    # objects rather than lazily reloading their already-expired attributes.
    old_revision, rule_id = old_goal.revision, old_rule.id
    assert old_rule.status == "active_constraint"
    with SessionLocal() as other:
        updated = change(other, goal, action="cancel", requirement_id=rule_id)
        other.commit()
    assert old_goal.revision == old_revision < updated["revision"]
    current = ledger.snapshot_goal(db, goal["id"])
    assert old_rule.status == "cancelled" and tool_constraint(current, "edit_project_file") is None


@pytest.mark.parametrize("action,status", [("confirm", "completed"), ("blocked", "blocked")])
def test_legacy_structural_contract_respects_explicit_user_review(db, action, status):
    goal = base(db)
    goal = ledger.record_user_input(db, goal["id"], expected_revision=goal["revision"],
        source_message_id="legacy-table-user", content="请提供两张表格")
    requirement = next(r for r in goal["requirements"] if (r.get("tool_arguments") or {}).get("contract", {}).get("kind") == "answer_table_count")
    goal = change(db, goal, action=action, requirement_id=requirement["id"], reason="用户已核对本项当前状态")
    saved = ledger.evaluate_input_contracts(db, goal["id"], "No tables in this later response")
    assert next(r for r in saved["requirements"] if r["id"] == requirement["id"])["status"] == status


def test_history_goal_binding_is_scoped_and_legacy_history_still_loads(client, db):
    from app.models import QARecord
    db.add_all([QARecord(id="criteria-history-a", conversation_id="criteria-a", question="q", answer="a", runtime_config={"run_id": "criteria-run"}),
        QARecord(id="criteria-legacy-a", conversation_id="criteria-a", question="old", answer="old"),
        QARecord(id="criteria-copied-b", conversation_id="criteria-b", question="q", answer="a", runtime_config={"run_id": "criteria-run"})])
    db.commit()
    messages = {r["id"]: r for r in client.get("/api/conversations/criteria-a").json()["messages"]}
    assert messages["criteria-history-a"]["goal_id"]
    assert messages["criteria-legacy-a"]["goal_id"] is None
    assert client.get("/api/conversations/criteria-b").json()["messages"][0]["goal_id"] is None


@pytest.mark.parametrize("late_point", ["during_tool", "before_hook_action"])
def test_running_executor_observes_cross_session_policy_add_and_cancel(db, monkeypatch, tmp_path, late_point):
    import json
    from app import approval_store, run_lifecycle
    from app.db import SessionLocal
    from app.agent import loop
    from app.agent.checkpoint import AgentState
    from app.foreground_run_store import save_agent_checkpoint
    from app.models import ForegroundRun
    from app.llm import LLMResponse
    monkeypatch.setattr(approval_store, "_pending", {})
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    cid, run_id = "criteria-live", "chat_criteria_live"
    db.add(Conversation(id=cid, title="Live criteria")); db.flush()
    binding = {"version": 1, "model_id": "Synthetic", "mode": "mock", "approval_mode": "full_access", "policy_revision": 0}
    row = ForegroundRun(id=run_id, conversation_id=cid, question="整理信息", skills=[], images=[], status="running", runtime_config=binding)
    db.add(row)
    attempt, _ = run_lifecycle.admit_attempt(db, run_id, cid, row.question)
    state = AgentState(phase="model", question=row.question, conversation_id=cid, runtime_config=binding,
        messages=[{"role": "user", "content": row.question}])
    checkpoint = run_lifecycle.bind_checkpoint(state.snapshot(), attempt)
    row.agent_checkpoint = checkpoint; db.commit()
    calls, events = [], []
    target, hook_target = tmp_path / "action.txt", tmp_path / "hook.txt"
    added = False
    def amend(action):
        nonlocal added
        with SessionLocal() as other:
            latest = ledger.snapshot_goal(other, state.goal_id or db.get(RuntimeRun, run_id).goal_id)
            if action == "add":
                change(other, latest, {"kind": "policy", "rule": "no_file_write"}, title="不要修改用户文件")
                added = True
            else:
                criterion = next(r for r in latest["requirements"] if r["status"] == "active_constraint")
                change(other, latest, action="cancel", requirement_id=criterion["id"])
            other.commit()
    def save(snapshot):
        save_agent_checkpoint(db, run_id, snapshot)
        pending = snapshot.get("pending_operation") or {}
        if late_point == "before_hook_action" and not added and pending.get("kind") == "hook" and pending.get("name") == "PostToolUse":
            amend("add")
    def hooks(name, payload, **kwargs):
        if name == "PostToolUse" and payload.get("tool_name") == "list_papers": hook_target.write_text("forbidden")
        return {"ran": 1, "blocked": False, "feedback": ""}
    monkeypatch.setattr(loop.hooks_store, "run_hooks", hooks)
    class Model:
        count = 0
        def stream_chat(self, messages, tools=None):
            self.count += 1
            if self.count < 4:
                yield "final", LLMResponse("", [{"id": f"live-{self.count}", "name": "list_papers" if self.count == 1 else "mutate_file", "arguments": {}}])
            else: yield "final", LLMResponse("已按用户最新要求执行。")
    monkeypatch.setattr(loop, "get_llm", lambda: ModelInstance)
    ModelInstance = Model()
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": n, "parameters": {"type": "object", "properties": {}}} for n in ("list_papers", "mutate_file")])
    monkeypatch.setattr(loop, "tool_risk", lambda name: "safe")
    def execute(db, name, arguments, **kwargs):
        calls.append(name)
        if name == "list_papers" and late_point == "during_tool": amend("add")
        if name == "mutate_file": target.write_text("allowed after release")
        return json.dumps({"status": "completed", "items": []})
    monkeypatch.setattr(loop, "execute_tool", execute)
    for kind, payload in loop.run_agent(db, row.question, conversation_id=cid, run_id=run_id, checkpoint=checkpoint, save_checkpoint=save):
        events.append((kind, deepcopy(payload)))
        if kind == "policy_block" and payload["name"] == "mutate_file":
            assert not target.exists() and not hook_target.exists()
            amend("cancel")
    assert calls == ["list_papers", "mutate_file"]
    assert target.read_text() == "allowed after release" and not hook_target.exists()
    assert sum(kind == "policy_block" for kind, _ in events) == 1
    assert not any(kind == "approval_request" for kind, _ in events)


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    base(db)
    db.add(Conversation(id="criteria-b", title="Other")); db.commit()
    with TestClient(app) as value: yield value


def test_real_scoped_api_cas_archive_and_unsupported_research_kind(client, db):
    goal = ledger.ensure_run_goal(db, "criteria-run", "criteria-a", "请解释方法并保留局限。")
    url = f'/api/goal/{goal["id"]}/requirements'
    assert client.get(url, params={"conversation_id": "criteria-b"}).status_code == 404
    body = {"conversation_id": "criteria-a", "expected_revision": goal["revision"], "request_id": "criteria-http-1",
        "action": "add", "title": "人工核对", "contract": {"kind": "manual"}}
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json={**body, "request_id": "criteria-http-2"}).status_code == 409
    db.get(Conversation, "criteria-a").archived_at = utcnow(); db.commit()
    assert client.get(url, params={"conversation_id": "criteria-a"}).status_code == 200
    assert client.post(url, json=body).status_code == 409
    db.get(Conversation, "criteria-a").archived_at = None
    db.get(RuntimeRun, "criteria-run").kind = "framework"; db.commit()
    assert client.post(url, json=body).status_code == 409
