"""Attempt admission, frozen deliveries and stale executor fencing."""
from copy import deepcopy
import threading

import pytest
from sqlalchemy import text

from app import run_lifecycle, run_store
from app.agent.checkpoint import AgentState, delivery_usage, prepare_resume, recovery_info
from app.runtime_models import RuntimeRun, RunAttempt


def partial_state(**changes):
    values = dict(question="Original request", conversation_id="c_attempts", phase="completed",
                  answer="A is complete; B needs access.",
                  result={"completed": False, "outcome": "partial", "answer": "A is complete; B needs access.",
                          "incomplete_steps": [{"title": "B", "phase": "read"}]},
                  usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
                  messages=[{"role": "user", "content": "Original request"}],
                  operations={"tool/0/0": {"result": "A", "executed": True}})
    values.update(changes)
    return AgentState(**values)


def test_partial_delivery_creates_new_answer_identity_without_losing_receipts():
    old = partial_state()
    before = old.snapshot()
    prepared, new_work = prepare_resume(before, 4)
    assert new_work and prepared.phase == "model"
    assert prepared.record_id != old.record_id and prepared.previous_record_id == old.record_id
    assert prepared.operations == old.operations
    assert prepared.usage == old.usage and delivery_usage(prepared)["total_tokens"] == 0
    assert prepared.messages[-2]["content"] == old.answer
    assert "B" in prepared.messages[-1]["content"]
    assert old.snapshot() == before
    prepared.usage["total_tokens"] += 6
    assert delivery_usage(prepared)["total_tokens"] == 6


@pytest.mark.parametrize("completed,outcome,missing,available", [
    (True, "completed", [], False), (False, "partial", [{"title": "B"}], True),
    (False, "blocked", [{"title": "B"}], True), (False, "partial", [], False),
    (False, "planned", [{"title": "B"}], False),
])
def test_recovery_uses_structured_result_not_delivery_phase(completed, outcome, missing, available):
    state = partial_state(result={"completed": completed, "outcome": outcome, "incomplete_steps": missing},
                          execution_mode="plan" if outcome == "planned" else "execute")
    assert recovery_info(state.snapshot())["available"] is available


def test_admission_archives_legacy_partial_and_fences_both_old_and_unbound_writers(db):
    old_state = partial_state()
    before = old_state.snapshot()
    attempt, created = run_lifecycle.admit_attempt(db, "chat_attempt_001", "c_attempts", old_state.question,
        checkpoint=before, resume=True, request_id="resume_001")
    db.commit()
    assert created and attempt.number == 2
    archived = db.query(RunAttempt).filter_by(run_id="chat_attempt_001", number=1).one()
    assert archived.checkpoint == before and archived.result == old_state.result
    for snapshot in (before, {**before, "attempt_id": archived.id, "writer_token": archived.writer_token}):
        with pytest.raises(run_lifecycle.StaleAttempt):
            run_lifecycle.save_checkpoint(db, "chat_attempt_001", snapshot)
        db.rollback()
    again, created = run_lifecycle.admit_attempt(db, "chat_attempt_001", "c_attempts", old_state.question,
        checkpoint=before, resume=True, request_id="resume_001", expected_revision=1)
    assert not created and again.id == attempt.id
    assert db.query(RunAttempt).count() == 2


@pytest.mark.parametrize("prior_status", ["ended", "failed", "stopped", "interrupted"])
def test_delivery_recovery_uses_new_attempt_and_freezes_terminal_source(db, prior_status):
    attempt, _ = run_lifecycle.admit_attempt(db, "chat_delivery_001", "c_attempts", "Original request")
    snapshot = run_lifecycle.bind_checkpoint(partial_state(phase="deliver").snapshot(), attempt)
    run_lifecycle.save_checkpoint(db, "chat_delivery_001", snapshot)
    run_lifecycle.finish_attempt(db, "chat_delivery_001", attempt.id, attempt.writer_token, prior_status)
    db.commit()
    old_token, old_id = attempt.writer_token, attempt.id
    frozen = {key: deepcopy(getattr(attempt, key)) for key in
              ("status", "result", "usage", "ended_at", "writer_token", "checkpoint")}
    prepared, new_work = prepare_resume(snapshot)
    resumed, created = run_lifecycle.admit_attempt(db, "chat_delivery_001", "c_attempts", "Original request",
        checkpoint=snapshot, resume=True, request_id="delivery_retry")
    db.commit()
    assert created and resumed.id != old_id and resumed.writer_token != old_token
    assert resumed.checkpoint["delivery_only"] is True and resumed.checkpoint["source_attempt_id"] == old_id
    db.refresh(attempt)
    assert {key: getattr(attempt, key) for key in frozen} == frozen
    assert prepared.phase == "deliver" and prepared.record_id == snapshot["record_id"] and not new_work
    with pytest.raises(run_lifecycle.StaleAttempt):
        run_lifecycle.assert_writer(db, "chat_delivery_001", old_id, old_token)


def test_revision_mismatch_does_not_create_attempt(db):
    attempt, _ = run_lifecycle.admit_attempt(db, "chat_revision_001", "c_attempts", "Original request")
    db.commit()
    with pytest.raises(run_lifecycle.RunConflict):
        run_lifecycle.admit_attempt(db, "chat_revision_001", "c_attempts", "Original request",
                                    resume=True, expected_revision=999)
    db.rollback()
    assert db.query(RunAttempt).count() == 1
    assert db.get(RuntimeRun, "chat_revision_001").current_attempt_id == attempt.id


def test_old_memory_cleanup_cannot_remove_a_new_attempt(monkeypatch):
    monkeypatch.setattr(run_store, "_active", {})
    monkeypatch.setattr(run_store, "_finished", {})
    monkeypatch.setattr(run_store, "_active_attempts", {})
    monkeypatch.setattr(run_store, "_finished_attempts", {})
    assert run_store.begin("chat_memory_001", "c_attempts", attempt_id="old")
    run_store.finish("chat_memory_001", "done", attempt_id="old")
    assert not run_store.begin("chat_memory_001", "c_attempts", attempt_id="old")
    assert run_store.begin("chat_memory_001", "c_attempts", attempt_id="new")
    run_store.finish("chat_memory_001", "failed", attempt_id="old")
    assert "chat_memory_001" in run_store.active_runs()
    run_store.finish("chat_memory_001", "done", attempt_id="new")


def test_multiple_delivery_request_receipts_remain_idempotent(db):
    state = partial_state(phase="deliver")
    original, _ = run_lifecycle.admit_attempt(db, "chat_delivery_multi", "c_attempts", state.question,
        resume=True, checkpoint=state.snapshot(), request_id="delivery_one")
    db.commit()
    resumed, created = run_lifecycle.admit_attempt(db, "chat_delivery_multi", "c_attempts", state.question,
        resume=True, checkpoint=state.snapshot(), request_id="delivery_two")
    db.commit()
    assert created and resumed.id != original.id
    for request in ("delivery_one", "delivery_two"):
        again, created = run_lifecycle.admit_attempt(db, "chat_delivery_multi", "c_attempts", state.question,
            resume=True, checkpoint=state.snapshot(), request_id=request, expected_revision=999)
        assert not created and again.id == (original.id if request == "delivery_one" else resumed.id)
    assert db.query(RunAttempt).count() == 3


@pytest.mark.parametrize("prior_status", ["failed", "interrupted"])
def test_concurrent_delivery_resume_admission_has_one_new_attempt_and_preserves_source(db, prior_status):
    from app.db import SessionLocal
    run_id = "chat_concurrent_delivery"
    state = partial_state(phase="deliver")
    original, _ = run_lifecycle.admit_attempt(db, run_id, state.conversation_id, state.question)
    snapshot = run_lifecycle.bind_checkpoint(state.snapshot(), original)
    run_lifecycle.save_checkpoint(db, run_id, snapshot)
    run_lifecycle.finish_attempt(db, run_id, original.id, original.writer_token, prior_status)
    db.commit()
    frozen = {key: deepcopy(getattr(original, key)) for key in
              ("status", "result", "usage", "ended_at", "writer_token", "checkpoint")}
    barrier, replies, errors = threading.Barrier(2), [], []

    def admit():
        try:
            barrier.wait(timeout=3)
            with SessionLocal() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                attempt, created = run_lifecycle.admit_attempt(session, run_id, state.conversation_id, state.question,
                    checkpoint=snapshot, resume=True, request_id="same_delivery_request")
                session.commit()
                replies.append((attempt.id, created))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert len(replies) == 2 and len({reply[0] for reply in replies}) == 1
    assert sorted(reply[1] for reply in replies) == [False, True]
    db.refresh(original)
    assert {key: getattr(original, key) for key in frozen} == frozen
    assert db.query(RunAttempt).count() == 2


def test_plan_delivery_does_not_change_goal_outcome_or_unlock_next_turn(db):
    from app.goal_models import Goal
    from app.models import ForegroundRun
    from app.next_turn_dispatch import _complete_reason
    run_id = "chat_planned_outcome"
    state = partial_state(execution_mode="plan", result={"completed": False, "outcome": "planned"})
    attempt, _ = run_lifecycle.admit_attempt(db, run_id, state.conversation_id, state.question)
    snapshot = run_lifecycle.bind_checkpoint(state.snapshot(), attempt)
    db.add(ForegroundRun(id=run_id, conversation_id=state.conversation_id, question=state.question,
                          status="completed", execution_mode="plan", agent_checkpoint=snapshot))
    run_lifecycle.save_checkpoint(db, run_id, snapshot)
    db.commit()
    logical = db.get(RuntimeRun, run_id)
    assert logical.outcome == "open" and db.get(Goal, logical.goal_id).outcome == "open"
    assert attempt.result["outcome"] == "planned"
    assert run_lifecycle.public_run(db, run_id)["delivery_type"] == "planned"
    assert _complete_reason(db, run_id)
