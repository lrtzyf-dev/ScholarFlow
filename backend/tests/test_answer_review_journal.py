"""A lost paid-review response is not an invitation to buy the call again."""
from copy import deepcopy

import pytest

from app.agent.answer_review_journal import cached_review


class PowerLoss(BaseException):
    pass


def test_intent_is_durable_before_dispatch_and_completed_review_is_reused():
    journal, saved, calls = {}, [], []
    def review():
        assert saved[-1]["key"] == {"review_dispatch": "pending"}
        calls.append(1)
        return {"status": "failed", "unsupported": [{"text": "a"}]}
    result = cached_review(journal, "key", review=review,
        persist=lambda: saved.append(deepcopy(journal)), before_review=lambda: None)
    assert calls == [1] and saved[-1]["key"] == result
    result["unsupported"].clear()
    restored = cached_review(journal, "key", review=lambda: pytest.fail("duplicate purchase"),
        persist=lambda: pytest.fail("completed receipt needs no rewrite"), before_review=lambda: None)
    assert restored["unsupported"] == [{"text": "a"}]


def test_recovered_pending_is_unknown_without_dispatch_and_remains_unknown():
    journal, saved = {"key": {"review_dispatch": "pending"}}, []
    def resume():
        return cached_review(journal, "key", review=lambda: pytest.fail("unknown may have been charged"),
            persist=lambda: saved.append(deepcopy(journal)), before_review=lambda: None)
    assert resume()["error_code"] == "dispatch_outcome_unknown"
    assert resume()["status"] == "unverified" and len(saved) == 1


def test_failed_intent_checkpoint_prevents_network():
    with pytest.raises(OSError):
        cached_review({}, "key", review=lambda: pytest.fail("must not dispatch"),
            persist=lambda: (_ for _ in ()).throw(OSError()), before_review=lambda: None)


def test_power_loss_after_dispatch_keeps_uncertain_pending():
    journal, saved = {}, []
    with pytest.raises(PowerLoss):
        cached_review(journal, "key", review=lambda: (_ for _ in ()).throw(PowerLoss()),
            persist=lambda: saved.append(deepcopy(journal)), before_review=lambda: None)
    result = cached_review(saved[-1], "key", review=lambda: pytest.fail("duplicate"),
        persist=lambda: None, before_review=lambda: None)
    assert result["error_code"] == "dispatch_outcome_unknown"


def test_completed_receipt_is_saved_before_post_call_cancellation():
    journal, saved, boundaries = {}, [], []
    def boundary():
        boundaries.append(1)
        if len(boundaries) == 2:
            raise PowerLoss()
    with pytest.raises(PowerLoss):
        cached_review(journal, "key", review=lambda: {"status": "passed", "usage": {"total_tokens": 9}},
            persist=lambda: saved.append(deepcopy(journal)), before_review=boundary)
    assert saved[-1]["key"]["usage"]["total_tokens"] == 9
    assert cached_review(saved[-1], "key", review=lambda: pytest.fail("duplicate"),
        persist=lambda: None, before_review=lambda: None)["status"] == "passed"


def test_result_save_failure_retains_pending_on_disk_and_does_not_resend():
    journal, saved = {}, []
    def save():
        if saved:
            raise OSError()
        saved.append(deepcopy(journal))
    with pytest.raises(OSError):
        cached_review(journal, "key", review=lambda: {"status": "passed"},
            persist=save, before_review=lambda: None)
    assert cached_review(saved[-1], "key", review=lambda: pytest.fail("possibly charged"),
        persist=lambda: None, before_review=lambda: None)["status"] == "unverified"
