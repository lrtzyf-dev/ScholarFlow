"""Failed methods stay visible until an actual retry or verified alternative."""
from copy import deepcopy
import json

import pytest

from app import run_lifecycle
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.foreground_run_store import save_agent_checkpoint
from app.goal_ledger import revise_goal, snapshot_goal
from app.llm import LLMResponse
from app.models import ForegroundRun
from app.runtime_models import RuntimeRun

PRIMARY, FALLBACK = "mcp__fixture__primary_search", "mcp__fixture__fallback_search"


def tool(name, index):
    return LLMResponse("", [{"id": f"call_{index}", "name": name, "arguments": {} if name == "update_plan" else {"q": "term"}}])


def run(db, monkeypatch, replies, *, required_primary=False, fallback_answer=False):
    rid, cid = "chat_failure_outcomes", "c_failure_outcomes"
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop, "tool_risk", lambda name: "safe")
    binding = {"version": 1, "mode": "mock", "model_id": "Synthetic", "approval_mode": "full_access"}
    row = ForegroundRun(id=rid, conversation_id=cid, question="Explain", status="running", skills=[], images=[])
    db.add(row)
    attempt, _ = run_lifecycle.admit_attempt(db, rid, cid, row.question)
    goal_id = db.get(RuntimeRun, rid).goal_id
    if required_primary:
        goal = snapshot_goal(db, goal_id)
        revise_goal(db, goal_id, expected_revision=goal["revision"], action="add_requirements", source_message_id="user_required_tool",
            requirements=[{"title": "指定来源的检索结果", "phase": "search", "tool_name": PRIMARY, "tool_arguments": {"q": "term"}}])
    state = AgentState(phase="model", question=row.question, conversation_id=cid,
                       runtime_config=binding, messages=[{"role": "user", "content": row.question}])
    row.agent_checkpoint = run_lifecycle.bind_checkpoint(state.snapshot(), attempt)
    db.commit()

    class Model:
        def stream_chat(self, messages, tools=None):
            assert replies, "unexpected model retry"
            yield "final", replies.pop(0)

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": name, "parameters": {"type": "object"}}
                                                        for name in (PRIMARY, FALLBACK, "update_plan")])
    primary_calls = []

    def invoke(db, name, args, **kw):
        if name == PRIMARY:
            primary_calls.append(args)
            return json.dumps({"error": "Synthetic source temporarily unavailable"} if len(primary_calls) == 1 else {"results": ["Actual primary result"]})
        if name == "update_plan":
            step = ({"title": "根据已有上下文解释", "phase": "analyze", "status": "pending"} if fallback_answer else
                    {"title": "改用备选检索", "phase": "search", "status": "pending", "tool_name": FALLBACK, "tool_arguments": {"q": "term"}})
            return json.dumps({"plan_steps": [step]})
        return json.dumps({"results": ["Actual fallback result"]})

    monkeypatch.setattr(loop, "execute_tool", invoke)
    events = list(loop.run_agent(db, row.question, conversation_id=cid, run_id=rid,
        checkpoint=deepcopy(row.agent_checkpoint), save_checkpoint=lambda snap: save_agent_checkpoint(db, rid, snap)))
    db.refresh(row)
    return next(event for kind, event in events if kind == "answer"), row.agent_checkpoint, snapshot_goal(db, goal_id)


def test_actual_successful_retry_clears_open_failure_and_retains_failure_history(db, monkeypatch):
    answer, checkpoint, goal = run(db, monkeypatch, [tool(PRIMARY, 0), tool(PRIMARY, 1), LLMResponse("已取得结果。")])
    assert answer["completed"] is True
    assert checkpoint["tool_failures"][0]["resolution"] == "successful_retry"
    assert checkpoint["tool_failures"][0]["error"]


def test_explicit_replan_and_actual_fallback_complete_without_erasing_failure(db, monkeypatch):
    answer, checkpoint, goal = run(db, monkeypatch, [tool(PRIMARY, 0), tool("update_plan", 1), tool(FALLBACK, 2), LLMResponse("备选来源已取得结果。")])
    failure = checkpoint["tool_failures"][0]
    assert answer["completed"] is True and failure["status"] == "resolved"
    assert failure["resolution"] == "verified_alternative" and failure["alternative_proof"]["tool_name"] == FALLBACK
    assert failure["tool_name"] == PRIMARY and failure["error"]


def test_fallback_cannot_waive_an_explicit_user_tool_requirement(db, monkeypatch):
    answer, checkpoint, goal = run(db, monkeypatch, [tool(PRIMARY, 0), tool("update_plan", 1), tool(FALLBACK, 2), LLMResponse("备选结果已取得，指定来源仍不可用。")], required_primary=True)
    assert answer["completed"] is False and goal["incomplete_requirements"]
    assert checkpoint["tool_failures"][0]["status"] == "open"


def test_unrelated_success_without_explicit_method_change_does_not_clear_failure(db, monkeypatch):
    answer, checkpoint, _ = run(db, monkeypatch, [tool(PRIMARY, 0), tool(FALLBACK, 1), LLMResponse("另一来源返回内容。")])
    assert answer["completed"] is False and checkpoint["tool_failures"][0]["status"] == "open"


def test_optional_exploration_can_be_replaced_by_a_completed_context_answer(db, monkeypatch):
    answer, checkpoint, goal = run(db, monkeypatch, [tool(PRIMARY, 0), tool("update_plan", 1), LLMResponse("根据已有上下文给出解释。")], fallback_answer=True)
    assert answer["completed"] is True and not goal["incomplete_requirements"]
    assert all(not requirement["required"] for requirement in goal["requirements"])
    assert checkpoint["tool_failures"][0]["resolution"] == "verified_alternative"
