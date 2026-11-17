"""Independent executor recovery checks; all semantic replies are synthetic."""
from copy import deepcopy
import json

import pytest

from app import cancel_store, run_lifecycle
from app.agent import rewritten_answer_quality as quality
from app.llm import LLMResponse
from tests.test_citation_answer_rewrite_execution_review import (
    Model, PowerLoss, setup, saved, run, cleanup, CORRECTED, USAGE)


QUALITY_USAGE = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


class BoundaryModel(Model):
    def __init__(self, *, lose_quality=False):
        super().__init__()
        self.lose_quality = lose_quality

    def chat(self, messages, **kwargs):
        if "语义修订后最终答案的独立质量核验员" in messages[0]["content"]:
            self.quality_reviews.append(deepcopy(messages))
            if self.lose_quality:
                raise PowerLoss("Synthetic power loss after quality intent")
            return LLMResponse('{"quality_issues":[]}', usage=QUALITY_USAGE)
        return super().chat(messages, **kwargs)


def persisted_quality(value):
    return [row for key, row in value["source_attribution_reviews"].items()
        if key.startswith("source_review/") and row.get("quality_status") == "passed"]


def persist(db, binding, value):
    run_lifecycle.save_checkpoint(db, binding["run_id"], value)
    db.commit()


def test_executor_quality_receipt_and_usage_survive_cancel_and_resume_without_repurchase(db, monkeypatch):
    model = BoundaryModel()
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    cancelled = []
    def save(value):
        persist(db, binding, value)
        if persisted_quality(value) and not cancelled:
            cancelled.append(True)
            cancel_store.request_cancel(binding["run_id"])
    try:
        events = run(db, cid, binding, checkpoint, save)
        assert any(kind == "cancelled" for kind, _ in events)
        checkpoint = saved(db, binding)
        assert checkpoint["phase"] == "finalize" and checkpoint["answer"] == CORRECTED
        assert persisted_quality(checkpoint)
        assert checkpoint["usage"] == {key: USAGE[key] + QUALITY_USAGE[key] for key in USAGE}
        cancel_store.clear(binding["run_id"])
        result = next(value for kind, value in run(db, cid, binding, checkpoint, save) if kind == "answer")
        assert result["answer"] == CORRECTED and result["completed"] and result["refusal"] == 0
        assert result["answer_quality"]["status"] == "passed"
        assert result["cumulative_usage"] == checkpoint["usage"]
        assert len(model.rewrites) == len(model.quality_reviews) == 1 and len(model.audits) == 2
    finally:
        cleanup(binding)


@pytest.mark.parametrize("upgrade_policy", [False, True])
def test_executor_pending_quality_keeps_answer_partial_and_does_not_rebuy_even_after_policy_upgrade(db, monkeypatch, upgrade_policy):
    model = BoundaryModel(lose_quality=True)
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    try:
        with pytest.raises(PowerLoss):
            run(db, cid, binding, checkpoint, lambda value: persist(db, binding, value))
        checkpoint = saved(db, binding)
        assert checkpoint["answer"] == CORRECTED
        pending = [row for key, row in checkpoint["source_attribution_reviews"].items()
            if key.startswith("source_review/") and row.get("review_dispatch") == "pending"]
        assert pending == [{"review_dispatch": "pending"}] and len(model.quality_reviews) == 1
        if upgrade_policy:
            original = quality.quality_only_instructions()
            monkeypatch.setattr(quality, "quality_only_instructions", lambda: original + " Updated synthetic quality policy.")
            monkeypatch.setattr(quality, "PROTOCOL", quality.PROTOCOL + 1)
        model.lose_quality = False
        events = run(db, cid, binding, checkpoint, lambda value: persist(db, binding, value))
        result = next(value for kind, value in events if kind == "answer")
        assert result["answer"] == CORRECTED and result["refusal"] == 0
        assert not result["completed"] and result["outcome"] == "partial"
        assert result["answer_quality"]["status"] == "unverified"
        assert result["answer_quality"]["known_failed_count"] == 0 and not result["answer_quality"]["issues"]
        assert any(value.get("audit", {}).get("error_code") == "dispatch_outcome_unknown" for _, value in events)
        assert len(model.rewrites) == len(model.quality_reviews) == 1 and len(model.audits) == 2
    finally:
        cleanup(binding)


def test_executor_new_policy_cannot_reuse_old_known_pass_but_only_buys_one_current_review(db, monkeypatch):
    model = BoundaryModel()
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    cancelled = []
    def save(value):
        persist(db, binding, value)
        if persisted_quality(value) and not cancelled:
            cancelled.append(True)
            cancel_store.request_cancel(binding["run_id"])
    try:
        first = run(db, cid, binding, checkpoint, save)
        assert any(kind == "cancelled" for kind, _ in first)
        checkpoint = saved(db, binding)
        old_rows = deepcopy(persisted_quality(checkpoint))
        assert old_rows and len(model.quality_reviews) == 1
        cancel_store.clear(binding["run_id"])
        original = quality.quality_only_instructions()
        monkeypatch.setattr(quality, "quality_only_instructions", lambda: original + " Current synthetic policy requires new assessment.")
        result = next(value for kind, value in run(db, cid, binding, checkpoint, save) if kind == "answer")
        assert result["completed"] and result["answer_quality"]["status"] == "passed"
        assert len(model.quality_reviews) == 2 and len(model.rewrites) == 1
        assert result["cumulative_usage"] == {key: USAGE[key] + 2 * QUALITY_USAGE[key] for key in USAGE}
        assert len(persisted_quality(saved(db, binding))) == 2
    finally:
        cleanup(binding)


def test_executor_recovered_applied_revision_without_quality_budget_is_explicitly_not_assessed(db, monkeypatch):
    model = BoundaryModel()
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    stopped = []
    def stop_after_applied(value):
        persist(db, binding, value)
        applied = [row for key, row in value["source_attribution_reviews"].items()
            if key.startswith("citation_rewrite/") and row.get("answer_applied") is True]
        if applied and not stopped:
            stopped.append(True)
            raise PowerLoss("Synthetic stop before post-revision quality admission")
    try:
        with pytest.raises(PowerLoss):
            run(db, cid, binding, checkpoint, stop_after_applied)
        checkpoint = saved(db, binding)
        assert checkpoint["answer"] == CORRECTED and len(model.quality_reviews) == 0
        assert not [key for key in checkpoint["source_attribution_reviews"] if key.startswith("source_review/")]
        checkpoint["budget_exhausted"] = True
        result = next(value for kind, value in run(db, cid, binding, checkpoint,
            lambda value: persist(db, binding, value)) if kind == "answer")
        assert result["refusal"] == 0 and CORRECTED in result["answer"]
        assert not result["completed"] and result["outcome"] == "partial"
        assert result["answer_quality"]["status"] == "not_assessed"
        assert result["answer_quality"]["known_failed_count"] == 0 and not result["answer_quality"]["issues"]
        assert len(model.quality_reviews) == 0 and len(model.rewrites) == 1
    finally:
        cleanup(binding)
