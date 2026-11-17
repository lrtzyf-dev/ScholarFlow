"""Explicit model response decisions never weaken planning or evidence policy."""
from copy import deepcopy
import json

import pytest

from app import run_inputs, run_lifecycle
from app.agent import loop, tools as agent_tools
from app.agent.checkpoint import AgentState, PlanResponseDecision
from app.execution_mode import PLAN_RESPONSE_TOOL_NAME, PlanConflict, claim_reviewed_plan
from app.foreground_run_store import save_agent_checkpoint
from app.llm import LLMResponse
from app.models import Conversation, ForegroundRun, QARecord


def control(kind="direct_response", answer="这是本轮需要的直接回答。", **extra):
    return {"id": "response-control", "name": PLAN_RESPONSE_TOOL_NAME,
            "arguments": {"kind": kind, "answer": answer, **extra}}


def response(call=None, text=""):
    return LLMResponse(text, [call] if call else [])


class Model:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def stream_chat(self, messages, tools=None):
        index = len(self.calls)
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        assert index < len(self.replies), "unexpected additional model request"
        yield "final", self.replies[index]


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(loop.settings, "memory_recall", False)
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *a, **k: "")
    monkeypatch.setattr(loop.goal_store, "get_current_goal", lambda: None)
    monkeypatch.setattr(loop, "_load_history", lambda *a, **k: ([], {"total": 0}))
    specs = [deepcopy(spec) for spec in agent_tools.TOOL_SPECS if spec["name"] in ("update_plan", "list_papers")]
    monkeypatch.setattr(loop, "get_tool_specs", lambda: deepcopy(specs))
    monkeypatch.setattr(loop.approval_store, "create", lambda *a, **k: pytest.fail("response requested approval"))
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: pytest.fail("planning invoked a command hook"))


def drive(db, monkeypatch, replies, *, question="解释这个概念", checkpoint=None, **kwargs):
    model = Model(replies)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(loop.run_agent(db, question, conversation_id="c_plan_response", execution_mode="plan",
                                 checkpoint=checkpoint, **kwargs))
    answer = next((event for kind, event in events if kind == "answer"), None)
    assert answer is not None, [(kind, event) for kind, event in events if kind in ("error", "status", "memory")]
    return model, events, answer


@pytest.mark.parametrize("question", ["你好", "比较这两种表达的语气", "把这句话翻译成英文", "Explain a general concept"])
def test_explicit_direct_response_is_one_model_call_without_visible_tool_or_executable_plan(db, monkeypatch, question):
    model, events, answer = drive(db, monkeypatch, [response(control())], question=question)
    assert answer["outcome"] == "completed" and answer["completed"] is True
    assert answer["response_kind"] == "direct_response" and answer["stop_reason"] == "completed"
    assert answer["plan_steps"] == [] and answer["execution_mode"] == "plan"
    assert len(model.calls) == 1 and "规划未完成" not in answer["answer"]
    assert not any(kind in ("tool_call", "tool_result", "approval_request", "hook", "plan") for kind, _ in events)
    record = db.query(QARecord).one()
    assert record.execution_mode == "plan" and not record.plan_steps
    with pytest.raises(PlanConflict):
        claim_reviewed_plan(db, record.id, "c_plan_response", "chat_not_a_plan")


def test_default_execute_never_offers_or_needs_response_control(db, monkeypatch):
    model = Model([response(text="正常直接回答")])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    events = list(loop.run_agent(db, "任意一般问题", conversation_id="c_execute_response"))
    assert next(event for kind, event in events if kind == "answer")["completed"] is True
    assert all(spec["name"] != PLAN_RESPONSE_TOOL_NAME for spec in model.calls[0]["tools"])
    assert len(model.calls) == 1


def test_control_content_is_not_mislabeled_as_work_narration_or_appended_to_answer(db, monkeypatch):
    _, events, answer = drive(db, monkeypatch, [response(control(answer="完整直接回答"), text="需要被替换的草稿")])
    assert answer["answer"] == "完整直接回答" and answer["completed"] is True
    assert not any(kind == "commentary" for kind, _ in events)
    assert any(kind == "answer_reset" for kind, _ in events)
    assert [event["text"] for kind, event in events if kind == "answer_delta"] == ["完整直接回答"]


@pytest.mark.parametrize("call", [control(kind="planned"), control(answer=" "), control(untrusted=True)])
def test_invalid_response_decision_does_not_convert_text_only_plan_into_completed(db, monkeypatch, call):
    model, events, answer = drive(db, monkeypatch, [response(call), response(text="尚未登记的计划正文")])
    assert answer["outcome"] == "partial" and answer["stop_reason"] == "plan_not_created"
    assert "response_kind" not in answer
    assert len(model.calls) == 2
    assert any(kind == "status" and event.get("response_accepted") is False for kind, event in events)


def test_control_cannot_share_a_batch_with_real_or_forbidden_tools(db, monkeypatch):
    invoked = []
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **k: invoked.append(True) or "{}")
    mixed = LLMResponse("", [control(), {"id": "write", "name": "edit_project_file", "arguments": {}}])
    _, events, answer = drive(db, monkeypatch, [mixed, response(text="缺少可审阅计划")])
    assert not invoked and answer["completed"] is False
    assert any(kind == "status" and event.get("response_accepted") is False for kind, event in events)


def test_registered_public_plan_cannot_be_downgraded_by_response_control(db, monkeypatch):
    step = {"title": "整理报告说明", "phase": "write", "status": "pending", "evidence": []}
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **k: json.dumps({"plan_steps": [step]}))
    plan = {"id": "plan", "name": "update_plan", "arguments": {"steps": [{"title": step["title"], "phase": "write"}]}}
    model, events, answer = drive(db, monkeypatch, [response(plan), response(control()), response(text="待审阅的真实计划")])
    assert answer["outcome"] == "planned" and answer["completed"] is False
    assert answer["plan_steps"][0]["title"] == step["title"] and len(model.calls) == 3
    assert "response_kind" not in answer
    assert any(kind == "status" and event.get("response_accepted") is False for kind, event in events)


def test_necessary_read_does_not_force_a_plan_before_a_direct_response(db, monkeypatch):
    reads = []
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **k: reads.append(True) or '{"papers": []}')
    read = {"id": "read", "name": "list_papers", "arguments": {}}
    model, events, answer = drive(db, monkeypatch, [response(read), response(control())])
    assert reads == [True] and len(model.calls) == 2
    assert answer["completed"] is True and answer["plan_steps"] == []
    assert [event["name"] for kind, event in events if kind == "tool_call"] == ["list_papers"]


def state(**kwargs):
    return AgentState(phase="model", question="说明当前情况", conversation_id="c_plan_response", execution_mode="plan",
                      messages=[{"role": "user", "content": "说明当前情况"}], **kwargs)


@pytest.mark.parametrize("guard", ["acceptance", "failure"])
def test_response_control_cannot_erase_unfinished_acceptance_or_tool_failure(db, monkeypatch, guard):
    current = state()
    if guard == "acceptance":
        current.plan_source = "requirements"
        current.todo = [{"title": "用户要求的文件", "phase": "write", "status": "pending", "acceptance": "project_file"}]
    else:
        current.tool_failures = [{"id": "failure/tool/0/0", "tool_name": "list_papers", "phase": "read", "status": "open"}]
    _, _, answer = drive(db, monkeypatch, [response(control()), response(text="保留未完成内容")],
                        question=current.question, checkpoint=current.snapshot())
    assert answer["outcome"] == "partial" and answer["completed"] is False
    assert answer["incomplete_steps"] and "response_kind" not in answer


def test_direct_response_cannot_bypass_a_required_goal_ledger_item_missing_from_local_plan(db, monkeypatch):
    from app.goal_ledger import create_goal, sync_plan_requirements
    db.add(Conversation(id="c_plan_response"))
    goal = create_goal(db, "用户指定的交付要求", scope="conversation", conversation_id="c_plan_response",
                       source_message_id="user-required-delivery")
    sync_plan_requirements(db, goal["id"], [{"title": "必须交付的文件", "phase": "write",
        "acceptance": "project_file"}], trusted_user_acceptance=True)
    db.commit()
    current = state(goal_id=goal["id"])
    _, _, answer = drive(db, monkeypatch, [response(control()), response(text="尚有未完成交付")],
                        question=current.question, checkpoint=current.snapshot())
    assert not current.todo and answer["completed"] is False
    assert any(step.get("acceptance") == "project_file" for step in answer["incomplete_steps"])


def test_clarification_is_not_a_completed_task_or_a_missing_plan(db, monkeypatch):
    current = state(plan_source="requirements", todo=[{"title": "用户要求的文件", "phase": "write",
        "status": "pending", "acceptance": "project_file"}])
    model, _, answer = drive(db, monkeypatch, [response(control(kind="clarification", answer="请提供需要处理的文件。"))],
                            question=current.question, checkpoint=current.snapshot())
    assert len(model.calls) == 1 and answer["answer"] == "请提供需要处理的文件。"
    assert answer["outcome"] == "partial" and answer["completed"] is False
    assert answer["stop_reason"] == "needs_clarification" and answer["response_kind"] == "clarification"
    assert any(step.get("acceptance") == "project_file" for step in answer["incomplete_steps"])
    assert "规划未完成" not in answer["answer"] and not db.query(QARecord).one().plan_steps


@pytest.mark.parametrize("boundary", ["budget", "summary", "failure", "stale_input"])
def test_finalization_rechecks_decision_instead_of_trusting_saved_control(db, monkeypatch, boundary):
    current = state(plan_response=PlanResponseDecision(kind="direct_response"))
    current.phase, current.answer = "finalize", "已保存的直接回应"
    # Isolate finalization from the separate legacy-memory migration path.
    # A fabricated checkpoint without a fingerprint correctly rebuilds its
    # dialogue stage; it is not an already-current finalization checkpoint.
    from app.memory_governance import fingerprint
    current.memory_fingerprint = fingerprint(db, current.conversation_id)
    if boundary == "budget":
        current.budget_exhausted = True
    elif boundary == "summary":
        current.summary_failed = True
    elif boundary == "failure":
        current.tool_failures = [{"id": "failure/tool/0/0", "tool_name": "list_papers", "phase": "read", "status": "open"}]
    else:
        current.applied_input_ids = ["new-input"]
    model, _, answer = drive(db, monkeypatch, [], question=current.question, checkpoint=current.snapshot())
    assert not model.calls and answer["completed"] is False
    assert answer["outcome"] in ("partial", "blocked") and "response_kind" not in answer


def test_control_answer_still_passes_citation_rejection(db, monkeypatch):
    _, _, answer = drive(db, monkeypatch, [response(control(answer="这是虚构来源的结论。[^e999]"))])
    assert answer["refusal"] == 1 and answer["outcome"] == "blocked" and answer["completed"] is False
    assert "response_kind" not in answer


def test_response_decision_survives_checkpoint_restart_without_another_model_call(db, monkeypatch):
    snapshots = []
    class Crash(BaseException):
        pass
    def save(snapshot):
        snapshots.append(deepcopy(snapshot))
        db.commit()
        if snapshot["phase"] == "finalize" and snapshot.get("plan_response"):
            raise Crash()
    model = Model([response(control(answer="已持久化的回答"))])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    with pytest.raises(Crash):
        list(loop.run_agent(db, "说明当前情况", conversation_id="c_plan_response", execution_mode="plan", save_checkpoint=save))
    saved = snapshots[-1]
    assert saved["plan_response"] == {"kind": "direct_response", "applied_input_ids": []}
    assert saved["answer"] == "已持久化的回答" and len(model.calls) == 1
    resumed, _, answer = drive(db, monkeypatch, [], question="说明当前情况", checkpoint=saved)
    assert not resumed.calls and answer["completed"] is True and answer["answer"] == "已持久化的回答"
    assert db.query(QARecord).count() == 1


def test_user_steering_invalidates_old_direct_decision_before_new_response(db, monkeypatch):
    cid, run_id, question = "c_plan_response", "chat_plan_response_steer", "说明当前情况"
    db.add(Conversation(id=cid))
    binding = {"version": 1, "mode": "mock", "approval_mode": "full_access", "policy_revision": 0}
    row = ForegroundRun(id=run_id, conversation_id=cid, question=question, execution_mode="plan",
                        skills=[], images=[], status="running", runtime_config=binding)
    db.add(row)
    attempt, _ = run_lifecycle.admit_attempt(db, run_id, cid, question)
    current = state(runtime_config=binding)
    checkpoint = run_lifecycle.bind_checkpoint(current.snapshot(), attempt)
    row.agent_checkpoint = checkpoint
    db.commit()
    model = Model([response(control(answer="不能交付的旧答复")), response(text="新请求还没有登记计划")])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = []
    for kind, event in loop.run_agent(db, question, conversation_id=cid, execution_mode="plan", run_id=run_id,
            checkpoint=checkpoint, save_checkpoint=lambda value: save_agent_checkpoint(db, run_id, value)):
        events.append((kind, event))
        if kind == "status" and event.get("response_accepted"):
            run_inputs.submit_input(db, run_id, input_id="input_plan_response_001", conversation_id=cid,
                kind="steer", content="请先制定下一步的可执行计划", expected_revision=0, workspace_revision=0)
            db.commit()
    answer = next(event for kind, event in events if kind == "answer")
    db.refresh(row)
    assert answer["completed"] is False and answer["stop_reason"] == "plan_not_created"
    assert "不能交付的旧答复" not in answer["answer"] and len(model.calls) == 2
    assert row.agent_checkpoint["plan_response"] is None
    assert row.agent_checkpoint["applied_input_ids"] == ["input_plan_response_001"]
