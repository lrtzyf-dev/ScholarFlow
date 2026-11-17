"""Budget pauses remain resumable without replaying saved operations."""
import threading
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import task_runner
from app.agent.checkpoint import AgentState
from app.main import app
from app.models import TaskRecord


def paused_snapshot(*, max_steps=6, operations=None):
    return AgentState(
        question="Read the project files",
        conversation_id="c_budget_resume",
        phase="paused",
        step=max_steps,
        max_steps=max_steps,
        budget_exhausted=True,
        messages=[
            {"role": "user", "content": "Read the project files"},
            {"role": "assistant", "content": "Saved model context"},
        ],
        todo=[{"title": "Read files", "phase": "read", "status": "in_progress"}],
        operations=operations or {"tool/0/0": {"text": "saved read result"}},
        trace=[{"kind": "tool_result", "text": "saved read result"}],
    ).snapshot()


def add_task(db, checkpoint, *, status="paused", result=None):
    task = TaskRecord(
        type="agent",
        title=checkpoint["question"],
        conversation_id="c_budget_resume",
        payload={"question": checkpoint["question"], "skills": [], "workspace": None},
        checkpoint=deepcopy(checkpoint),
        result=deepcopy(result),
        status=status,
        events=[],
        plan=deepcopy(checkpoint.get("todo")),
        progress=0.25,
    )
    db.add(task)
    db.commit()
    return task


class ScheduledThread:
    def __init__(self, *, target, args, daemon, name):
        self.target, self.args, self.daemon, self.name = target, args, daemon, name

    def start(self):
        scheduled.append(self)


@pytest.fixture(autouse=True)
def isolate_resume(monkeypatch):
    global scheduled
    scheduled = []
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(task_runner.threading, "Thread", ScheduledThread)


def test_resume_budget_pause_adds_default_budget_and_preserves_checkpoint_work(db):
    operations = {"tool/0/0": {"text": "already read"}, "tool/1/0": {"text": "second saved result"}}
    before = paused_snapshot(operations=operations)
    task = add_task(db, before)
    client = TestClient(app)

    response = client.post(f"/api/tasks/{task.id}/resume")

    assert response.status_code == 200, response.text
    db.refresh(task)
    resumed = AgentState.model_validate(task.checkpoint)
    assert task.status == "pending"
    assert resumed.phase == "model"
    assert resumed.step == 6 and resumed.max_steps == 12
    assert resumed.budget_exhausted is False
    assert resumed.summary_failed is False
    assert resumed.answer == "" and resumed.result is None
    assert resumed.messages == before["messages"]
    assert resumed.todo == before["todo"]
    assert resumed.operations == operations
    assert resumed.record_id == before["record_id"]
    assert task.result is None
    assert task.progress == 0.25
    assert "追加 6 轮预算" in task.events[-1]["text"]
    assert len(scheduled) == 1

    # A duplicate click while the worker is registered must not add budget twice.
    repeated = client.post(f"/api/tasks/{task.id}/resume", json={"additional_steps": 24})
    assert repeated.status_code == 200
    db.refresh(task)
    assert AgentState.model_validate(task.checkpoint).max_steps == 12
    assert len(scheduled) == 1


def test_background_executor_pause_is_persisted_as_paused_task(db, monkeypatch):
    state = AgentState(
        question="Read the project files",
        conversation_id="c_budget_resume",
        phase="paused",
        step=6,
        max_steps=6,
        budget_exhausted=True,
    )
    task = add_task(db, state.snapshot(), status="pending")

    def fake_run_agent(_db, *_args, save_checkpoint, **_kwargs):
        save_checkpoint(state.snapshot())
        yield "paused", {"text": "执行预算已用完，检查点已保存"}

    monkeypatch.setattr(task_runner, "run_agent", fake_run_agent)
    monkeypatch.setattr(task_runner.notifier, "notify", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "notify_tasks_changed", lambda: None)
    task_runner._run_in_background(task.id, task.title, None, [], task.conversation_id)

    db.refresh(task)
    assert task.status == "paused"
    assert task.checkpoint["phase"] == "paused"
    assert task.error and "可追加预算后继续" in task.error
    assert task.events[-1]["kind"] == "paused"


def test_resume_budget_is_finite_per_request_and_accumulates(db):
    task = add_task(db, paused_snapshot(max_steps=120))
    client = TestClient(app)

    invalid = client.post(f"/api/tasks/{task.id}/resume", json={"additional_steps": 25})
    assert invalid.status_code == 422
    db.refresh(task)
    assert AgentState.model_validate(task.checkpoint).phase == "paused"

    response = client.post(f"/api/tasks/{task.id}/resume", json={"additional_steps": 24})
    assert response.status_code == 200, response.text
    db.refresh(task)
    resumed = AgentState.model_validate(task.checkpoint)
    assert resumed.max_steps == 144
    assert resumed.step == 120
    assert "追加 24 轮预算" in task.events[-1]["text"]


@pytest.mark.parametrize("value", [0, -1, "6", 1.5])
def test_resume_rejects_invalid_budget_sizes(db, value):
    task = add_task(db, paused_snapshot())
    response = TestClient(app).post(
        f"/api/tasks/{task.id}/resume", json={"additional_steps": value}
    )
    assert response.status_code == 422
    db.refresh(task)
    assert AgentState.model_validate(task.checkpoint).phase == "paused"
    assert not scheduled


def test_regular_interrupted_checkpoint_resume_does_not_change_budget(db):
    state = AgentState(
        question="Read the project files",
        conversation_id="c_budget_resume",
        phase="model",
        step=3,
        max_steps=9,
        messages=[{"role": "user", "content": "Read the project files"}],
    ).snapshot()
    task = add_task(db, state, status="interrupted")

    response = TestClient(app).post(
        f"/api/tasks/{task.id}/resume", json={"additional_steps": 24}
    )

    assert response.status_code == 200, response.text
    db.refresh(task)
    resumed = AgentState.model_validate(task.checkpoint)
    assert resumed.phase == "model"
    assert resumed.step == 3 and resumed.max_steps == 9


def test_startup_reconciles_paused_checkpoint_and_legacy_completed_budget(db):
    paused = add_task(db, paused_snapshot(), status="running")
    legacy_state = AgentState(
        question="Legacy partial run",
        conversation_id="c_budget_resume",
        phase="completed",
        step=6,
        max_steps=6,
        budget_exhausted=True,
        result={"answer": "partial", "budget_exhausted": True},
    ).snapshot()
    legacy = add_task(db, legacy_state, status="running", result=legacy_state["result"])

    task_runner.recover_interrupted_tasks(db)
    db.refresh(paused)
    db.refresh(legacy)

    assert paused.status == "paused"
    assert legacy.status == "paused"
    assert task_runner.task_to_dict(paused)["recovery"]["available"]
    assert task_runner.task_to_dict(legacy)["recovery"]["available"]
    assert paused.checkpoint["phase"] == "paused"


def test_legacy_completed_budget_checkpoint_can_resume(db):
    legacy_state = AgentState(
        question="Legacy partial run",
        conversation_id="c_budget_resume",
        phase="completed",
        step=6,
        max_steps=6,
        budget_exhausted=True,
        result={"answer": "partial", "budget_exhausted": True},
    ).snapshot()
    task = add_task(db, legacy_state, status="paused", result=legacy_state["result"])

    response = TestClient(app).post(f"/api/tasks/{task.id}/resume")

    assert response.status_code == 200, response.text
    db.refresh(task)
    resumed = AgentState.model_validate(task.checkpoint)
    assert resumed.phase == "model"
    assert resumed.max_steps == 12
    assert not resumed.budget_exhausted
    assert task.result is None
