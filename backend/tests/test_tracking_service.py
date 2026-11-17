"""订阅刷新编排测试（离线，注入假 fetcher 与 LocalEmbedder）。"""
import pytest

from app.models import Paper, Recommendation, Subscription
from app.pipeline.embeddings import LocalEmbedder
from app.tracking.service import run_subscription


def _candidates():
    return [
        {
            "arxiv_id": "2401.00001", "title": "Deep Learning Retrieval",
            "authors": "Alice", "published_at": "2024-01-01",
            "abstract": "a deep learning retrieval method using neural networks",
            "url": "http://arxiv.org/abs/2401.00001", "github_url": None,
        },
        {
            "arxiv_id": "2401.00002", "title": "Open Source Study",
            "authors": "Bob", "published_at": "2024-02-01",
            "abstract": "reproducible research with released code toolkit",
            "url": "http://arxiv.org/abs/2401.00002",
            "github_url": "https://github.com/team/repo",
        },
    ]


def _fake_fetcher(topic, max_results):
    return _candidates()


def _failing_fetcher(topic, max_results):
    raise ConnectionError("network unavailable")


@pytest.fixture()
def subscription(db):
    sub = Subscription(topic="deep learning retrieval", interval_hours=24)
    db.add(sub)
    db.commit()
    return sub


def test_run_persists_recommendations(db, subscription):
    out = run_subscription(
        db, subscription, fetcher=_fake_fetcher, embedder=LocalEmbedder()
    )
    assert out["status"] == "ok"
    assert out["added"] == 2
    recs = db.query(Recommendation).filter(
        Recommendation.subscription_id == subscription.id
    ).all()
    assert len(recs) == 2
    for rec in recs:
        assert 0.0 <= rec.score <= 1.0
        assert rec.reasons  # 可解释理由非空
    # 开源候选记录了 GitHub 地址
    open_rec = next(r for r in recs if r.arxiv_id == "2401.00002")
    assert open_rec.github_url == "https://github.com/team/repo"


def test_second_run_deduplicates(db, subscription):
    kwargs = dict(fetcher=_fake_fetcher, embedder=LocalEmbedder())
    first = run_subscription(db, subscription, **kwargs)
    second = run_subscription(db, subscription, **kwargs)
    assert first["added"] == 2
    assert second["added"] == 0
    assert db.query(Recommendation).count() == 2


def test_fetcher_failure_records_status(db, subscription):
    out = run_subscription(
        db, subscription, fetcher=_failing_fetcher, embedder=LocalEmbedder()
    )
    assert out["status"] == "failed"
    assert subscription.last_status.startswith("failed")


def test_local_paper_feeds_relevance(db, subscription):
    db.add(Paper(
        title="Neural Retrieval Foundations", status="parsed",
        content_hash="local-1", abstract="neural networks for retrieval",
    ))
    db.commit()
    run_subscription(
        db, subscription, fetcher=_fake_fetcher, embedder=LocalEmbedder()
    )
    recs = db.query(Recommendation).all()
    all_reasons = " ".join(r for rec in recs for r in rec.reasons)
    # 本地论文与候选共享 neural/retrieval 词，应出现本地库理由
    assert "本地库" in all_reasons
