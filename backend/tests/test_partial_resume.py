"""Partial delivery continuation changes QA identity while preserving prior work."""
from copy import deepcopy
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import run_lifecycle, run_store, task_runner
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.api import chat
from app.foreground_run_store import finish, save_agent_checkpoint
from app.llm import LLMResponse
from app.main import app
from app.models import Conversation, ForegroundRun, QARecord, TaskRecord, ModelCredential
from app.runtime_models import RunAttempt


class ScheduledThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
    def start(self):
        pass


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(run_store, "_active", {})
    monkeypatch.setattr(run_store, "_finished", {})
    monkeypatch.setattr(run_store, "_active_attempts", {})
    monkeypatch.setattr(run_store, "_finished_attempts", {})
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(task_runner.notifier, "notify", lambda *a, **kw: None)


def partial(question="Original request", cid="c_partial_resume"):
    return AgentState(question=question, conversation_id=cid, phase="completed", step=1,
        answer="A is complete. B needs access.",
        result={"answer": "A is complete. B needs access.", "completed": False,
                "outcome": "partial", "incomplete_steps": [{"title": "Read B", "phase": "search"}]},
        messages=[{"role": "user", "content": question},
                  {"role": "assistant", "content": None, "tool_calls": [{"id": "read_a", "type": "function",
                    "function": {"name": "list_papers", "arguments": "{\"tag\":\"A\"}"}}]},
                  {"role": "tool", "tool_call_id": "read_a", "content": "{\"read\":\"A\"}"}],
        todo=[{"title": "Read A", "phase": "search", "status": "done", "tool_name": "list_papers", "tool_arguments": {"tag": "A"},
               "evidence": [{"kind": "tool_result", "tool_name": "list_papers", "arguments": {"tag": "A"}, "status": "completed"}]},
              {"title": "Read B", "phase": "search", "status": "pending", "tool_name": "list_papers", "tool_arguments": {"tag": "B"}}],
        operations={"tool/0/0": {"result": "{\"read\":\"A\"}", "executed": True, "error": ""}},
        usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10})


def add_task(db, state):
    row = TaskRecord(type="agent", title=state.question, conversation_id=state.conversation_id,
        status="done", payload={"question": state.question, "skills": [], "workspace": None},
        result=deepcopy(state.result), checkpoint=state.snapshot(), events=[])
    db.add(row)
    db.commit()
    return row


def test_partial_continuation_only_does_missing_work_and_keeps_both_qa_rows(db, monkeypatch):
    state = partial()
    task = add_task(db, state)
    old = QARecord(id=state.record_id, question=state.question, conversation_id=state.conversation_id,
                   answer=state.answer, usage=deepcopy(state.usage))
    db.add(old)
    db.commit()
    monkeypatch.setattr(task_runner.threading, "Thread", ScheduledThread)
    task_runner.resume_task(db, task.id, resume_request_id="partial_resume_001")
    new_record = task.checkpoint["record_id"]
    assert new_record != old.id
    seen, dispatched = [], []

    class Model:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            if len(seen) == 1:
                yield "final", LLMResponse("", [{"id": "read_b", "name": "list_papers", "arguments": {"tag": "B"}}],
                    usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4})
            else:
                yield "final", LLMResponse("A and B are now complete.", usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": "list_papers", "parameters": {
        "type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]}}])
    monkeypatch.setattr(loop, "execute_tool", lambda db, name, args, **kw: dispatched.append(args["tag"]) or json.dumps({"read": args["tag"]}))
    task_runner._run_in_background(task.id, state.question, None, [], state.conversation_id)
    db.refresh(task)
    db.refresh(old)
    assert dispatched == ["B"]
    assert any(message.get("tool_call_id") == "read_a" for message in seen[0])
    assert old.answer == state.answer and old.usage == state.usage
    assert task.status == "done" and task.result["completed"] is True
    newer = db.get(QARecord, new_record)
    assert newer and newer.answer == "A and B are now complete."
    assert newer.usage["total_tokens"] == 10
    assert task.result["cumulative_usage"]["total_tokens"] == 20
    assert db.query(QARecord).count() == 2
    assert db.query(RunAttempt).count() == 2


def test_foreground_partial_is_listed_and_resume_request_is_idempotent(db, monkeypatch):
    state = partial(cid="c_front_partial")
    run_id = "chat_front_partial_001"
    row = ForegroundRun(id=run_id, conversation_id=state.conversation_id, question=state.question,
        status="completed", skills=[], images=[], phase="completed", agent_checkpoint=state.snapshot())
    db.add(row)
    db.commit()
    client = TestClient(app)
    listed = client.get("/api/foreground-runs", params={"conversation_id": state.conversation_id}).json()["runs"]
    assert listed[0]["resume_available"] and listed[0]["resume_kind"] == "continue_missing"
    captured = []

    def fake_agent(db, *args, checkpoint, run_id, **kw):
        resumed = AgentState.model_validate(checkpoint)
        captured.append(resumed)
        resumed.result = {"completed": True, "outcome": "completed", "answer": "Now complete.", "incomplete_steps": []}
        resumed.phase = "completed"
        finish(db, run_id, "completed", attempt_id=resumed.attempt_id, writer_token=resumed.writer_token)
        save_agent_checkpoint(db, run_id, resumed.snapshot())
        yield "answer", resumed.result
        yield "done", {"text": "Done"}

    monkeypatch.setattr(chat, "run_agent", fake_agent)
    request = {"question": state.question, "conversation_id": state.conversation_id, "run_id": run_id,
               "resume_checkpoint": True, "resume_request_id": "front_partial_retry"}
    first = client.post("/api/chat/stream", json=request)
    assert first.status_code == 200, first.text
    repeated = client.post("/api/chat/stream", json=request)
    assert repeated.status_code == 200, repeated.text
    assert len(captured) == 1 and captured[0].record_id != state.record_id
    assert db.query(RunAttempt).count() == 2


def test_resume_rejects_mismatched_task_identity_before_launch(db, monkeypatch):
    state = partial()
    task = add_task(db, state)
    task.payload = {**task.payload, "question": "Another user's request"}
    db.commit()
    monkeypatch.setattr(task_runner.threading, "Thread", ScheduledThread)
    with pytest.raises(task_runner.TaskConflict, match="不匹配"):
        task_runner.resume_task(db, task.id)
    db.rollback()
    assert db.query(RunAttempt).count() == 0


def test_delivery_crash_replays_same_qa_without_model_calls_and_freezes_old_attempt(db, monkeypatch):
    state = partial()
    state.phase = "deliver"
    task = add_task(db, state)
    old = QARecord(id=state.record_id, question=state.question, conversation_id=state.conversation_id,
                   answer=state.answer, usage=deepcopy(state.usage))
    db.add(old)
    db.commit()
    monkeypatch.setattr(task_runner.threading, "Thread", ScheduledThread)
    task_runner.resume_task(db, task.id, resume_request_id="replay_delivery_001")
    attempt_id = task.checkpoint["attempt_id"]

    class NoModelCalls:
        def stream_chat(self, *args, **kw):
            raise AssertionError("delivery replay must not call a model")

    monkeypatch.setattr(loop, "get_llm", lambda: NoModelCalls())
    task_runner._run_in_background(task.id, state.question, None, [], state.conversation_id)
    db.refresh(task)
    assert task.status == "done" and task.checkpoint["phase"] == "completed"
    assert task.checkpoint["record_id"] == state.record_id
    assert task.checkpoint["attempt_id"] == attempt_id
    assert task.checkpoint["delivery_only"] is True and task.checkpoint["source_attempt_id"]
    assert db.query(QARecord).count() == 1 and db.query(RunAttempt).count() == 2
    task_runner.resume_task(db, task.id, resume_request_id="replay_delivery_001")
    assert db.query(RunAttempt).count() == 2


@pytest.mark.parametrize("execution_mode,outcome", [("execute", "completed"), ("plan", "planned")])
def test_completed_or_planned_delivery_cannot_be_misclassified_as_partial(db, monkeypatch, execution_mode, outcome):
    state = partial()
    state.execution_mode = execution_mode
    state.result = {"completed": outcome == "completed", "outcome": outcome, "incomplete_steps": []}
    task = add_task(db, state)
    monkeypatch.setattr(task_runner.threading, "Thread", ScheduledThread)
    with pytest.raises(task_runner.TaskConflict):
        task_runner.resume_task(db, task.id)
    db.rollback()
    assert db.query(RunAttempt).count() == 0


def test_resume_keeps_bound_model_and_rejects_revoked_credentials(db, monkeypatch):
    from app import model_profiles
    from app.timeutil import utcnow
    state = partial()
    profile = model_profiles.save_profile(db, {"name": "Synthetic only", "base_url": "https://synthetic.invalid/v1",
        "model_id": "Exact-Case-Model", "api_key": "Synthetic-Test-Key"})
    model_profiles.set_default(db, profile.id)
    db.flush()
    state.runtime_config = model_profiles.capture_run_config(db, state.conversation_id)
    assert state.runtime_config["model_id"] == "Exact-Case-Model"
    db.commit()
    task = add_task(db, state)
    credential = db.get(ModelCredential, profile.credential_ref)
    credential.revoked_at = utcnow()
    db.commit()
    monkeypatch.setattr(task_runner.threading, "Thread", ScheduledThread)
    with pytest.raises(task_runner.TaskConflict):
        task_runner.resume_task(db, task.id)
    db.rollback()
    assert db.query(RunAttempt).count() == 0
    assert task.checkpoint["runtime_config"]["model_id"] == "Exact-Case-Model"


def test_failed_foreground_admission_commit_releases_memory_registration(db, monkeypatch):
    from app.db import SessionLocal
    run_id = "chat_admission_rollback"
    row = ForegroundRun(id=run_id, conversation_id="c_admission_rollback", question="Explain", skills=[], images=[])
    db.add(row)
    db.commit()

    def failing_session():
        session = SessionLocal()
        def fail_commit():
            raise RuntimeError("synthetic commit failure")
        session.commit = fail_commit
        return session

    monkeypatch.setattr(chat, "SessionLocal", failing_session)
    from app.schemas import ChatRequest
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        chat.chat_stream(ChatRequest(question=row.question, conversation_id=row.conversation_id, run_id=run_id))
    assert run_id not in run_store.active_runs()
    db.expire_all()
    assert db.query(RunAttempt).count() == 0
    assert db.get(ForegroundRun, run_id).started_at is None


def test_foreground_delivery_replay_commits_completion_and_keeps_original_qa(db, monkeypatch):
    state = partial(cid="c_front_delivery_replay")
    state.phase = "deliver"
    state.result["qa_id"] = state.record_id
    run_id = "chat_front_delivery_replay"
    row = ForegroundRun(id=run_id, conversation_id=state.conversation_id, question=state.question,
        status="completed", skills=[], images=[], phase="deliver", agent_checkpoint=state.snapshot())
    old = QARecord(id=state.record_id, question=state.question, conversation_id=state.conversation_id,
                   answer=state.answer, usage=deepcopy(state.usage))
    db.add_all([row, old])
    db.commit()

    class NoModelCalls:
        def stream_chat(self, *args, **kw):
            raise AssertionError("delivery replay must not call a model")

    monkeypatch.setattr(loop, "get_llm", lambda: NoModelCalls())
    result = TestClient(app).post("/api/chat/stream", json={
        "question": state.question, "conversation_id": state.conversation_id, "run_id": run_id,
        "resume_checkpoint": True, "resume_request_id": "front_delivery_replay_001"})
    assert result.status_code == 200, result.text
    assert "event: answer" in result.text and "event: error" not in result.text
    db.refresh(row)
    assert row.status == "completed" and row.agent_checkpoint["delivery_only"] is True
    assert row.agent_checkpoint["record_id"] == old.id
    assert db.query(QARecord).count() == 1 and db.query(RunAttempt).count() == 2


@pytest.mark.parametrize("recover", [True, False])
def test_background_tool_error_uses_actual_recovery_or_explicit_partial_receipts(db, monkeypatch, recover):
    state = partial()
    state.phase = "model"
    state.result = None
    task = add_task(db, state)
    task.status = "pending"
    db.commit()

    requests, tools = [], []
    class Model:
        def stream_chat(self, messages, **kwargs):
            requests.append(deepcopy(messages))
            if len(requests) <= (2 if recover else 1):
                yield "final", LLMResponse("", [{"id": f"read_b_{len(requests)}", "name": "list_papers",
                                                 "arguments": {"tag": "B"}}])
            else:
                yield "final", LLMResponse("Retry succeeded." if recover else "Source B remains unavailable.")

    def invoke(db, name, args, **kwargs):
        tools.append(deepcopy(args))
        if len(tools) == 1:
            raise OSError("Synthetic source temporarily unavailable")
        return json.dumps({"read": "B"})

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [{"name": "list_papers", "parameters": {"type": "object"}}])
    monkeypatch.setattr(loop, "execute_tool", invoke)
    task_runner._run_in_background(task.id, state.question, None, [], state.conversation_id)
    db.refresh(task)
    assert task.status == "done" and not task.error and task.result["completed"] is recover
    assert len(tools) == (2 if recover else 1)
    failure = task.checkpoint["tool_failures"][0]
    assert failure["status"] == ("resolved" if recover else "open")
    assert any(event.get("failure_id") == failure["id"] and event["source"] == "tool" for event in task.events)
    assert db.get(QARecord, task.result["qa_id"]).answer == task.result["answer"]


def test_unknown_stop_hook_result_keeps_saved_qa_but_blocks_next_turn(db, monkeypatch):
    from app.next_turn_dispatch import _complete_reason
    from app.runtime_models import RuntimeRun
    run_id, cid = "chat_unknown_stop_hook", "c_unknown_stop_hook"
    client = TestClient(app)
    assert client.post("/api/foreground-runs", json={"id": run_id, "conversation_id": cid, "question": "Explain"}).status_code == 200

    class Model:
        def stream_chat(self, *args, **kw):
            yield "final", LLMResponse("已形成回答。")

    def hooks(name, *args, **kw):
        if name == "Stop":
            raise RuntimeError("Synthetic unknown hook result")
        return {"ran": 0, "blocked": False, "feedback": ""}

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop.hooks_store, "run_hooks", hooks)
    response = client.post("/api/chat/stream", json={"question": "Explain", "conversation_id": cid, "run_id": run_id})
    assert response.status_code == 200 and "event: error" in response.text
    db.expire_all()
    row = db.get(ForegroundRun, run_id)
    assert row.status == "failed" and row.agent_checkpoint["phase"] == "deliver"
    assert row.agent_checkpoint["pending_operation"]["name"] == "Stop"
    assert db.query(QARecord).count() == 1
    assert db.get(RuntimeRun, run_id).status == "failed" and _complete_reason(db, run_id)
