from app.chat_events import RunChannel
import json


def test_live_evidence_and_review_progress_survive_snapshot_resync():
    channel = RunChannel("chat_live_citations", "conversation", durable=False)
    channel.record("evidence", {"evidence": [
        {"id": "e1", "quote": "Verified source excerpt", "paper_title": "Paper A"},
    ]})
    channel.record("review_progress", {"phase": "citation_review", "stage": "semantic_audit",
        "status": "running", "completed": 4, "total": 12, "batch": 2, "total_batches": 3})

    snapshot = channel.snapshot()
    assert snapshot["evidence"] == [{"id": "e1", "quote": "Verified source excerpt", "paper_title": "Paper A"}]
    assert snapshot["review_progress"]["completed"] == 4
    assert snapshot["review_progress"]["total"] == 12

    restored = RunChannel("chat_live_citations", "conversation", saved=snapshot, durable=False)
    restored_snapshot = restored.snapshot()
    assert restored_snapshot["evidence"] == snapshot["evidence"]
    assert restored_snapshot["review_progress"]["stage"] == "semantic_audit"


def test_evidence_events_merge_by_id_and_reset_with_new_answer_attempt():
    channel = RunChannel("chat_live_citations_reset", "conversation", durable=False)
    item = {"id": "e1", "quote": "Source excerpt"}
    channel.record("evidence", {"evidence": [item]})
    channel.record("evidence", {"evidence": [item, {"id": "e2", "quote": "Another excerpt"}]})
    assert [row["id"] for row in channel.snapshot()["evidence"]] == ["e1", "e2"]

    channel.record("answer_reset", {})
    assert channel.snapshot()["evidence"] == []
    assert channel.snapshot()["review_progress"] is None


def test_public_evidence_snapshot_has_a_finite_size():
    channel = RunChannel("chat_live_citations_bounded", "conversation", durable=False)
    public = channel.record("evidence", {"evidence": [
        {"id": f"e{index}", "quote": "x" * 10_000} for index in range(300)
    ]})
    snapshot = channel.snapshot()
    assert len(public["evidence"]) <= 256
    assert len(snapshot["evidence"]) <= 256
    assert len(json.dumps(snapshot["evidence"], ensure_ascii=False)) <= 100_000
