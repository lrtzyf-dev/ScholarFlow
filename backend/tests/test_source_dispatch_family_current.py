"""Exact result keys must not double-purchase a pending user revision."""
from copy import deepcopy
import json

import pytest

from app.agent.paid_review_family import FIELD, family_identity
from app.agent.source_review_journal import cached_source_review


def family(question="当前用户要求"):
    return family_identity("source_attribution", question=question,
        messages=[{"role": "user", "content": question}])


def invoke(journal, calls, *, key="a" * 64, current_family=None, review=None, persist=lambda: None):
    return cached_source_review(journal, key, contract_id="b" * 64,
        review=review or (lambda: calls.append("review") or {"status": "passed", "verified": True}),
        persist=persist, before_review=lambda: None,
        dispatch_family=current_family or family())


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_changed_exact_key_cannot_hide_old_unscoped_intent(legacy, state):
    key = "c" * 64 if legacy else "source_review/" + "c" * 64 + "/" + "d" * 64
    record = {"review_dispatch": "pending"} if state == "pending" else {"error_code": "dispatch_outcome_unknown"}
    journal, calls = {key: record}, []
    for _ in range(3):
        journal = json.loads(json.dumps(journal))
        result = invoke(journal, calls)
        assert result["error_code"] == "dispatch_outcome_unknown"
        assert set(journal) == {key} and FIELD not in journal[key]
    assert calls == []


def test_new_intent_is_stamped_before_dispatch_and_blocks_changed_key_after_json_recovery():
    class LostPower(BaseException):
        pass
    journal, calls, saves = {}, [], []
    def lose():
        calls.append("dispatched")
        assert next(iter(saves[-1].values()))[FIELD] == family()
        raise LostPower()
    with pytest.raises(LostPower):
        invoke(journal, calls, review=lose, persist=lambda: saves.append(deepcopy(journal)))
    restored = json.loads(json.dumps(saves[-1]))
    value = invoke(restored, calls, key="e" * 64)
    assert value["error_code"] == "dispatch_outcome_unknown" and calls == ["dispatched"]
    assert FIELD not in value


def test_new_actual_user_revision_may_dispatch_but_does_not_rewrite_prior_unknown():
    prior_key = "source_review/" + "c" * 64 + "/" + "d" * 64
    prior = {"review_dispatch": "pending", FIELD: family()}
    journal, calls = {prior_key: deepcopy(prior)}, []
    value = invoke(journal, calls, current_family=family("新的实际用户要求"))
    assert value["status"] == "passed" and calls == ["review"]
    assert journal[prior_key] == prior and FIELD not in value


def test_pending_to_unknown_retains_its_original_family_for_a_genuinely_new_user_request():
    key = "source_review/" + "c" * 64 + "/" + "d" * 64
    journal, calls = {key: {"review_dispatch": "pending", FIELD: family()}}, []
    value = invoke(journal, calls)
    assert value["error_code"] == "dispatch_outcome_unknown"
    assert journal[key][FIELD] == family()
    current = invoke(json.loads(json.dumps(journal)), calls, current_family=family("另一条真实用户指令"))
    assert current["status"] == "passed" and calls == ["review"]


@pytest.mark.parametrize("value", [None, {}, {"protocol": "paid_review_user_revision_v1",
    "stage": "some_other_stage", "user_revision": None}])
def test_invalid_family_metadata_cannot_hide_an_unknown(value):
    journal = {"source_review/" + "c" * 64 + "/" + "d" * 64:
        {"review_dispatch": "pending", FIELD: value}}
    before, calls = deepcopy(journal), []
    result = invoke(journal, calls)
    assert result["error_code"] == "journal_invalid" and not calls and journal == before
