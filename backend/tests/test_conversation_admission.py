"""Transactions reserve one writer per conversation across all launch paths."""
import threading

import pytest
from fastapi import HTTPException

from app import run_lifecycle, run_store, task_runner
from app.api import chat, foreground_runs
from app.db import SessionLocal
from app.models import Conversation, ForegroundRun, TaskRecord
from app.runtime_models import RuntimeRun
from app.schemas import ChatRequest


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(run_store, "_active", {})
    monkeypatch.setattr(run_store, "_finished", {})
    monkeypatch.setattr(run_store, "_active_attempts", {})
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(task_runner, "_launch", lambda db, task: task_runner.task_to_dict(task))


def create(db, cid="c_single_writer", run_id="chat_reservation_001"):
    return foreground_runs.create_run(foreground_runs.CreateRun(
        id=run_id, conversation_id=cid, question="Explain"), db)


def test_child_reservation_blocks_other_window_and_allows_only_its_own_start(db, monkeypatch):
    create(db, run_id="chat_next_reserved_child")
    with pytest.raises(HTTPException) as failure:
        create(db, run_id="chat_other_window_001")
    assert failure.value.status_code == 409
    db.rollback()
    with pytest.raises(task_runner.TaskConflict):
        task_runner.start_agent_task(db, "Other task", None, [], "c_single_writer")
    db.rollback()

    class ScheduledThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            pass

    monkeypatch.setattr(chat.threading, "Thread", ScheduledThread)
    response = chat.chat_stream(ChatRequest(question="Explain", conversation_id="c_single_writer",
                                            run_id="chat_next_reserved_child"))
    assert response.status_code == 200
    assert run_store.active_for_conversation("c_single_writer") == ["chat_next_reserved_child"]
    run_store.finish("chat_next_reserved_child", "cancelled")


def test_cross_window_foreground_and_background_admission_are_atomic(db):
    barrier, admitted, denied, errors = threading.Barrier(2), [], [], []

    def launch(kind):
        try:
            barrier.wait(timeout=3)
            with SessionLocal() as session:
                try:
                    if kind == "foreground":
                        create(session)
                    else:
                        task_runner.start_agent_task(session, "Explain", None, [], "c_single_writer")
                    admitted.append(kind)
                except (HTTPException, task_runner.TaskConflict) as exc:
                    session.rollback()
                    denied.append(exc.status_code)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=launch, args=(kind,)) for kind in ("foreground", "background")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert len(admitted) == 1 and denied == [409]
    assert db.query(ForegroundRun).count() + db.query(TaskRecord).count() == 1


@pytest.mark.parametrize("status", ["paused", "cancelled", "failed", "interrupted", "completed"])
def test_inactive_or_partial_foreground_does_not_block_a_new_request(db, status):
    db.add(ForegroundRun(id="chat_old_inactive_001", conversation_id="c_single_writer", question="Prior",
                          status=status, agent_checkpoint={"result": {"completed": False}}))
    db.commit()
    assert create(db)["status"] == "running"


def test_another_conversation_can_run_in_parallel(db):
    assert create(db)["status"] == "running"
    assert create(db, cid="c_parallel_writer", run_id="chat_parallel_writer_001")["status"] == "running"
    assert db.query(ForegroundRun).count() == 2


def test_orphan_runtime_is_retired_but_active_memory_worker_keeps_ownership(db):
    attempt, _ = run_lifecycle.admit_attempt(db, "chat_orphan_active", "c_single_writer", "Old request")
    db.commit()
    assert run_store.begin("chat_orphan_active", "c_single_writer", attempt_id=attempt.id)
    with pytest.raises(HTTPException):
        create(db)
    db.rollback()
    assert db.get(RuntimeRun, "chat_orphan_active").status == "queued"
    run_store.finish("chat_orphan_active", "failed", attempt_id=attempt.id)
    assert create(db)["status"] == "running"
    db.expire_all()
    assert db.get(RuntimeRun, "chat_orphan_active").status == "interrupted"
