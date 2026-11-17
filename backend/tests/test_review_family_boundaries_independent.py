"""Independent positive and negative review-family boundaries.

Uses the real API adapter and local transport from the separate independent
probe. No provider credentials, external HTTP, or production file mutations.
"""
from copy import deepcopy

import pytest

from app import call_ledger
from app.llm import APIClient

from .test_review_family_independent import (
    ANSWER, SUPPLEMENT, alter_instructions, controlled_provider, invoke,
)


def _interrupted_dispatch(db, stage, model, journal, requests, answer):
    """Fail checkpoint only after an actual-path completed local request.

    Tolerate separate metadata checkpoints: the retained snapshot must be the
    last successful predispatch state, not an assumed numbered save call.
    """
    before = len(requests)
    durable = []

    def save():
        if len(requests) > before:
            rows = call_ledger.list_calls()["items"]
            assert len(rows) == len(requests)
            assert all(row["state"] == "completed" for row in rows)
            raise OSError("fabricated loss after completed local review response")
        durable.append(deepcopy(journal))

    with pytest.raises(OSError):
        invoke(db, stage, model, journal, answer=answer, persist=save)
    assert len(requests) == before + 1 and durable
    return deepcopy(durable[-1])


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("unknown_state", ["pending", "unknown"])
def test_unresolved_same_stage_dispatch_precedes_a_previous_exact_success(
        db, controlled_provider, stage, unknown_state):
    requests = controlled_provider
    model, journal = APIClient(), {}
    assert invoke(db, stage, model, journal)["passed"]
    assert len(requests) == 1
    changed = ANSWER + SUPPLEMENT
    restored = _interrupted_dispatch(db, stage, model, journal, requests, changed)
    assert len(requests) == 2
    if unknown_state == "unknown":
        same = invoke(db, stage, model, restored, answer=changed)
        assert same["error_code"] == "dispatch_outcome_unknown"
        assert len(requests) == 2
    result = invoke(db, stage, model, restored)
    assert len(requests) == 2
    assert result.get("error_code") == "dispatch_outcome_unknown"
    assert not result["passed"] and result.get("verified") is not True


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("unknown_state", ["pending", "unknown"])
def test_other_stage_unknown_does_not_block_first_review_in_this_stage(
        db, controlled_provider, stage, unknown_state):
    requests = controlled_provider
    model, journal = APIClient(), {}
    other = "scope" if stage == "audit" else "audit"
    restored = _interrupted_dispatch(db, other, model, journal, requests, ANSWER)
    if unknown_state == "unknown":
        same = invoke(db, other, model, restored)
        assert same["error_code"] == "dispatch_outcome_unknown"
        assert len(requests) == 1
    result = invoke(db, stage, model, restored)
    assert result["passed"] and len(requests) == 2
    assert len(call_ledger.list_calls()["items"]) == 2


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("mutation", ["answer", "instructions"])
def test_known_completed_result_replays_exactly_but_does_not_approve_changed_content(
        db, controlled_provider, monkeypatch, stage, mutation):
    requests = controlled_provider
    model, journal = APIClient(), {}
    assert invoke(db, stage, model, journal)["passed"]
    assert invoke(db, stage, model, journal)["passed"]
    assert len(requests) == 1
    answer = ANSWER + SUPPLEMENT if mutation == "answer" else ANSWER
    if mutation == "instructions":
        alter_instructions(monkeypatch, stage)
    changed = invoke(db, stage, model, journal, answer=answer)
    assert changed["passed"] and len(requests) == 2
    assert len(call_ledger.list_calls()["items"]) == 2
