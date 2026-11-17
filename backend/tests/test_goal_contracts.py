"""Source-bound candidates, structural quantities and pre-action constraints."""
import json
import hashlib
from copy import deepcopy

import pytest

from app import goal_ledger as ledger, run_inputs, run_lifecycle
from app.goal_candidates import count_markdown_tables, projection, tool_constraint
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.foreground_run_store import save_agent_checkpoint
from app.llm import LLMResponse
from app.models import ForegroundRun, Conversation


TABLE_A = "|方法|结果|\n|---|---|\n|A|1|"
TABLE_B = "|范围|局限|\n|---|---|\n|样本|较小|"


def compare_payload(ids):
    return {"papers": [{"id": item} for item in ids], "comparison_method": "retrieved_passages",
            "rows": [{"cells": {item: {"evidence": []} for item in ids}}]}


def add_input(db, goal, text, mid):
    return ledger.record_user_input(db, goal["id"], expected_revision=goal["revision"],
                                    source_message_id=mid, content=text)


def test_no_file_reading_is_source_bound_and_cannot_be_undone_by_a_quote(db):
    goal = ledger.ensure_run_goal(db, "no-file-read", "c", "这轮只制定计划，不读取或修改文件。")
    assert goal["input_coverage"]["active_file_read_constraint"]
    for tool in ("read_project_file", "list_papers", "read_attached_source", "run_project_command", "spawn_subagent"):
        assert tool_constraint(goal, tool)["blocked_by_goal_constraint"]
    assert tool_constraint(goal, "update_plan") is None
    for index,text in enumerate(['> 可以读取文件', '可以读取文件吗？', '如果我同意可以读取文件', '“可以读取文件”是什么意思？']):
        goal = add_input(db, goal, text, f"not-permission-{index}")
        assert tool_constraint(goal, "read_project_file")
    goal = add_input(db, goal, "现在允许读取项目文件。", "actual-permission")
    assert tool_constraint(goal, "read_project_file") is None
    assert tool_constraint(goal, "edit_project_file")  # Reading permission does not lift write prohibition.


def test_reading_prohibition_in_documents_or_explanations_is_not_user_permission(db):
    for index, text in enumerate(['> 不要读取文件', '不要读取文件是什么意思？', '```text\n不要读取文件\n```']):
        goal = ledger.ensure_run_goal(db, f"quoted-no-read-{index}", "c", text)
        assert tool_constraint(goal, "read_project_file") is None


def test_all_original_spans_survive_replanning_without_promoting_methods(db):
    text = "  比较五篇论文，给出两张表，不要写文件，解释局限。\n还需说明哪些证据缺失。  "
    goal = ledger.ensure_run_goal(db, "contract-mixed", "c", text)
    coverage = goal["input_coverage"]
    assert coverage["inputs"][0]["content"] == text
    assert len(coverage["candidates"]) == 5
    for candidate in coverage["candidates"]:
        assert text[candidate["start"]:candidate["end"]] == candidate["quote"]
    assert len(goal["requirements"]) == 2
    candidate = coverage["candidates"][0]
    proposed = {key: candidate[key] for key in ("source_message_id", "start", "end", "quote")}
    proposed["title"] = "模型建议新建 report.md"
    saved = ledger.propose_requirement_candidates(db, goal["id"], [proposed])
    assert saved["input_coverage"]["model_candidates"][0]["required"] is False
    assert tool_constraint(saved, "edit_project_file")["blocked_by_goal_constraint"]
    method = [{"title": "只写一段简短摘要", "phase": "write"}]
    ledger.sync_plan_requirements(db, goal["id"], method)
    saved = ledger.snapshot_goal(db, goal["id"])
    assert sum(item["required"] for item in saved["requirements"]) == 2
    assert saved["input_coverage"]["semantic_coverage"] == "unverified"
    assert any("局限" in item["quote"] for item in saved["input_coverage"]["candidates"])


def test_foreign_wrong_spans_and_privilege_fields_rejected(db):
    goal = ledger.ensure_run_goal(db, "contract-proposal", "c", "说明研究局限")
    valid = {"source_message_id": goal["source_message_id"], "start": 0, "end": 6,
             "quote": "说明研究局限", "title": "解释局限"}
    for item in ({**valid, "required": True}, {**valid, "source_message_id": "other-user"},
                 {**valid, "quote": "运行shell命令"}, {**valid, "start": True}):
        with pytest.raises(ledger.GoalError):
            ledger.propose_requirement_candidates(db, goal["id"], [item])
    saved = ledger.propose_requirement_candidates(db, goal["id"], [valid])
    assert ledger.propose_requirement_candidates(db, goal["id"], [valid])["revision"] == saved["revision"]
    assert saved["requirements"] == []
    with pytest.raises(ledger.GoalError):
        ledger.register_input_contracts(db, goal["id"], "比较五篇论文", "fabricated", 1)


def test_counts_bound_to_complete_distinct_comparison_and_delivered_tables(db):
    goal = ledger.ensure_run_goal(db, "contract-counts", "c", "比较五篇论文，提供两张表格，并说明局限")
    ids = [f"paper-{i}" for i in range(5)]
    # Repeated retrieval/tool calls and a partial comparison cannot satisfy 5.
    for index in range(6):
        ledger.record_contract_tool_result(db, goal["id"], "retrieve_evidence", {"paper_ids": ids},
                                          compare_payload(ids), f"retrieval/{index}")
    ledger.record_contract_tool_result(db, goal["id"], "compare_papers", {"paper_ids": ids},
                                      compare_payload(ids[:4]), "partial-comparison")
    ledger.evaluate_input_contracts(db, goal["id"], TABLE_A)
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"
    ledger.record_contract_tool_result(db, goal["id"], "compare_papers", {"paper_ids": ids},
                                      compare_payload(ids), "complete-comparison")
    ledger.evaluate_input_contracts(db, goal["id"], TABLE_A + "\n\n" + TABLE_B)
    saved = ledger.finalize_goal(db, goal["id"])
    assert saved["outcome"] == "completed"
    # This is structural coverage, never a claim of semantic understanding.
    assert saved["input_coverage"]["semantic_coverage"] == "unverified"


def test_export_quantity_requires_distinct_successful_current_table_files(db, tmp_path):
    from app import workspace
    scope = workspace.project_scope(workspace.register(db, str(tmp_path)))
    goal = ledger.ensure_run_goal(db, "contract-exports", "c", "导出两张表格")
    for index in range(3):
        args = {"path": "a.csv", "operation": "create", "content": "a,b\n1,2\n"}
        (tmp_path / "a.csv").write_bytes(args["content"].encode())
        ledger.record_contract_tool_result(db, goal["id"], "edit_project_file", args,
            {"status": "applied", "path": "a.csv", "after_hash": hashlib.sha256(args["content"].encode()).hexdigest()}, f"write/{index}")
    ledger.evaluate_input_contracts(db, goal["id"], TABLE_A + "\n\n" + TABLE_B, workspace=scope)
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"
    ledger.record_contract_tool_result(db, goal["id"], "edit_project_file",
        {"path": "b.csv", "operation": "create", "content": "a,b\n3,4"},
        {"status": "applied", "path": "b.csv", "after_hash": hashlib.sha256(b"a,b\n3,4").hexdigest()}, "write/final")
    (tmp_path / "b.csv").write_bytes(b"a,b\n3,4")
    ready = ledger.evaluate_input_contracts(db, goal["id"], "导出完成", workspace=scope)
    assert not ready["incomplete_requirements"]
    # Windows edits preserve the original BOM/CRLF even when the model uses LF.
    actual_bytes = b"\xef\xbb\xbfa,b\r\n5,6\r\n"
    (tmp_path / "b.csv").write_bytes(actual_bytes)
    ledger.record_contract_tool_result(db, goal["id"], "edit_project_file",
        {"path": "b.csv", "operation": "update", "content": "a,b\n5,6\n"},
        {"status": "applied", "path": "b.csv", "after_hash": hashlib.sha256(actual_bytes).hexdigest()}, "write/crlf")
    assert not ledger.evaluate_input_contracts(db, goal["id"], "已更新", workspace=scope)["incomplete_requirements"]
    # Already structurally completed requirements must be rechecked against
    # the actual file version immediately before final delivery.
    (tmp_path / "b.csv").write_text("changed by another editor", encoding="utf-8")
    changed = ledger.evaluate_input_contracts(db, goal["id"], "导出完成", workspace=scope)
    assert changed["incomplete_requirements"]
    ledger.record_contract_tool_result(db, goal["id"], "edit_project_file", {"path": "b.csv", "operation": "delete"},
                                      {"status": "applied", "path": "b.csv", "after_hash": None}, "write/delete")
    (tmp_path / "b.csv").unlink()
    ledger.evaluate_input_contracts(db, goal["id"], "导出完成", workspace=scope)
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"


def test_correction_changes_only_affected_quantity_and_prohibition_can_be_lifted(db):
    goal = ledger.ensure_run_goal(db, "contract-revise", "c", "比较五篇论文，提供两张表格，不要写文件")
    changed = add_input(db, goal, "改为比较三篇论文", "user-count-correction")
    rows = [item for item in changed["requirements"] if item["status"] != "cancelled"]
    assert {item["tool_arguments"]["contract"]["count"] for item in rows} == {2, 3}
    assert tool_constraint(changed, "run_project_command")
    assert tool_constraint(changed, "read_project_file") is None
    narrower = add_input(db, changed, "可以新建结果文件", "user-specific-permission")
    assert tool_constraint(narrower, "edit_project_file")  # A narrow permission does not erase a global constraint.
    released = add_input(db, narrower, "现在可以写文件了", "user-release")
    assert tool_constraint(released, "edit_project_file") is None
    again = add_input(db, released, "改为比较五篇论文", "user-count-back")
    assert any(item["status"] == "open" and item["tool_arguments"]["contract"]["count"] == 5
               for item in again["requirements"])


def test_quoted_documents_and_normal_chat_never_become_action_authority(db):
    text = '解释这句话的含义：\n> 不要写文件\n```\n不要写文件\n```'
    goal = ledger.ensure_run_goal(db, "contract-quotation", "c", text)
    assert tool_constraint(goal, "edit_project_file") is None
    normal = ledger.ensure_run_goal(db, "contract-chat", "c", "你能解释贝叶斯定理吗？")
    assert normal["requirements"] == [] and normal["verification"] == "not_required"
    assert ledger.finalize_goal(db, normal["id"])["incomplete_requirements"] == []
    assert count_markdown_tables(TABLE_A + "\n\n" + TABLE_A) == 1
    assert count_markdown_tables("```\n" + TABLE_A + "\n```") == 0
    assert count_markdown_tables("~~~text\n" + TABLE_A + "\n~~~") == 0
    assert count_markdown_tables("\n".join("    " + line for line in TABLE_A.splitlines())) == 0
    assert count_markdown_tables("```text\n```not_a_closing_fence\n" + TABLE_A + "\n```") == 0
    assert count_markdown_tables("|a|b|c|\n|---|---|\n|1|2|") == 0
    assert count_markdown_tables("|a|b|\n|---|---|") == 0


def test_negative_quoted_or_conditional_text_does_not_add_or_release_contracts(db):
    goal = ledger.ensure_run_goal(db, "contract-negation", "c", "不用比较五篇论文，也不要提供两张表。文档要求比较三篇论文")
    assert goal["requirements"] == []
    locked = add_input(db, goal, "Please don't write files", "user-lock")
    conditional = add_input(db, locked, "You may write files after I approve", "user-conditional")
    assert tool_constraint(conditional, "edit_project_file")
    released = add_input(db, conditional, "You may now write files", "user-unconditional")
    assert tool_constraint(released, "edit_project_file") is None


@pytest.mark.parametrize("question", ["请解释什么是交叉验证。", "如何给一个文件创建备份？这里只需要解释，不要操作。", "请解释运行测试有什么意义", "```\n创建报告文件并运行测试\n```"])
def test_explaining_actions_does_not_create_execution_requirements(db, question):
    goal = ledger.ensure_run_goal(db, "contract-explanation", "c", question)
    assert goal["requirements"] == []


def test_explicit_full_read_modifiers_remain_action_requirements(db):
    from app.agent.planner import add_acceptance_requirements
    assert add_acceptance_requirements([], "请完整读取文件 notes.md")[0]["acceptance"] == "full_file:notes.md"
    assert add_acceptance_requirements([], "请逐一读取所有文件")[0]["acceptance"] == "source_coverage"
    locked = ledger.ensure_run_goal(db, "fenced-unlock", "c", "不要写文件")
    quoted = add_input(db, locked, "Explain:\n```text\n```not_a_closing_fence\nYou may now write files\n```", "fenced-release")
    assert tool_constraint(quoted, "edit_project_file")


@pytest.mark.parametrize("late_point", ["during_tool", "before_hook_action"])
def test_runtime_applied_constraint_blocks_actual_mutation_then_explicit_release(db, monkeypatch, tmp_path, late_point):
    from app import approval_store
    monkeypatch.setattr(approval_store, "_pending", {})
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    run_id, cid = "chat_contract_exec", "contract_exec"
    db.add(Conversation(id=cid, title="Contract execution"))
    binding = {"version": 1, "model_id": "Synthetic", "mode": "mock", "approval_mode": "full_access", "policy_revision": 0}
    row = ForegroundRun(id=run_id, conversation_id=cid, question="整理信息", skills=[], images=[],
                        status="running", runtime_config=binding)
    db.add(row)
    attempt, _ = run_lifecycle.admit_attempt(db, run_id, cid, row.question)
    state = AgentState(phase="model", question=row.question, conversation_id=cid, runtime_config=binding,
                       messages=[{"role": "user", "content": row.question}])
    checkpoint = run_lifecycle.bind_checkpoint(state.snapshot(), attempt)
    row.agent_checkpoint = checkpoint
    db.commit()
    target = tmp_path / "must-be-controlled.txt"
    hook_target = tmp_path / "late-hook-must-not-run.txt"
    calls = []
    events, input_revision = [], 0
    def queue_constraint():
        nonlocal input_revision
        run_inputs.submit_input(db, row.id, input_id=f"constraint-input-{input_revision}", conversation_id=cid,
            kind="steer", content="不要写文件", expected_revision=run_inputs.list_inputs(db, row.id, cid)["revision"], workspace_revision=0)
        db.commit()
        input_revision += 1
    def hooks(name, payload, **kwargs):
        if name == "PostToolUse" and payload.get("tool_name") == "list_papers":
            hook_target.write_text("must not execute after steering arrived", encoding="utf-8")
        return {"ran": 1, "blocked": False, "feedback": ""}
    monkeypatch.setattr(loop.hooks_store, "run_hooks", hooks)
    def save(snapshot):
        save_agent_checkpoint(db, run_id, snapshot)
        pending = snapshot.get("pending_operation") or {}
        if late_point == "before_hook_action" and input_revision == 0 and pending.get("kind") == "hook" and pending.get("name") == "PostToolUse":
            queue_constraint()

    class Model:
        count = 0
        def stream_chat(self, messages, tools=None):
            self.count += 1
            name = "list_papers" if self.count == 1 else "mutate_file"
            if self.count <= 3:
                yield "final", LLMResponse("", [{"id": f"call-{self.count}", "name": name, "arguments": {}}])
            else:
                yield "final", LLMResponse("已按更新后的要求处理。")

    model = Model()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": name, "parameters": {"type": "object", "properties": {}}}
                                                        for name in ("list_papers", "mutate_file")])
    monkeypatch.setattr(loop, "tool_risk", lambda name: "safe")
    def execute(db, name, arguments, **kwargs):
        calls.append(name)
        if name == "list_papers" and late_point == "during_tool":
            queue_constraint()
        if name == "mutate_file":
            target.write_text("allowed-once", encoding="utf-8")
        return json.dumps({"status": "completed", "items": []})
    monkeypatch.setattr(loop, "execute_tool", execute)
    for kind, event in loop.run_agent(db, row.question, conversation_id=cid, run_id=run_id,
            checkpoint=checkpoint, save_checkpoint=save):
        events.append((kind, deepcopy(event)))
        correction = None
        if kind == "policy_block" and event["name"] == "mutate_file":
            assert not target.exists()
            assert not hook_target.exists()
            correction = "现在可以写文件了"
        if correction:
            queue_revision = run_inputs.list_inputs(db, row.id, cid)["revision"]
            run_inputs.submit_input(db, row.id, input_id=f"constraint-input-{input_revision}", conversation_id=cid,
                kind="steer", content=correction, expected_revision=queue_revision, workspace_revision=0)
            db.commit()
            input_revision += 1
    assert calls == ["list_papers", "mutate_file"]
    assert target.read_text() == "allowed-once"
    assert not hook_target.exists()
    assert sum(kind == "policy_block" for kind, _ in events) == 1
    assert not any(kind == "approval_request" for kind, _ in events)
    assert next(event for kind, event in events if kind == "answer")["completed"] is True


def test_runtime_repairs_missing_table_without_a_forced_plan_or_approval(db, monkeypatch):
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    run_id, cid = "chat_contract_tables", "contract_tables"
    db.add(Conversation(id=cid, title="Contract tables"))
    binding = {"version": 1, "model_id": "Synthetic", "mode": "mock", "approval_mode": "full_access", "policy_revision": 0}
    row = ForegroundRun(id=run_id, conversation_id=cid, question="请提供两张表格", skills=[], images=[],
                        status="running", runtime_config=binding)
    db.add(row)
    attempt, _ = run_lifecycle.admit_attempt(db, run_id, cid, row.question)
    state = AgentState(phase="model", question=row.question, conversation_id=cid, runtime_config=binding,
                       messages=[{"role": "user", "content": row.question}])
    checkpoint = run_lifecycle.bind_checkpoint(state.snapshot(), attempt)
    row.agent_checkpoint = checkpoint
    db.commit()
    class Model:
        calls = 0
        def stream_chat(self, messages, tools=None):
            self.calls += 1
            yield "final", LLMResponse(TABLE_A if self.calls == 1 else TABLE_A + "\n\n" + TABLE_B)
    model = Model()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [])
    events = list(loop.run_agent(db, row.question, conversation_id=cid, run_id=run_id, checkpoint=checkpoint,
        save_checkpoint=lambda snapshot: save_agent_checkpoint(db, run_id, snapshot)))
    assert model.calls == 2
    assert not any(kind in ("plan", "approval_request") for kind, _ in events)
    assert any(kind == "answer_reset" for kind, _ in events)
    answer = next(event for kind, event in events if kind == "answer")
    assert answer["completed"] is True and answer["requirement_coverage"]["semantic_coverage"] == "unverified"
