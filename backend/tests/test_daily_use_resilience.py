"""Failure/recovery boundaries for daily use; no real provider or user data."""
from copy import deepcopy
import json

import pytest

from app import cancel_store
from app.agent import loop, tools as agent_tools
from app.llm import LLMResponse
from app.models import QARecord


class Replies:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def stream_chat(self, messages, tools=None):
        self.calls.append(deepcopy(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield "final", reply


def tool(name):
    return LLMResponse("", [{"id": name + "-call", "name": name, "arguments": {}}])


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "approval_mode", "auto")
    monkeypatch.setattr(loop.settings, "work_rules", "")
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *args: "")
    monkeypatch.setattr(loop.goal_store, "get_current_goal", lambda: None)
    monkeypatch.setattr(loop, "_load_history", lambda *args, **kwargs: ([], {"total": 0}))
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *args, **kwargs: {
        "ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [
        deepcopy(spec) for spec in agent_tools.TOOL_SPECS if spec["name"] == "list_papers"])


def drive(db, **kwargs):
    return loop.run_agent(db, "按当前请求继续", conversation_id="c_daily_resilience", **kwargs)


def test_unknown_tool_returns_to_model_without_human_approval(db, monkeypatch):
    model = Replies(tool("nonexistent_research_tool"), tool("list_papers"), LLMResponse("当前目录已核对。"))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    source = drive(db, run_id="daily_unknown")
    events = []
    try:
        for event in source:
            events.append(event)
            if event[0] == "approval_request":
                # Never let a regression test hang on impossible-tool approval.
                cancel_store.request_cancel("daily_unknown")
    finally:
        source.close()
    assert not any(kind == "approval_request" for kind, _ in events), (
        "An unknown model tool must be returned as an error, not block on human approval")
    assert any(kind == "tool_result" and "unknown tool" in event["preview"]
               for kind, event in events)
    first_result = next(index for index, (kind, _) in enumerate(events) if kind == "tool_result")
    assert not any(kind in ("plan", "tool_call", "todo_update") for kind, _ in events[:first_result])
    assert db.query(QARecord).one().answer == "当前目录已核对。"


@pytest.mark.parametrize("arguments", [None, [], {}, {"query": 1}, {"query": ""},
                                      {"query": "valid", "limit": True},
                                      {"query": "valid", "limit": 0},
                                      {"query": "valid", "unrecognized": "value"}])
def test_invalid_dynamic_mcp_arguments_return_to_model_before_execution(db, monkeypatch, arguments):
    name = "mcp__fixture__lookup"
    schema = {"type": "object", "additionalProperties": False,
              "$defs": {"query": {"type": "string", "minLength": 1}},
              "properties": {"query": {"$ref": "#/$defs/query"},
                             "limit": {"type": "integer", "minimum": 1}}, "required": ["query"]}
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": name, "parameters": schema}])
    monkeypatch.setattr(loop, "tool_risk", lambda value: "safe")
    model = Replies(LLMResponse("", [{"id": "bad", "name": name, "arguments": arguments}]),
                    LLMResponse("", [{"id": "fixed", "name": name, "arguments": {"query": "valid", "limit": 2}}]),
                    LLMResponse("已经按有效参数完成。"))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    calls = []
    monkeypatch.setattr(loop, "execute_tool", lambda db, name, arguments, **kwargs:
                        calls.append((name, arguments)) or json.dumps({"items": []}))
    events = list(drive(db))
    assert calls == [(name, {"query": "valid", "limit": 2})]
    assert not any(kind == "approval_request" for kind, _ in events)
    first_result = next(event for kind, event in events if kind == "tool_result")
    assert json.loads(first_result["preview"])["invalid_tool_call"] is True
    assert "规范" in model.calls[1][-1]["content"] or "JSON 对象" in model.calls[1][-1]["content"]
    assert next(event for kind, event in events if kind == "answer")["completed"] is True


def test_tool_exception_is_visible_to_model_and_recovery_can_complete(db, monkeypatch):
    model = Replies(tool("list_papers"), tool("list_papers"), LLMResponse("已成功读取目录。"))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    attempts = []

    def execute(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("synthetic first read failure")
        return json.dumps({"papers": []})

    monkeypatch.setattr(loop, "execute_tool", execute)
    events = list(drive(db))
    assert len(attempts) == 2
    assert any(kind == "error" and "synthetic first read failure" in event["text"]
               for kind, event in events)
    assert "synthetic first read failure" in model.calls[1][-1]["content"]
    answer = next(event for kind, event in events if kind == "answer")
    assert answer["completed"] is True and answer["outcome"] == "completed"
    assert db.query(QARecord).one().answer == "已成功读取目录。"


def test_model_cannot_register_fabricated_plan_completion_or_evidence(db, monkeypatch):
    specs = [deepcopy(spec) for spec in agent_tools.TOOL_SPECS if spec["name"] in ("update_plan", "list_papers")]
    monkeypatch.setattr(loop, "get_tool_specs", lambda: specs)
    bad_steps = [{"title": "核对目录", "phase": "search", "status": "done", "evidence": [{"kind": "invented"}]}]
    good_steps = [{"title": "核对目录", "phase": "search", "tool_name": "list_papers", "tool_arguments": {}},
                  {"title": "交付结果", "phase": "write"}]
    model = Replies(LLMResponse("", [{"id": "fake", "name": "update_plan", "arguments": {"steps": bad_steps}}]),
                    LLMResponse("", [{"id": "valid", "name": "update_plan", "arguments": {"steps": good_steps}}]),
                    tool("list_papers"), LLMResponse("当前目录为空。"))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(drive(db))
    first_result = next(event for kind, event in events if kind == "tool_result")
    assert json.loads(first_result["preview"])["invalid_tool_call"] is True
    first_plan = next(event for kind, event in events if kind == "plan")
    assert all(step["status"] == "pending" and not step.get("evidence") for step in first_plan["steps"])
    final_answer = next(event for kind, event in events if kind == "answer")
    assert final_answer["completed"] is True
    assert not any(proof.get("kind") == "invented" for step in final_answer["plan_steps"]
                   for proof in step.get("evidence", []))
    actual = next(step for step in final_answer["plan_steps"] if step.get("tool_name") == "list_papers")
    assert actual["status"] == "done" and any(proof.get("kind") == "tool_result"
        and proof.get("tool_name") == "list_papers" and proof.get("status") == "completed"
        for proof in actual.get("evidence", []))


def test_mcp_protocol_error_never_completes_search_plan(db, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import app.mcp_bridge as mcp_module

    class ProtocolClient:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text="Synthetic search rejected")])

    bridge = mcp_module.MCPBridge.__new__(mcp_module.MCPBridge)
    bridge._conns = {"fixture": mcp_module._Conn(
        config={"name": "fixture", "enabled": True}, client=ProtocolClient(), status="connected")}
    monkeypatch.setattr(bridge, "_run", lambda coro, timeout: asyncio.run(coro))
    monkeypatch.setattr(mcp_module, "bridge", bridge)
    name = "mcp__fixture__search"
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{
        "name": name, "parameters": {"type": "object", "properties": {}}}])
    monkeypatch.setattr(loop, "tool_risk", lambda value: "safe")
    model = Replies(tool(name), LLMResponse("本次检索失败，尚未取得资料。"),
                    LLMResponse("工具仍不可用，检索未完成。"))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(drive(db))
    result = next(event for kind, event in events if kind == "tool_result")
    assert json.loads(result["preview"])["error"]
    answer = next(event for kind, event in events if kind == "answer")
    assert answer["completed"] is False and answer["outcome"] == "partial"
    search = next(step for step in answer["incomplete_steps"] if step["phase"] == "search")
    assert search["status"] != "done" and not search.get("evidence")


def test_empty_model_answer_is_generic_blocked_response_without_research(db, monkeypatch):
    model = Replies(LLMResponse(""))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(drive(db))
    answer = next(event for kind, event in events if kind == "answer")
    assert "模型没有返回有效回答" in answer["answer"]
    assert "文献" not in answer["answer"]
    assert answer["outcome"] == "blocked" and answer["refusal"] == 1
    assert not any(kind in ("plan", "tool_call", "audit") for kind, _ in events)
    assert db.query(QARecord).count() == 1


def test_model_failure_after_tool_can_resume_without_replaying_completed_tool(db, monkeypatch):
    model = Replies(tool("list_papers"), ValueError("synthetic provider failure"))
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    executed = []
    monkeypatch.setattr(loop, "execute_tool", lambda *args, **kwargs:
                        executed.append(True) or json.dumps({"papers": []}))
    saved = []

    def persist(snapshot):
        saved.append(deepcopy(snapshot))
        db.commit()

    events = list(drive(db, save_checkpoint=persist))
    assert any(kind == "error" for kind, _ in events)
    assert not any(kind in ("answer", "done") for kind, _ in events)
    assert db.query(QARecord).count() == 0 and len(executed) == 1
    assert saved[-1]["phase"] == "model"
    model.replies = [LLMResponse("基于已保存的目录继续完成。")]
    resumed = list(drive(db, checkpoint=saved[-1], save_checkpoint=persist))
    assert len(executed) == 1
    assert next(event for kind, event in resumed if kind == "answer")["completed"] is True
    assert db.query(QARecord).count() == 1
