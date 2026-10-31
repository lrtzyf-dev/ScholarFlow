"""Bridge task changes from worker threads to coalesced SSE subscriptions."""
from __future__ import annotations

import asyncio
import threading
from uuid import uuid4
from contextlib import asynccontextmanager

_lock = threading.Lock()
_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
_instance = uuid4().hex
_revision = 0


def conversation_epoch() -> str:
    with _lock:
        return f"{_instance}:{_revision}"


def _enqueue(queue: asyncio.Queue) -> None:
    if queue.empty():
        queue.put_nowait(None)


def notify_tasks_changed() -> None:
    global _revision
    with _lock:
        _revision += 1
        subscribers = tuple(_subscribers)
    for loop, queue in subscribers:
        try:
            loop.call_soon_threadsafe(_enqueue, queue)
        except RuntimeError:
            # A disconnected client's event loop may already be closed.
            pass


@asynccontextmanager
async def subscribe():
    queue = asyncio.Queue(maxsize=1)
    subscriber = (asyncio.get_running_loop(), queue)
    with _lock:
        _subscribers.add(subscriber)
    try:
        yield queue
    finally:
        with _lock:
            _subscribers.discard(subscriber)
