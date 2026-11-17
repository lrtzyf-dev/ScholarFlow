"""后台调度器测试：到期判断、tick 处理与崩溃恢复。"""
from datetime import datetime, timedelta

from app.models import Subscription
from app.tracking.scheduler import TrackingScheduler, is_due
from app.timeutil import utcnow


def test_is_due_logic():
    now = datetime(2026, 11, 6, 12, 0, 0)
    assert is_due(None, 24, now) is True
    recent = now - timedelta(hours=1)
    assert is_due(recent, 24, now) is False
    old = now - timedelta(hours=25)
    assert is_due(old, 24, now) is True


def test_tick_runs_due_subscriptions(db):
    due = Subscription(topic="due topic", active=1)
    paused = Subscription(topic="paused topic", active=0)
    not_due = Subscription(
        topic="not due", active=1, last_run_at=utcnow()
    )
    db.add_all([due, paused, not_due])
    db.commit()

    ran = []

    def fake_runner(session, sub):
        ran.append(sub.topic)
        sub.last_status = "ok"

    processed = TrackingScheduler(runner=fake_runner).tick()
    assert processed == 1
    assert ran == ["due topic"]


def test_recover_interrupted(db):
    db.add_all([
        Subscription(topic="stale-1", last_status="running"),
        Subscription(topic="stale-2", last_status="running"),
        Subscription(topic="fine", last_status="ok"),
    ])
    db.commit()

    scheduler = TrackingScheduler()
    scheduler.recover_interrupted()
    db.expire_all()

    statuses = {s.topic: s.last_status for s in db.query(Subscription).all()}
    assert statuses["stale-1"] == "interrupted"
    assert statuses["stale-2"] == "interrupted"
    assert statuses["fine"] == "ok"
