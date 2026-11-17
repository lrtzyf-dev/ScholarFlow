"""Transaction-friendly attempt admission and fencing; never commits for callers."""
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import func, select, update

from .runtime_models import RuntimeRun, RunAttempt, RunAdmissionRequest
from .timeutil import utcnow

TERMINAL = frozenset(("ended", "failed", "stopped", "interrupted"))


class RunConflict(ValueError):
    pass


class StaleAttempt(RunConflict):
    pass


def result_outcome(result):
    """Canonical execution outcome; a planned delivery leaves work open."""
    if not isinstance(result, dict):
        return "open"
    if result.get("outcome") == "planned":
        return "open"
    if result.get("completed") is False:
        return "blocked" if result.get("outcome") == "blocked" else "partial"
    return "completed" if result.get("completed") is True else "open"


def current_attempt(db, run_id):
    row = db.get(RuntimeRun, run_id)
    return db.get(RunAttempt, row.current_attempt_id) if row and row.current_attempt_id else None


def admitted_request(db, run_id, request_id):
    if not request_id:
        return None
    receipt = db.get(RunAdmissionRequest, (run_id, request_id))
    if receipt is not None:
        return db.get(RunAttempt, receipt.attempt_id)
    return db.scalar(select(RunAttempt).where(
        RunAttempt.run_id == run_id, RunAttempt.request_id == request_id))


def _record_admission(db, run_id, request_id, attempt):
    if request_id:
        db.add(RunAdmissionRequest(run_id=run_id, request_id=request_id, attempt_id=attempt.id))


def validate_binding(db, binding):
    """Resume with the bound model only while its configuration remains usable."""
    from .model_profiles import LEGACY_PROFILE_ID, get_profile, resolve_model_config
    profile = (binding or {}).get("profile_id")
    if profile and profile != LEGACY_PROFILE_ID:
        get_profile(db, profile, usable=True)
    resolve_model_config(binding, db)


def assert_conversation_idle(db, conversation_id, *, exclude_run_id=None, trusted_parent=None):
    """One writer per conversation, checked inside the admission transaction.

    Foreground rows reserve the slot before a worker starts. A dispatcher may
    start its own reservation, but another window cannot slip into that gap.
    Paused and delivered partial results are not execution ownership.
    """
    if not conversation_id:
        return
    excluded = {exclude_run_id} if exclude_run_id else set()
    if trusted_parent is not None:
        from .research_children import assert_parent
        parent, _ = assert_parent(db, trusted_parent)
        if parent.conversation_id != conversation_id:
            raise RunConflict("父执行与研究任务会话不匹配")
        excluded.add(parent.id)
    from . import run_store, task_runner
    from .models import ForegroundRun, TaskRecord
    if any(run not in excluded for run in run_store.active_for_conversation(conversation_id)):
        raise RunConflict("此会话已有任务正在执行，请补充当前任务或等待完成")
    foreground = db.scalar(select(ForegroundRun.id).where(
        ForegroundRun.conversation_id == conversation_id, ForegroundRun.status == "running",
        ForegroundRun.id.not_in(excluded)))
    tasks = db.scalar(select(TaskRecord.id).where(
        TaskRecord.conversation_id == conversation_id, TaskRecord.type == "agent",
        TaskRecord.id.not_in(excluded), TaskRecord.status.in_(("pending", "running"))))
    logical_rows = db.scalars(select(RuntimeRun).where(
        RuntimeRun.conversation_id == conversation_id, RuntimeRun.id.not_in(excluded),
        RuntimeRun.status.in_(("queued", "running")))).all()
    background_ids = set(task_runner.running_task_ids()) - excluded
    executing = db.scalar(select(TaskRecord.id).where(TaskRecord.conversation_id == conversation_id,
        TaskRecord.id.in_(background_ids))) if background_ids else None
    if foreground or tasks or executing:
        raise RunConflict("此会话已有排队或运行中的任务，请补充当前任务或等待完成")
    for logical in logical_rows:
        # All launch paths publish reservations and registry ownership while
        # holding the admission lock. Here neither a reservation nor a worker
        # exists, so this is an orphan left by an older process/recovery path.
        attempt = current_attempt(db, logical.id)
        if attempt is not None:
            finish_attempt(db, logical.id, attempt.id, attempt.writer_token, "interrupted",
                           "旧执行器已停止，保留检查点等待明确恢复")
        if logical.status in ("queued", "running"):
            logical.status = attempt.status if attempt and attempt.status in TERMINAL else "interrupted"
            logical.revision += 1
            logical.updated_at = utcnow()


def public_run(db, run_id):
    run = db.get(RuntimeRun, run_id)
    if run is None:
        return None
    attempt = current_attempt(db, run_id)
    return {"run_id": run.id, "goal_id": run.goal_id, "revision": run.revision,
            "status": run.status, "outcome": run.outcome,
            "delivery_type": "planned" if attempt and (attempt.result or {}).get("outcome") == "planned" else "answer",
            "attempt_id": attempt.id if attempt else None,
            "attempt_number": attempt.number if attempt else 0}


def admit_attempt(db, run_id, conversation_id, question, *, kind="foreground", checkpoint=None,
                  resume=False, request_id=None, expected_revision=None):
    """Call under the admission lock and SQLite write transaction.

    A completed HTTP retry with the same request ID returns its original attempt,
    never schedules work again. Legacy checkpoints become an archived attempt.
    """
    run = db.get(RuntimeRun, run_id)
    if run is not None and run.conversation_id != conversation_id:
        raise RunConflict("执行记录与会话不匹配")
    if request_id:
        previous = admitted_request(db, run_id, request_id)
        if previous is not None:
            return previous, False
    if expected_revision is not None and (run is None or run.revision != expected_revision):
        raise RunConflict("运行已更新，请刷新后再继续")
    if resume:
        from .agent.checkpoint import recovery_info
        info = recovery_info(checkpoint)
        if not info.get("available"):
            raise RunConflict(info.get("reason") or "此轮目标已完成或没有有效的继续检查点")
    if run is None:
        from .goal_ledger import ensure_run_goal
        goal = ensure_run_goal(db, run_id, conversation_id, question)
        run = RuntimeRun(id=run_id, kind=kind, conversation_id=conversation_id,
                         goal_id=goal["goal_id"], status="queued", outcome="open", revision=1)
        db.add(run)
        db.flush()
        if resume and checkpoint:
            old_result = deepcopy(checkpoint.get("result"))
            old = RunAttempt(id=uuid4().hex, run_id=run_id, number=1, writer_token=uuid4().hex,
                             status="ended" if checkpoint.get("phase") == "completed" else "interrupted",
                             checkpoint=deepcopy(checkpoint), result=old_result,
                             usage=deepcopy(checkpoint.get("usage")), ended_at=utcnow())
            db.add(old)
            db.flush()
            run.current_attempt_id = old.id
            run.outcome = result_outcome(old_result)
    old = current_attempt(db, run_id)
    if old is not None and not resume:
        raise RunConflict("执行 ID 已使用，请明确继续原任务或新建请求")
    if old is not None and old.status not in TERMINAL:
        # Caller has proved no live worker owns the row before invoking resume.
        old.status, old.ended_at = "interrupted", utcnow()
    number = (db.scalar(select(func.max(RunAttempt.number)).where(RunAttempt.run_id == run_id)) or 0) + 1
    attempt_checkpoint = deepcopy(checkpoint)
    if resume and attempt_checkpoint is not None:
        attempt_checkpoint["source_attempt_id"] = old.id if old else None
        attempt_checkpoint["delivery_only"] = attempt_checkpoint.get("phase") == "deliver"
    attempt = RunAttempt(id=uuid4().hex, run_id=run_id, number=number,
                         request_id=request_id, writer_token=uuid4().hex,
                         checkpoint=attempt_checkpoint, status="queued")
    db.add(attempt)
    db.flush()
    _record_admission(db, run_id, request_id, attempt)
    if attempt_checkpoint is not None:
        attempt.checkpoint = bind_checkpoint(attempt_checkpoint, attempt)
    run.current_attempt_id, run.status = attempt.id, "queued"
    run.revision += 1
    run.updated_at = utcnow()
    db.flush()
    from .run_timing import phase
    phase(db, run_id, attempt.id, attempt.writer_token, "run_queue")
    return attempt, True


def bind_checkpoint(snapshot, attempt):
    snapshot = deepcopy(snapshot)
    snapshot["attempt_id"], snapshot["writer_token"] = attempt.id, attempt.writer_token
    return snapshot


def assert_writer(db, run_id, attempt_id, writer_token, *, allow_terminal=False, lock=False):
    if not attempt_id:
        if db.get(RuntimeRun, run_id) is not None:
            raise StaleAttempt("持久运行缺少执行身份，拒绝提交未绑定的旧结果")
        return None
    if lock:
        # Acquire SQLite's write reservation before checking the attempt. The
        # following checkpoint/QA/UI projection then shares that transaction,
        # so a newer admission cannot race between validation and commit.
        acquired = db.execute(update(RuntimeRun).where(
            RuntimeRun.id == run_id, RuntimeRun.current_attempt_id == attempt_id,
        ).values(revision=RuntimeRun.revision).execution_options(synchronize_session=False))
        if acquired.rowcount != 1:
            raise StaleAttempt("该执行尝试已被替代，停止提交旧结果")
    # SELECT, not identity-map reads: another session may have superseded us.
    row = db.execute(select(RuntimeRun.current_attempt_id, RunAttempt.writer_token, RunAttempt.status)
        .join(RunAttempt, RunAttempt.id == RuntimeRun.current_attempt_id)
        .where(RuntimeRun.id == run_id)).one_or_none()
    if row is None or row[0] != attempt_id or row[1] != writer_token or (row[2] in TERMINAL and not allow_terminal):
        raise StaleAttempt("该执行尝试已被替代或结束，停止提交旧结果")
    return db.get(RunAttempt, attempt_id)


def save_checkpoint(db, run_id, snapshot):
    attempt_id, token = snapshot.get("attempt_id"), snapshot.get("writer_token")
    if not attempt_id:
        assert_writer(db, run_id, None, None)
        return  # Unmanaged legacy/test callers preserve their prior contract.
    attempt = assert_writer(db, run_id, attempt_id, token, lock=True)
    run = db.get(RuntimeRun, run_id)
    attempt.checkpoint = deepcopy(snapshot)
    if isinstance(snapshot.get("result"), dict):
        attempt.result = deepcopy(snapshot["result"])
        run.outcome = result_outcome(attempt.result)
    attempt.usage = deepcopy(snapshot.get("usage"))
    phase = snapshot.get("phase")
    status = "ended" if phase == "completed" else "paused" if phase == "paused" else "running"
    attempt.status, run.status = status, status
    if status == "ended":
        attempt.ended_at = utcnow()
    run.revision += 1
    run.updated_at = utcnow()
    from .run_timing import phase
    phase(db, run_id, attempt_id, token,
          None if status == "ended" else "user_wait" if status == "paused" else "run_active")
    db.flush()


def finish_attempt(db, run_id, attempt_id, writer_token, status, error=""):
    if not attempt_id:
        return
    attempt = assert_writer(db, run_id, attempt_id, writer_token, allow_terminal=True, lock=True)
    if attempt.status in TERMINAL:
        return  # Idempotent cleanup; never reopen or rewrite its result.
    run = db.get(RuntimeRun, run_id)
    mapped = {"done": "ended", "completed": "ended", "cancelled": "stopped"}.get(status, status)
    attempt.status, run.status = mapped, mapped
    attempt.error = str(error)[:2000]
    if mapped in TERMINAL:
        attempt.ended_at = utcnow()
    run.revision += 1
    run.updated_at = utcnow()
    from .run_timing import phase
    phase(db, run_id, attempt_id, writer_token, "user_wait" if mapped == "paused" else None)
    db.flush()
