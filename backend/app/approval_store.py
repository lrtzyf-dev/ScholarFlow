"""人工审批（Human-in-the-loop）：高风险工具执行前暂停，等待用户决定。

同步 SSE 生成器在独立线程中运行（chat_stream 为同步 def），因此这里用
threading.Event 让生成器阻塞等待，不影响事件循环。
"""
from __future__ import annotations

import threading
import time
import uuid

from .task_events import notify_tasks_changed


class PendingApproval:
    def __init__(self, tool_name: str, arguments: dict, risk: str,
                 reason: str = "", run_id: str | None = None) -> None:
        self.id = f"apv_{uuid.uuid4().hex[:10]}"
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk = risk
        self.reason = reason
        self.run_id = run_id  # 前台执行或后台任务 id，用于取消时唤醒
        self.event = threading.Event()
        self.decision: str | None = None  # approve / deny
        self.scope: str = "once"  # once / session
        self.created = time.time()

    def to_request(self) -> dict:
        return {
            "approval_id": self.id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "risk": self.risk,
            "reason": self.reason,
        }


_pending: dict[str, PendingApproval] = {}
_lock = threading.Lock()

# 默认等待 5 分钟，超时按拒绝处理，避免生成器永久挂起
DEFAULT_TIMEOUT = 300.0


def create(tool_name: str, arguments: dict, risk: str, reason: str = "",
           run_id: str | None = None) -> PendingApproval:
    p = PendingApproval(tool_name, arguments, risk, reason, run_id)
    with _lock:
        _pending[p.id] = p
    if run_id:
        notify_tasks_changed()
    return p


def resolve_for_run(run_id: str, decision: str = "deny") -> int:
    """取消任务时，把该 run 下所有未决审批统一 resolve（默认拒绝），唤醒阻塞等待。"""
    resolved = 0
    with _lock:
        targets = [p for p in _pending.values()
                   if p.run_id == run_id and p.decision is None]
        for p in targets:
            p.decision = decision
            p.scope = "once"
            p.event.set()
            resolved += 1
    if resolved:
        notify_tasks_changed()
    return resolved


def get(approval_id: str) -> PendingApproval | None:
    return _pending.get(approval_id)


def resolve(approval_id: str, decision: str, scope: str = "once") -> bool:
    with _lock:
        p = _pending.get(approval_id)
        if p is None or p.decision is not None:
            return False
        p.decision = decision
        p.scope = scope
        p.event.set()
    if p.run_id:
        notify_tasks_changed()
    return True


def list_for_run(run_id: str) -> list[dict]:
    with _lock:
        return [p.to_request() for p in _pending.values()
                if p.run_id == run_id and p.decision is None]


def wait(approval_id: str, timeout: float = DEFAULT_TIMEOUT) -> PendingApproval:
    p = _pending.get(approval_id)
    if p is not None:
        p.event.wait(timeout)
    return p


def discard(approval_id: str) -> None:
    with _lock:
        pending = _pending.pop(approval_id, None)
    if pending and pending.run_id:
        notify_tasks_changed()


def discard_for_run(run_id: str) -> None:
    """Release waiters and remove records even if their generator closed early."""
    with _lock:
        targets = [p for p in _pending.values() if p.run_id == run_id]
        for pending in targets:
            if pending.decision is None:
                pending.decision = "deny"
            pending.event.set()
            _pending.pop(pending.id, None)
    if targets:
        notify_tasks_changed()
