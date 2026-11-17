"""追踪 API 端点集成测试（直接调用，离线）。"""
import pytest

from app.api.tracking import (
    FeedbackRequest,
    SubscriptionCreate,
    create_subscription,
    delete_subscription,
    give_feedback,
    list_subscriptions,
    refresh_subscription,
    toggle_subscription,
)
from app.models import Recommendation, Subscription


def test_create_and_list(db):
    out = create_subscription(
        SubscriptionCreate(topic="  retrieval agents  ", interval_hours=12), db
    )
    assert out["topic"] == "retrieval agents"  # 去除首尾空白
    assert out["active"] is True
    assert len(list_subscriptions(db)) == 1


def test_create_validation_gate(db):
    with pytest.raises(Exception) as empty:
        create_subscription(SubscriptionCreate(topic="   "), db)
    assert empty.value.status_code == 400
    with pytest.raises(Exception) as bad_interval:
        create_subscription(SubscriptionCreate(topic="x", interval_hours=999), db)
    assert bad_interval.value.status_code == 400


def test_toggle_and_delete(db):
    sub = create_subscription(SubscriptionCreate(topic="topic"), db)
    toggled = toggle_subscription(sub["id"], db)
    assert toggled["active"] is False
    delete = delete_subscription(sub["id"], db)
    assert delete["deleted"] == sub["id"]
    assert list_subscriptions(db) == []


def test_missing_subscription_404(db):
    with pytest.raises(Exception) as exc:
        refresh_subscription("nope", db)
    assert exc.value.status_code == 404


def test_feedback_changes_display_score(db):
    sub = create_subscription(SubscriptionCreate(topic="topic"), db)
    db.add(Recommendation(
        subscription_id=sub["id"], title="Paper", score=0.5, feedback=None
    ))
    db.commit()
    rec_id = db.query(Recommendation).first().id

    up = give_feedback(rec_id, FeedbackRequest(feedback="up"), db)
    assert up["feedback"] == "up"
    assert up["display_score"] == 0.65

    with pytest.raises(Exception) as invalid:
        give_feedback(rec_id, FeedbackRequest(feedback="maybe"), db)
    assert invalid.value.status_code == 400
