"""Short application admission barrier for consistent data maintenance.

Existing model/terminal work is never cancelled to obtain a backup. Callers
first reserve maintenance, drain admitted requests, and then require workers
to be idle. Scheduler passes participate in the same admission counter.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from starlette.responses import JSONResponse


class MaintenanceBusy(RuntimeError):
    pass


_condition = threading.Condition(threading.RLock())
_active = 0
_reason = ""
_pending_restore = False


def enter():
    global _active
    with _condition:
        if _reason or _pending_restore:
            raise MaintenanceBusy(_reason or "恢复已确认，请完全退出并重新打开研序，或在数据设置中取消恢复")
        _active += 1


def leave():
    global _active
    with _condition:
        _active -= 1
        _condition.notify_all()


@contextmanager
def activity():
    enter()
    try:
        yield
    finally:
        leave()


@contextmanager
def exclusive(reason="正在准备数据备份，请稍后再试", timeout=10):
    global _reason
    with _condition:
        if _reason:
            raise MaintenanceBusy(_reason)
        _reason = reason
        deadline = time.monotonic() + timeout
        try:
            while _active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MaintenanceBusy("仍有资料读写请求，原数据未改动；请稍后重试")
                _condition.wait(remaining)
        except BaseException:
            _reason = ""
            _condition.notify_all()
            raise
    try:
        yield
    finally:
        with _condition:
            _reason = ""
            _condition.notify_all()


def current_reason():
    with _condition:
        return _reason or ("恢复等待重启" if _pending_restore else "")


def set_restore_pending(value):
    global _pending_restore
    with _condition:
        _pending_restore = bool(value)
        _condition.notify_all()


class MaintenanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] not in ("http", "websocket") or not path.startswith("/api/") or path.startswith("/api/backups"):
            return await self.app(scope, receive, send)
        try:
            enter()
        except MaintenanceBusy as exc:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013, "reason": str(exc)})
            else:
                await JSONResponse({"detail": str(exc)}, status_code=423,
                                   headers={"Retry-After": "2"})(scope, receive, send)
            return
        held = True

        async def observed_send(message):
            nonlocal held
            # Observers do not own a worker. Retaining a counter for an SSE/WS
            # lifetime would make backups impossible while the app is open.
            streaming = message["type"] == "http.response.start" and any(
                k.lower() == b"content-type" and b"text/event-stream" in v for k, v in message.get("headers", []))
            if held and (streaming or message["type"] == "websocket.accept"):
                held = False
                leave()
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            if held:
                leave()
