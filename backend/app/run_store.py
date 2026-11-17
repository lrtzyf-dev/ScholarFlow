"""Coordinate foreground execution, cancellation and conversation mutations."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from . import approval_store, cancel_store
from .task_events import notify_tasks_changed

_lock = threading.RLock()
_active: dict[str, str | None] = {}
_finished: dict[str, tuple[float, str]] = {}
_early_cancels: dict[str, float] = {}
_active_attempts: dict[str, str | None] = {}
_finished_attempts: dict[str, str | None] = {}


def _prune() -> None:
    now = time.monotonic()
    for records, ttl in ((_finished, 300), (_early_cancels, 60)):
        for key, value in tuple(records.items()):
            timestamp = value[0] if isinstance(value, tuple) else value
            if now - timestamp > ttl:
                records.pop(key, None)
        while len(records) > 1024:
            records.pop(next(iter(records)))
    for run_id in tuple(_finished_attempts):
        if run_id not in _finished:
            _finished_attempts.pop(run_id, None)


@contextmanager
def coordinated():
    """Serialize run admission with short conversation/task DB mutations."""
    with _lock:
        yield


def begin(run_id: str, conversation_id: str | None, *, attempt_id: str | None = None) -> bool:
    with _lock:
        _prune()
        if run_id in _active:
            return False
        previous = _finished.get(run_id)
        if previous:
            if previous[1] != "paused" and (not attempt_id or attempt_id == _finished_attempts.get(run_id)):
                return False
            _finished.pop(run_id, None)
        _active[run_id] = conversation_id
        _active_attempts[run_id] = attempt_id
        cancel_store.clear(run_id)
        if _early_cancels.pop(run_id, None) is not None:
            cancel_store.request_cancel(run_id)
        notify_tasks_changed()
        return True


def cancel(run_id: str) -> str:
    with _lock:
        _prune()
        if run_id in _finished:
            return _finished[run_id][1]
        if run_id not in _active:
            # The cancel HTTP request may arrive before its stream POST.
            _early_cancels[run_id] = time.monotonic()
            _prune()
        else:
            cancel_store.request_cancel(run_id)
            approval_store.resolve_for_run(run_id)
        notify_tasks_changed()
        return "cancelling"


def finish(run_id: str, status: str, *, attempt_id: str | None = None) -> None:
    with _lock:
        if attempt_id is not None and _active_attempts.get(run_id) != attempt_id:
            return
        approval_store.discard_for_run(run_id)
        _active.pop(run_id, None)
        _finished_attempts[run_id] = _active_attempts.pop(run_id, attempt_id)
        _finished[run_id] = (time.monotonic(), status)
        cancel_store.clear(run_id)
        _prune()
    notify_tasks_changed()


def active_runs() -> dict[str, str | None]:
    with _lock:
        return dict(_active)


def active_for_conversation(conversation_id: str) -> list[str]:
    with _lock:
        return [run_id for run_id, cid in _active.items() if cid == conversation_id]


def cancel_all() -> None:
    with _lock:
        for run_id in tuple(_active):
            cancel(run_id)
