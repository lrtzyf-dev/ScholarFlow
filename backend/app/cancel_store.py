"""协作式取消信号：前台执行和后台任务均可被用户请求中断。

- 取消是协作式的：run_agent 在每个安全检查点（每轮思考前、每个工具执行前、
  审批等待返回后及 LLM 流块之间）主动检查标志；不强行终止阻塞中的工具或线程，
  不会强行中断线程，避免写坏数据库或留下半完成状态；
- 用 threading.Event 实现；任务若正阻塞在人工审批上，由
  approval_store.resolve_for_run 唤醒，取消随即生效。
"""
from __future__ import annotations

import threading


_flags: dict[str, threading.Event] = {}
_lock = threading.Lock()


def request_cancel(run_id: str) -> None:
    """请求取消某个后台任务（幂等，可重复调用）。"""
    with _lock:
        ev = _flags.get(run_id)
        if ev is None:
            ev = threading.Event()
            _flags[run_id] = ev
        ev.set()


def is_cancelled(run_id: str | None) -> bool:
    """没有执行 id 的内部/旧调用不参与取消。"""
    if not run_id:
        return False
    with _lock:
        ev = _flags.get(run_id)
        return bool(ev and ev.is_set())


def clear(run_id: str) -> None:
    """任务启动/恢复时清除旧标志，避免上一轮的取消信号残留。"""
    with _lock:
        _flags.pop(run_id, None)
