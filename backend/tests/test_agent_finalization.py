"""A bounded final answer must consume completed tool results and preserve truth."""
from copy import deepcopy
import json
import threading

import pytest

from app import cancel_store, task_runner
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.agent.execution import LIMIT_SUMMARY_FAILED, LIMIT_SUMMARY_NOTE
from app.llm import LLMResponse
from app.models import QARecord, TaskRecord


class PowerLoss(BaseException):
    pass


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(loop.settings, "max_agent_steps", 6)
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "memory_recall", False)
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(loop, "make_plan", lambda *a: [
        {"title": "读取文件", "phase": "read", "status": "pending"},
        {"title": "核实其余文件", "phase": "audit", "status": "pending"},
        {"title": "定稿交付", "phase": "final", "status": "pending"},
    ])
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {
        "ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [
        {"name": "list_papers", "parameters": {"type": "object", "properties": {}}}])
    monkeypatch.setattr(loop, "tool_risk", lambda name: "safe")


class LimitModel:
    def __init__(self, outcome="valid", run_id=None):
        self.calls = []
        self.outcome = outcome
        self.run_id = run_id
        self.closed = 0
        self.tool_limit = None

    def stream_chat(self, messages, tools=None):
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        try:
            if tools is not None:
                index = sum(call["tools"] is not None for call in self.calls)
                if self.tool_limit is not None and index > self.tool_limit:
                    yield "final", LLMResponse("已完成剩余核实并整理答案。")
                    return
                yield "final", LLMResponse("", [{"id": f"call_{index}", "name": "list_papers",
                    "arguments": {"page": index}}], usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
                return
            if self.outcome == "error":
                raise TimeoutError("synthetic timeout")
            if self.outcome == "empty_stream":
                return
            if self.outcome == "cancel":
                cancel_store.request_cancel(self.run_id)
                yield "delta", "answer", "must not be delivered"
                return
            if self.outcome == "tools":
                yield "delta", "answer", "unverified preamble"
                yield "final", LLMResponse("unverified preamble", [{"id": "extra", "name": "list_papers", "arguments": {}}])
                return
            content = "" if self.outcome == "empty" else "已读取 file-6 的结果；其余文件尚未核实。"
            yield "delta", "answer", content
            yield "final", LLMResponse(content, usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})
        finally:
            self.closed += 1


def setup_model(monkeypatch, outcome="valid", run_id=None):
    model = LimitModel(outcome, run_id)
    completed = []
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    def tool(db, name, arguments, **kwargs):
        completed.append(deepcopy(arguments))
        return json.dumps({"text": f"file-{arguments['page']}", "partial": True})
    monkeypatch.setattr(loop, "execute_tool", tool)
    return model, completed


def drive(db, **kwargs):
    if kwargs.get("checkpoint") and not kwargs["checkpoint"].get("memory_fingerprint"):
        # These fixtures exercise finalization of an accepted current stage.
        # Legacy stages without a fingerprint intentionally rebuild elsewhere.
        from app.memory_governance import fingerprint
        kwargs["checkpoint"] = {**kwargs["checkpoint"],
            "memory_fingerprint": fingerprint(db, "c_finalization")}
    return loop.run_agent(db, "读取文件夹全部内容", conversation_id="c_finalization", **kwargs)


def final_answer(events):
    return next(event for kind, event in events if kind == "answer")


def assert_plan_incomplete(events):
    progress = [event for kind, event in events if kind == "todo_update"]
    if progress:
        assert not all(step["status"] == "done" for step in progress[-1]["steps"])
    else:
        # Automatic acceptance checks remain enforceable without inventing a
        # visible plan for each tool call.
        result = final_answer(events)
        assert result["plan_steps"] == [] and result["incomplete_steps"]
        assert result["completed"] is False
    assert all(event["text"] != "全部任务完成" for event in progress)


def test_budget_boundary_saves_checkpoint_and_automatically_continues(db, monkeypatch):
    model, completed = setup_model(monkeypatch)
    model.tool_limit = 6
    saved = []
    events = list(drive(db, run_id="auto_continue", save_checkpoint=lambda state: saved.append(deepcopy(state))))
    assert len(completed) == 6
    assert len(model.calls) == 8
    assert all(call["tools"] for call in model.calls)
    assert not any(kind == "paused" for kind, _ in events)
    assert any(kind == "status" and "自动继续" in event["text"] for kind, event in events)
    state = AgentState.model_validate(saved[-1])
    assert state.phase == "completed" and state.budget_exhausted is False
    assert state.step == 6 and state.max_steps == 12
    assert state.continuation_segments == 1
    assert sum(key.startswith("tool/") for key in state.operations) == 6
    assert sum(kind == "tool_call" for kind, _ in events) == 6
    assert model.closed == 8
    assert final_answer(events)["stop_reason"] == "incomplete_requirements"
    assert final_answer(events)["outcome"] == "partial"
    assert_plan_incomplete(events)


def test_multiple_automatic_segments_preserve_completed_tool_results(db, monkeypatch):
    model, completed = setup_model(monkeypatch)
    model.tool_limit = 12
    saved = []
    events = list(drive(db, run_id="multi_segment", save_checkpoint=lambda state: saved.append(deepcopy(state))))
    state = AgentState.model_validate(saved[-1])
    assert len(completed) == 12
    assert len(model.calls) == 14
    assert sum(kind == "status" and "自动继续" in event["text"] for kind, event in events) == 2
    assert state.phase == "completed" and state.step == 12 and state.max_steps == 18
    assert state.continuation_segments == 2
    assert final_answer(events)["stop_reason"] == "incomplete_requirements"
    assert final_answer(events)["outcome"] == "partial"
    assert_plan_incomplete(events)


def test_repeated_identical_segments_pause_with_recoverable_checkpoint(db, monkeypatch):
    class RepeatingModel:
        def stream_chat(self, messages, tools=None):
            yield "final", LLMResponse("", [{"id": "same", "name": "list_papers", "arguments": {}}])

    model = RepeatingModel()
    monkeypatch.setattr(loop.settings, "max_agent_steps", 1)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **k: json.dumps({"same": "result"}))
    saved = []
    events = list(drive(db, run_id="stalled_run", save_checkpoint=lambda state: saved.append(deepcopy(state))))
    assert sum(kind == "status" and "自动继续" in event["text"] for kind, event in events) == 2
    assert sum(kind == "paused" for kind, _ in events) == 1
    state = AgentState.model_validate(saved[-1])
    assert state.phase == "paused" and state.budget_exhausted
    assert state.step == state.max_steps == 3
    assert state.no_progress_segments == 2
    assert db.query(QARecord).count() == 0


def test_cancel_during_summary_closes_stream_and_resume_never_replays_tools(db, monkeypatch):
    model, completed = setup_model(monkeypatch, "cancel", "final_cancel")
    state = AgentState(question="读取文件夹全部内容", conversation_id="c_finalization", phase="summarize",
        step=6, max_steps=6, budget_exhausted=True,
        messages=[{"role": "user", "content": "question"}],
        todo=[{"title": "读取文件", "phase": "read", "status": "in_progress"},
              {"title": "核实文件", "phase": "audit", "status": "pending"}])
    saved = []
    events = list(drive(db, run_id="final_cancel", checkpoint=state.snapshot(),
                        save_checkpoint=lambda s: saved.append(deepcopy(s))))
    assert any(kind == "cancelled" for kind, _ in events)
    assert not any(kind == "answer" for kind, _ in events)
    assert model.closed == 1 and not completed
    assert saved[-1]["phase"] == "summarize"
    cancel_store.clear("final_cancel")
    model.outcome = "valid"
    events = list(drive(db, run_id="final_cancel", checkpoint=saved[-1]))
    assert not completed and len(model.calls) == 2
    assert final_answer(events)["budget_exhausted"] is True
    assert db.query(QARecord).count() == 1


@pytest.mark.parametrize("crash_phase", ["summarize", "finalize", "deliver"])
def test_durable_summary_boundaries_reuse_tools_and_completed_summary(db, monkeypatch, crash_phase):
    model, completed = setup_model(monkeypatch)
    state = AgentState(question="读取文件夹全部内容", conversation_id="c_finalization", phase="summarize",
        step=6, max_steps=6, budget_exhausted=True,
        messages=[{"role": "user", "content": "question"}],
        todo=[{"title": "读取文件", "phase": "read", "status": "in_progress"},
              {"title": "核实文件", "phase": "audit", "status": "pending"}])
    saved = []
    def persist(snapshot):
        saved.append(deepcopy(snapshot))
        db.commit()
        if snapshot["phase"] == crash_phase:
            raise PowerLoss()
    with pytest.raises(PowerLoss):
        list(drive(db, checkpoint=state.snapshot(), save_checkpoint=persist))
    calls_before = len(model.calls)
    events = list(drive(db, checkpoint=saved[-1]))
    assert not completed
    assert len(model.calls) == calls_before + (1 if crash_phase == "summarize" else 0)
    answer = final_answer(events)
    assert answer["answer"].count(LIMIT_SUMMARY_NOTE) == 1
    assert db.query(QARecord).count() == 1
    assert_plan_incomplete(events)


def test_legacy_limit_summary_still_enforces_citations_without_another_rewrite(db, monkeypatch):
    model, _ = setup_model(monkeypatch)
    monkeypatch.setattr(loop.settings, "llm_api_key", "synthetic-for-test")
    calls = []
    def gate(text, ids):
        calls.append((text, ids))
        return {"passed": False, "unsupported": [], "invalid_markers": ["missing"],
                "total_claims": 1, "marked_claims": 0, "grounded_ratio": 0.0}
    monkeypatch.setattr(loop, "citation_gate", gate)
    state = AgentState(question="读取文件夹全部内容", conversation_id="c_finalization", phase="summarize",
        step=6, max_steps=6, evidence=[{"id": "e1", "quote": "result"}],
        budget_exhausted=True,
        todo=[{"title": "Reading", "phase": "read", "status": "in_progress"},
              {"title": "Audit", "phase": "audit", "status": "pending"}],
        messages=[{"role": "user", "content": "question"}])
    events = list(drive(db, checkpoint=state.snapshot()))
    assert len(model.calls) == 1 and model.calls[0]["tools"] is None
    assert calls and calls[0][1] == ["e1"]
    assert final_answer(events)["refusal"] == 1
    assert_plan_incomplete(events)


def test_existing_checkpoint_retains_budget_and_new_fields_are_backwards_compatible():
    legacy = {"question": "test", "max_steps": 6, "phase": "model"}
    restored = AgentState.model_validate(legacy)
    assert restored.max_steps == 6
    assert restored.budget_exhausted is False and restored.summary_failed is False
    assert AgentState(question="test").max_steps == 6


def test_plan_does_not_complete_unread_sources_after_an_answer(db, monkeypatch):
    class FinalModel:
        def stream_chat(self, messages, tools=None):
            yield "final", LLMResponse("已核实本轮结果。")
    monkeypatch.setattr(loop, "get_llm", lambda: FinalModel())
    # A real saved/model-created plan must remain incomplete without proof;
    # requests no longer get an unconditional research plan before inference.
    state = AgentState(question="读取文件夹全部内容", conversation_id="c_finalization", phase="model",
        todo=[{"title": "读取文件", "phase": "read", "status": "pending"},
              {"title": "核实其余文件", "phase": "audit", "status": "pending"},
              {"title": "定稿交付", "phase": "final", "status": "pending"}])
    events = list(drive(db, checkpoint=state.snapshot()))
    initial_plan = next(event for kind, event in events if kind == "plan")
    assert all(step["status"] == "pending" for step in initial_plan["steps"])
    final_plan = [event for kind, event in events if kind == "todo_update"][-1]
    assert any(step["status"] != "done" for step in final_plan["steps"])
    assert final_answer(events)["outcome"] == "partial"
    saved_plan = next(event for event in db.query(QARecord).one().trace if event["kind"] == "plan")
    assert saved_plan == initial_plan


def test_background_limit_preserves_paused_checkpoint_and_progress_after_restart(db, monkeypatch):
    model, completed = setup_model(monkeypatch)
    model.tool_limit = 6
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(task_runner.notifier, "notify", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "notify_tasks_changed", lambda: None)
    task = TaskRecord(type="agent", title="读取文件夹全部内容", conversation_id="c_finalization",
                      payload={"question": "读取文件夹全部内容", "skills": []}, status="pending", events=[])
    db.add(task)
    db.commit()
    task_runner._run_in_background(task.id, task.title, None, [], task.conversation_id)
    db.refresh(task)
    assert len(model.calls) == 8 and len(completed) == 6
    assert task.status == "done"
    assert task.progress < 1
    assert task.error == ""
    assert task.result["stop_reason"] == "incomplete_requirements"
    assert task.result["outcome"] == "partial"
    assert task.checkpoint["budget_exhausted"] is False
    assert task.checkpoint["phase"] == "completed"
    assert task.checkpoint["plan_source"] == "requirements"
    assert task.checkpoint["todo"] and task.plan == []
    assert not any(event["kind"] in {"plan", "todo_update"} for event in task.checkpoint["trace"])
    assert task.result["plan_steps"] == [] and task.result["incomplete_steps"]
    progress, result = task.progress, deepcopy(task.result)
    # Completed automatic continuations remain completed after restart.
    task.status = "running"
    db.commit()
    task_runner.recover_interrupted_tasks(db)
    db.refresh(task)
    assert task.status == "done" and task.progress == progress
    assert task.result == result
