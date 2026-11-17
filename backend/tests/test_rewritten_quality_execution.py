"""The real executor must not confuse a revised cited answer with completion."""
from copy import deepcopy
import json

import pytest

from app import run_lifecycle
from app.agent import source_attribution
from app.llm import LLMResponse
from tests.test_citation_answer_rewrite_execution_review import (
    Model, setup, saved, run, cleanup, QUESTION, CORRECTED)


BAD = "我无法确认方法。[^e1]"


class QualityModel(Model):
    def __init__(self, *, quality="passed", replacement=CORRECTED):
        super().__init__()
        self.quality, self.replacement, self.corrections = quality, replacement, []

    def chat(self, messages, **kwargs):
        if "语义修订后最终答案的独立质量核验员" in messages[0]["content"]:
            self.quality_reviews.append(deepcopy(messages))
            data = json.loads(messages[-1]["content"])
            if self.quality == "invalid":
                return LLMResponse("not valid JSON")
            fail = self.quality == "always_failed" or (self.quality == "repair" and data["answer"] == BAD)
            issues = [{"kind": "unresolved_request", "text": data["answer"], "related_text": None,
                "user_constraint": {"receipt_id": "question", "quote": "解释方法"},
                "reason": "已提供材料说明方法，但最终回答只说无法确认。", "confirmed": True}] if fail else []
            return LLMResponse(json.dumps({"quality_issues": issues}, ensure_ascii=False))
        result = super().chat(messages, **kwargs)
        if self.rewrites and len(self.audits) == 1:
            # Source/rewrite verdicts are synthetic; this isolates the later
            # goal-quality gate without claiming semantic model accuracy.
            result.content = self.replacement
        return result

    def stream_chat(self, messages, tools=None):
        self.corrections.append(deepcopy(messages))
        yield "final", LLMResponse(CORRECTED)


def preserve_known_review(*args, **kwargs):
    return {"mode": "source_attribution", "status": "not_applicable", "passed": False,
        "verified": True, "unsupported": [], "claims": [], "unverified_count": 0,
        "quality_status": "unverified", "quality_verified": False, "quality_issues": []}


@pytest.mark.parametrize("quality", ["passed", "invalid"])
def test_rewritten_answer_has_explicit_quality_and_unknown_stays_partial(db, monkeypatch, quality):
    model = QualityModel(quality=quality)
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db, binding["run_id"], value); db.commit()
        events = run(db, cid, binding, checkpoint, persist)
        result = next(value for kind, value in events if kind == "answer")
        assert result["refusal"] == 0 and result["answer"] == CORRECTED
        assert len(model.rewrites) == len(model.quality_reviews) == 1
        assert result["answer_quality"]["status"] == ("passed" if quality == "passed" else "unverified")
        assert result["completed"] is (quality == "passed")
        if quality == "invalid":
            assert result["outcome"] == "partial" and result["answer_quality"]["known_failed_count"] == 0
            assert result["incomplete_steps"][-1]["reason"] == "answer_quality_unresolved"
            assert not model.corrections
    finally:
        cleanup(binding)


@pytest.mark.parametrize("quality", ["repair", "always_failed"])
def test_missing_goal_is_corrected_once_without_rebuying_citation_rewrite(db, monkeypatch, quality):
    model = QualityModel(quality=quality, replacement=BAD)
    monkeypatch.setattr(source_attribution, "review_source_attribution", preserve_known_review)
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    try:
        durable = []
        def persist(value):
            durable.append(deepcopy(value))
            run_lifecycle.save_checkpoint(db, binding["run_id"], value); db.commit()
        result = next(value for kind, value in run(db, cid, binding, checkpoint, persist) if kind == "answer")
        assert len(model.rewrites) == len(model.corrections) == 1
        assert len(model.quality_reviews) == 2
        assert any(any(r.get("repair_claimed") for r in s["answer_quality_repairs"].values()) for s in durable)
        assert result["answer"] == CORRECTED and result["refusal"] == 0
        assert result["completed"] is (quality == "repair")
        if quality == "always_failed":
            assert result["outcome"] == "partial" and result["answer_quality"]["known_failed_count"] > 0
    finally:
        cleanup(binding)


def test_cited_answer_without_semantic_rewrite_does_not_buy_extra_quality_review(db, monkeypatch):
    model = QualityModel()
    cid, binding, checkpoint = setup(db, monkeypatch, model, answer=CORRECTED)
    try:
        result = next(value for kind, value in run(db, cid, binding, checkpoint, lambda value: None) if kind == "answer")
        assert result["answer"] == CORRECTED and not model.rewrites and not model.quality_reviews
    finally:
        cleanup(binding)
