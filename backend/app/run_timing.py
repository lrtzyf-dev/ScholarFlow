"""Durable monotonic timing with attempt/process fencing and conservative recovery.

The request window starts when a quota slot is acquired. It includes request
preparation and teardown, and is deliberately not called HTTP/provider compute.
No function here changes existing budget accounting.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import timedelta
import threading
import time
from uuid import uuid4
from sqlalchemy import select
from .timeutil import utcnow
from .timing_models import TimingSpan

PROCESS_ID = uuid4().hex
PHASES = ("run_queue", "run_active", "approval_wait", "user_wait")
_stop = threading.Event()
_thread = None


def _identity(db, run_id, attempt_id=None, writer_token=None, *, terminal=False):
    from .run_lifecycle import assert_writer
    from .runtime_models import RuntimeRun, RunAttempt
    run = db.get(RuntimeRun, run_id) if run_id else None
    if run is None:
        return None
    if attempt_id is None:
        # Only lifecycle callers may resolve a current identity. Request callers
        # require their frozen attempt ID and must never borrow a new writer.
        attempt_id = run.current_attempt_id
    attempt = db.get(RunAttempt, attempt_id) if attempt_id else None
    if attempt is None:
        from .run_lifecycle import StaleAttempt
        raise StaleAttempt("持久运行缺少有效的耗时执行身份")
    return assert_writer(db, run_id, attempt_id, attempt.writer_token if writer_token is None else writer_token,
                         allow_terminal=terminal, lock=True)


def _confirm(row, *, close=None, now=None, mono=None):
    if row.state != "open":
        return
    if row.process_id != PROCESS_ID:
        # A different process cannot use this monotonic anchor. Never extend a
        # prior process's span to startup time.
        row.state = "interrupted"
        return
    now, mono = now or utcnow(), time.monotonic() if mono is None else mono
    elapsed = max(row.elapsed_ms, int(max(0, mono - row.started_monotonic) * 1000))
    if abs((now - row.started_at).total_seconds() - elapsed / 1000) > 2:
        row.clock_adjusted = 1
    row.elapsed_ms, row.confirmed_at = elapsed, now
    if close:
        row.state = close


def _open(db, run_id, attempt, kind, **values):
    now = utcnow()
    row = TimingSpan(id=uuid4().hex, run_id=run_id, attempt_id=attempt.id,
        writer_token=attempt.writer_token, process_id=PROCESS_ID, kind=kind,
        started_at=now, confirmed_at=now, started_monotonic=time.monotonic(),
        elapsed_ms=0, state="open", clock_adjusted=0, **values)
    db.add(row)
    return row


def phase(db, run_id, attempt_id, writer_token, kind=None):
    """Participates in caller's fenced transaction; never commits it."""
    if kind is not None and kind not in PHASES:
        raise ValueError("Unknown timing phase")
    attempt = _identity(db, run_id, attempt_id, writer_token, terminal=True)
    if attempt is None:
        return
    for abandoned in db.scalars(select(TimingSpan).where(TimingSpan.run_id == run_id,
            TimingSpan.attempt_id != attempt_id, TimingSpan.state == "open")):
        abandoned.state = "interrupted"
    rows = list(db.scalars(select(TimingSpan).where(TimingSpan.run_id == run_id,
        TimingSpan.kind.in_(PHASES), TimingSpan.state == "open")))
    same = None
    for row in rows:
        if (row.attempt_id == attempt_id and row.writer_token == writer_token
                and row.process_id == PROCESS_ID and row.kind == kind):
            _confirm(row)
            same = row
        else:
            if row.attempt_id != attempt_id:
                row.state = "interrupted"
            else:
                _confirm(row, close="closed")
    if kind is not None and same is None:
        _open(db, run_id, attempt, kind)
    db.flush()


def confirm_attempt(db, run_id, attempt_id, writer_token):
    attempt = _identity(db, run_id, attempt_id, writer_token, terminal=True)
    if attempt is None:
        return
    for row in db.scalars(select(TimingSpan).where(TimingSpan.run_id == run_id,
            TimingSpan.attempt_id == attempt_id, TimingSpan.writer_token == writer_token,
            TimingSpan.process_id == PROCESS_ID, TimingSpan.state == "open")):
        _confirm(row)


def begin_request(binding, run_id, service):
    """Returns no record for unmanaged probes; never guesses their attempt."""
    if not run_id or not binding.get("attempt_id"):
        return None
    from .db import SessionLocal
    with SessionLocal() as db:
        attempt = _identity(db, run_id, binding["attempt_id"])
        if attempt is None:
            return None
        dispatch_id = uuid4().hex
        _open(db, run_id, attempt, "request_queue", dispatch_id=dispatch_id, service=service[:60])
        db.commit()
        return {"run_id": run_id, "attempt_id": attempt.id, "writer_token": attempt.writer_token,
                "dispatch_id": dispatch_id, "process_id": PROCESS_ID, "service": service[:60]}


def request_boundary(identity, boundary, call_id=None):
    if not identity:
        return
    if identity["process_id"] != PROCESS_ID:
        from .run_lifecycle import StaleAttempt
        raise StaleAttempt("旧进程不能提交新请求的耗时")
    from .db import SessionLocal
    with SessionLocal() as db:
        attempt = _identity(db, identity["run_id"], identity["attempt_id"], identity["writer_token"],
                            terminal=boundary not in ("acquired", "bind"))
        if attempt is None:
            return
        rows = list(db.scalars(select(TimingSpan).where(
            TimingSpan.dispatch_id == identity["dispatch_id"])))
        if boundary == "acquired":
            queue = next((r for r in rows if r.kind == "request_queue"), None)
            execution = next((r for r in rows if r.kind == "request_execution_window"), None)
            if execution is None and (queue is None or queue.state != "open"):
                raise ValueError("已取消的排队不能重新建立请求窗口")
            if queue and queue.state == "open":
                _confirm(queue, close="closed")
            if execution is None:
                _open(db, identity["run_id"], attempt, "request_execution_window",
                      dispatch_id=identity["dispatch_id"], service=identity["service"])
        elif boundary == "bind":
            for row in rows:
                if row.kind == "request_execution_window":
                    if row.call_id not in (None, call_id):
                        raise ValueError("耗时记录已绑定另一调用")
                    row.call_id = call_id
        else:
            for row in rows:
                _confirm(row, close=boundary)
        db.commit()


@contextmanager
def approval_wait(run_id):
    from .runtime_context import current_runtime_config
    from .db import SessionLocal
    binding = current_runtime_config() or {}
    aid = binding.get("attempt_id")
    token = None
    if run_id and aid:
        with SessionLocal() as db:
            attempt = _identity(db, run_id, aid)
            if attempt:
                token = attempt.writer_token
                phase(db, run_id, aid, token, "approval_wait")
                db.commit()
    try:
        yield
    finally:
        if token:
            from .run_lifecycle import StaleAttempt, TERMINAL
            try:
                with SessionLocal() as db:
                    attempt = _identity(db, run_id, aid, token, terminal=True)
                    phase(db, run_id, aid, token,
                          None if attempt.status in TERMINAL else "user_wait" if attempt.status == "paused" else "run_active")
                    db.commit()
            except StaleAttempt:
                pass  # Never close/reopen the replacement attempt's phase.


def tick(stop_event=None):
    """One shared heartbeat for all foreground/background/request/wait spans."""
    from .db import SessionLocal
    from .run_lifecycle import StaleAttempt
    with SessionLocal() as db:
        identities = list(db.execute(select(TimingSpan.run_id, TimingSpan.attempt_id,
            TimingSpan.writer_token).where(TimingSpan.process_id == PROCESS_ID,
                TimingSpan.state == "open").distinct()))
    for rid, aid, token in identities:
        if stop_event is not None and stop_event.is_set():
            break
        with SessionLocal() as db:
            try:
                confirm_attempt(db, rid, aid, token)
                db.commit()
            except StaleAttempt:
                db.rollback()
                # Replacement/recovery owns the old close, not the stale timer.


def recover_startup():
    from .runtime_owner import owner
    from .db import SessionLocal
    with owner.startup_recovery_guard():
        with SessionLocal() as db:
            for row in db.scalars(select(TimingSpan).where(TimingSpan.state == "open")):
                # No duration increment: only confirmed pre-crash time survives.
                row.state = "interrupted"
            db.commit()


def start():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    def heartbeat():
        import logging
        while not _stop.wait(5):
            try:
                tick(_stop)
            except Exception:
                # Do not invent intervals if persistence is unavailable.
                logging.getLogger(__name__).warning("任务耗时确认暂未保存", exc_info=True)
    _thread = threading.Thread(target=heartbeat, name="run-timing-heartbeat", daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=6)
    from .db import SessionLocal
    from .run_lifecycle import StaleAttempt
    with SessionLocal() as db:
        identities = list(db.execute(select(TimingSpan.run_id, TimingSpan.attempt_id,
            TimingSpan.writer_token).where(TimingSpan.process_id == PROCESS_ID,
                TimingSpan.state == "open").distinct()))
    for rid, aid, token in identities:
        with SessionLocal() as db:
            try:
                _identity(db, rid, aid, token, terminal=True)
                for row in db.scalars(select(TimingSpan).where(TimingSpan.run_id == rid,
                    TimingSpan.attempt_id == aid, TimingSpan.process_id == PROCESS_ID,
                    TimingSpan.state == "open")):
                    _confirm(row, close="interrupted")
                db.commit()
            except StaleAttempt:
                db.rollback()


def active():
    return _thread is not None and _thread.is_alive()


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum((b - a).total_seconds() for a, b in merged)


def snapshot(db, run):
    """Read-only; open spans show only their latest durable confirmation."""
    from .runtime_models import RunAttempt
    rows = list(db.scalars(select(TimingSpan).where(TimingSpan.run_id == run.id)))
    if not rows:
        return {"run_id": run.id, "available": False, "reason": "此历史任务尚无分段耗时记录"}
    phases = [r for r in rows if r.kind in PHASES]
    intervals = lambda items: [(r.started_at, r.started_at + timedelta(milliseconds=r.elapsed_ms)) for r in items]
    terminal_at = db.scalar(select(RunAttempt.ended_at).where(RunAttempt.id == run.current_attempt_id))
    # Running/paused spans retain a visible, explicitly unknown unconfirmed tail.
    end = terminal_at if terminal_at and run.status not in ("running", "queued", "paused") else utcnow()
    start = min(r.started_at for r in rows)
    elapsed = max(0, (end - start).total_seconds())
    confirmed = _union(intervals(phases))
    clock_uncertain = any(r.clock_adjusted or (r.state == "open" and r.process_id == PROCESS_ID
        and abs((utcnow() - r.started_at).total_seconds()
                - max(0, time.monotonic() - r.started_monotonic)) > 2) for r in rows)
    requests = [r for r in rows if r.kind == "request_execution_window"]
    unique = {}
    for row in requests:
        key = ("call", row.call_id) if row.call_id else ("dispatch", row.dispatch_id or row.id)
        unique[key] = max(unique.get(key, 0), row.elapsed_ms / 1000)
    result = {"run_id": run.id, "attempt_id": run.current_attempt_id, "available": True,
        "as_of": utcnow().isoformat(), "elapsed_wall_seconds": round(elapsed, 3),
        "confirmed_task_wall_seconds": round(confirmed, 3),
        "active_wall_seconds": round(max(0,
            _union(intervals(r for r in rows if r.kind in ("run_active", "request_queue")))
            - _union(intervals(r for r in rows if r.kind == "request_queue"))), 3),
        "queue_wall_seconds": round(_union(intervals(r for r in rows if r.kind in ("run_queue", "request_queue"))), 3),
        "approval_wait_seconds": round(sum(r.elapsed_ms for r in phases if r.kind == "approval_wait") / 1000, 3),
        "user_wait_seconds": round(sum(r.elapsed_ms for r in phases if r.kind == "user_wait") / 1000, 3),
        "offline_or_unknown_seconds": round(max(0, elapsed - confirmed), 3),
        "request_execution_sum_seconds": round(sum(unique.values()), 3),
        "request_execution_wall_seconds": round(_union(intervals(requests)), 3),
        "request_windows": len(requests), "deduplicated_request_windows": len(unique),
        "duplicate_call_windows": len(requests) - len(unique),
        "queue_only_requests": len({r.dispatch_id for r in rows if r.kind == "request_queue"} - {r.dispatch_id for r in requests}),
        "open_segments": sum(r.state == "open" for r in rows),
        "last_confirmed_at": max(r.confirmed_at for r in rows).isoformat(),
        "clock_adjustment_detected": clock_uncertain, "wall_time_basis_reliable": not clock_uncertain,
        "request_time_scope": "quota_slot_acquired_through_request_teardown",
        "duration_scope": "persisted_confirmation_only", "request_sum_additive_to_wall": False}
    if clock_uncertain:
        # A UTC adjustment cannot be called offline time. Monotonic request and
        # serial wait durations remain known, but cross-span wall unions do not.
        for key in ("elapsed_wall_seconds", "confirmed_task_wall_seconds", "active_wall_seconds",
                    "queue_wall_seconds", "offline_or_unknown_seconds", "request_execution_wall_seconds"):
            result[key] = None
    return result
