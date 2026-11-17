"""轻量后台调度器：定时检查到期订阅，worker slot 限制并发，含崩溃恢复。

单用户桌面端采用串行刷新（并发上限 1），避免并发请求触发 arXiv 限流。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from ..db import SessionLocal
from ..models import Subscription
from .service import run_subscription
from ..timeutil import utcnow

CHECK_INTERVAL_SECONDS = 60.0


def is_due(last_run_at: datetime | None, interval_hours: int, now: datetime) -> bool:
    """纯函数：判断订阅是否到期。"""
    if last_run_at is None:
        return True
    return now >= last_run_at + timedelta(hours=interval_hours)


class TrackingScheduler:
    def __init__(self, runner=run_subscription) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._slot_lock = threading.Lock()  # worker slot，保证并发上限
        self._runner = runner

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self.recover_interrupted()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="tracking-scheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(CHECK_INTERVAL_SECONDS)

    # ---- 单次检查（可独立测试）----
    def tick(self) -> int:
        from ..maintenance import activity, MaintenanceBusy
        try:
            with activity():
                return self._tick_admitted()
        except MaintenanceBusy:
            return 0

    def _tick_admitted(self) -> int:
        # 非阻塞获取 worker slot；已有刷新在跑则跳过本轮
        if not self._slot_lock.acquire(blocking=False):
            return 0
        try:
            db = SessionLocal()
            processed = 0
            try:
                now = utcnow()
                subs = db.query(Subscription).filter(Subscription.active == 1).all()
                for sub in subs:
                    if is_due(sub.last_run_at, sub.interval_hours, now):
                        self._runner(db, sub)
                        processed += 1
            finally:
                db.close()
            return processed
        finally:
            self._slot_lock.release()

    def recover_interrupted(self) -> None:
        """启动时把上次崩溃残留的 running 标记为 interrupted，不自动重跑。"""
        db = SessionLocal()
        try:
            stale = db.query(Subscription).filter(
                Subscription.last_status == "running"
            ).all()
            for sub in stale:
                sub.last_status = "interrupted"
            if stale:
                db.commit()
        finally:
            db.close()
