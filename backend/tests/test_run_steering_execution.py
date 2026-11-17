"""Applied user input changes decisions before new tools and obsolete approvals."""
from copy import deepcopy
import json

import pytest

from app import approval_store, run_inputs, run_lifecycle
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.foreground_run_store import save_agent_checkpoint
from app.llm import LLMResponse
from app.models import ForegroundRun, QARecord


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(approval_store, "_pending", {})
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "memory_capture", False)


def source(db, monkeypatch, model, *, guarded=False):
    run_id, cid = "chat_steering_execute", "c_steering_execute"
    binding = {"version": 1, "model_id": "Synthetic-Model", "mode": "mock",
               "approval_mode": "always" if guarded else "full_access", "policy_revision": 0}
    row = ForegroundRun(id=run_id, conversation_id=cid, question="Explain the result", skills=[],
                        images=[], status="running", runtime_config=binding)
    db.add(row)
    attempt, _ = run_lifecycle.admit_attempt(db, run_id, cid, row.question)
    state = AgentState(phase="model", question=row.question, conversation_id=cid,
                       runtime_config=binding, messages=[{"role": "user", "content": row.question}])
    checkpoint = run_lifecycle.bind_checkpoint(state.snapshot(), attempt)
    row.agent_checkpoint = checkpoint
    db.commit()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": "synthetic_read", "parameters": {
        "type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]}}])
    monkeypatch.setattr(loop, "tool_risk", lambda name: "guarded" if guarded else "safe")
    return row, loop.run_agent(db, row.question, conversation_id=cid, run_id=run_id,
        checkpoint=checkpoint, save_checkpoint=lambda snapshot: save_agent_checkpoint(db, run_id, snapshot))


def steer(db, row, *, text="请用中文分三条说明", number=0):
    result = run_inputs.submit_input(db, row.id, input_id=f"input_steering_{number:03}",
        conversation_id=row.conversation_id, kind="steer", content=text,
        expected_revision=number, workspace_revision=0)
    assert result["input"]["status"] == "queued"
    db.commit()


def test_input_received_during_model_stream_replaces_draft_before_delivery(db, monkeypatch):
    seen = []

    class Model:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            if len(seen) == 1:
                yield "delta", "answer", "Old draft"
                yield "final", LLMResponse("Old draft")
            else:
                assert messages[-1]["content"] == "请用中文分三条说明"
                yield "final", LLMResponse("一、结果；二、依据；三、建议。")

    row, stream = source(db, monkeypatch, Model())
    events = []
    for kind, event in stream:
        events.append((kind, event))
        if kind == "answer_delta":
            steer(db, row)
    applied = [event["input"] for kind, event in events if kind == "run_input"]
    assert len(applied) == 1 and applied[0]["status"] == "applied"
    assert applied[0]["attempt_id"]
    answer = next(event for kind, event in events if kind == "answer")
    assert answer["answer"] == "一、结果；二、依据；三、建议。" and answer["completed"] is True
    assert db.query(QARecord).one().answer == answer["answer"]
    db.refresh(row)
    assert row.agent_checkpoint["applied_input_ids"] == ["input_steering_000"]
    assert run_inputs.list_inputs(db, row.id, row.conversation_id)["inputs"][0]["status"] == "applied"


def test_input_between_tools_preserves_first_receipt_and_abandons_remaining_old_calls(db, monkeypatch):
    seen, executed = [], []

    class Model:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            if len(seen) == 1:
                yield "final", LLMResponse("", [{"id": item, "name": "synthetic_read", "arguments": {"item": item}}
                                                    for item in ("A", "B")])
            else:
                yield "final", LLMResponse("已按补充要求整理。")

    row, stream = source(db, monkeypatch, Model())
    monkeypatch.setattr(loop, "execute_tool", lambda db, name, args, **kw: executed.append(args["item"]) or json.dumps({"read": args["item"]}))
    for kind, event in stream:
        if kind == "tool_result":
            steer(db, row)
    assert executed == ["A"]
    receipts = {message["tool_call_id"]: json.loads(message["content"]) for message in seen[-1] if message["role"] == "tool"}
    assert receipts["A"] == {"read": "A"}
    assert receipts["B"]["cancelled_by_user_steering"] is True
    db.refresh(row)
    assert "tool/0/0" in row.agent_checkpoint["operations"]
    assert "tool/0/1" not in row.agent_checkpoint["operations"]


def test_input_while_waiting_for_approval_supersedes_old_action_without_execution(db, monkeypatch):
    seen, executed, decisions = [], [], []

    class Model:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            if len(seen) == 1:
                yield "final", LLMResponse("", [{"id": "A", "name": "synthetic_read", "arguments": {"item": "A"}}])
            else:
                yield "final", LLMResponse("已改用用户补充的信息，不再执行旧操作。")

    row, stream = source(db, monkeypatch, Model(), guarded=True)
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **kw: executed.append(True) or "{}")
    for kind, event in stream:
        if kind == "approval_request":
            steer(db, row)
        elif kind == "approval_result":
            decisions.append(event["decision"])
    assert executed == [] and decisions == ["superseded"]
    assert not approval_store.list_for_run(row.id)
    assert db.query(QARecord).count() == 1


def test_optional_analysis_method_does_not_turn_a_delivered_answer_into_partial(db, monkeypatch):
    calls = []

    class Model:
        def stream_chat(self, messages, tools=None):
            calls.append(deepcopy(messages))
            if len(calls) == 1:
                yield "final", LLMResponse("", [{"id": "plan", "name": "update_plan", "arguments": {}}])
            else:
                yield "final", LLMResponse("已完成解释。")

    row, stream = source(db, monkeypatch, Model())
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": "update_plan", "parameters": {"type": "object"}}])
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **kw: json.dumps({"plan_steps": [
        {"title": "组织解释", "phase": "analyze", "status": "pending"}]}))
    events = list(stream)
    answer = next(event for kind, event in events if kind == "answer")
    assert answer["completed"] is True and len(calls) == 2
