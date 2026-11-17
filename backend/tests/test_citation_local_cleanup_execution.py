"""Current executor, actual fixed-source and SQLite recovery; synthetic model."""
from copy import deepcopy
import json
import re

import pytest

from app import cancel_store, run_lifecycle
from app.agent import source_attribution
from app.llm import LLMResponse
from tests.test_citation_answer_rewrite_execution_review import (
    Model, setup, saved, run, cleanup, CORRECTED)
from tests.test_rewritten_quality_execution import preserve_known_review


EXTRA = "另一个结果为999项。[^e1]"
REWRITTEN = CORRECTED + "\n\n" + EXTRA + "\n\n"


class CleanupModel(Model):
    def __init__(self, quality="passed", rewrite_output=REWRITTEN):
        super().__init__()
        self.quality = quality
        self.corrections = []
        self.rewrite_output = rewrite_output

    def chat(self, messages, **kwargs):
        text, payload = messages[0]["content"], messages[-1]["content"]
        if "语义修订后最终答案的独立质量核验员" in text:
            self.quality_reviews.append(deepcopy(messages))
            if self.quality == "invalid":
                return LLMResponse("invalid JSON")
            answer = json.loads(payload)["answer"]
            issues = [] if self.quality == "passed" else [{"kind": "unresolved_request",
                "text": answer, "related_text": None,
                "user_constraint": {"receipt_id": "question", "quote": "解释方法"},
                "reason": "Synthetic omitted required method detail", "confirmed": True}]
            return LLMResponse(json.dumps({"quality_issues": issues}))
        if text == "你是严谨的引用审计员，只输出 JSON。":
            self.audits.append(deepcopy(messages))
            pairs = re.findall(r"\[论断 (\d+)\] ([^\r\n]+)", payload)
            return LLMResponse(json.dumps({"results": [{"index": int(i),
                "supported": "99个" not in claim and "999项" not in claim,
                "reason": "Synthetic source verdict", "support_kind": "source",
                "process_scope": "none", "supports": []} for i, claim in pairs]}))
        self.rewrites.append((deepcopy(messages), kwargs))
        assert len(self.rewrites) == 1, "Do not rebuy the semantic rewrite"
        return LLMResponse(self.rewrite_output)

    def stream_chat(self, messages, tools=None):
        self.corrections.append(deepcopy(messages))
        yield "final", LLMResponse(CORRECTED)


@pytest.mark.parametrize("quality", ["passed", "invalid", "failed"])
def test_cleanup_rechecks_sources_and_goal_quality_before_completion(db, monkeypatch, quality):
    model = CleanupModel(quality)
    monkeypatch.setattr(source_attribution, "review_source_attribution", preserve_known_review)
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db, binding["run_id"], value)
            db.commit()
        result = next(value for kind, value in run(db, cid, binding, checkpoint, persist) if kind == "answer")
        assert result["refusal"] == 0 and result["answer"].strip() == CORRECTED
        assert result["audit"]["status"] == "passed" and len(model.rewrites) == 1
        assert len(model.audits) >= 3  # original, rewritten, cleaned
        assert result["completed"] is (quality == "passed")
        if quality != "passed":
            assert result["outcome"] == "partial"
        if quality == "failed":
            assert len(model.corrections) == 1 and len(model.quality_reviews) == 2
            assert result["answer_quality"]["known_failed_count"] > 0
        else:
            assert len(model.quality_reviews) == 1
        snapshot = saved(db, binding)
        rows = [row for key, row in snapshot["source_attribution_reviews"].items()
                if key.startswith("citation_local_cleanup/")]
        assert len(rows) == 1 and rows[0]["semantic_verification"] == "not_performed"
    finally:
        cleanup(binding)


def test_cancel_after_saved_cleanup_resumes_without_new_rewrite_or_lost_answer(db, monkeypatch):
    model = CleanupModel()
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    cancelled = []
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db, binding["run_id"], value)
            db.commit()
            if any(k.startswith("citation_local_cleanup/") for k in value["source_attribution_reviews"]) and not cancelled:
                cancelled.append(True)
                cancel_store.request_cancel(binding["run_id"])
        events = run(db, cid, binding, checkpoint, persist)
        assert any(kind == "cancelled" for kind, _ in events)
        snapshot = saved(db, binding)
        assert snapshot["answer"].strip() == CORRECTED and not model.quality_reviews
        cancel_store.clear(binding["run_id"])
        result = next(value for kind, value in run(db, cid, binding, snapshot, persist) if kind == "answer")
        assert result["completed"] is True and result["answer"].strip() == CORRECTED
        assert len(model.rewrites) == len(model.quality_reviews) == 1
    finally:
        cleanup(binding)


def test_inseparable_known_error_still_blocks_and_explains_skipped_quality(db, monkeypatch):
    # Balanced emphasis spans both the supported and rejected sentences, so
    # deleting one raw sentence would change the surviving Markdown scope.
    # The formerly used two plain sentences are now the explicit successful
    # sentence-cleanup fixture in test_d04_execution_current.py.
    model = CleanupModel(rewrite_output="**" + CORRECTED + EXTRA + "**")
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db, binding["run_id"], value)
            db.commit()
        result = next(value for kind, value in run(db, cid, binding, checkpoint, persist) if kind == "answer")
        assert result["refusal"] == 0 and result["completed"] is False
        assert result["outcome"] == "partial"
        assert result["audit"]["known_failed_count"] >= 1
        assert len(model.quality_reviews) == 1 and len(model.rewrites) == 1
        snapshot = saved(db, binding)
        assert not any(k.startswith("citation_local_cleanup/") for k in snapshot["source_attribution_reviews"])
    finally:
        cleanup(binding)
