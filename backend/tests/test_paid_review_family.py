"""Author regression contracts for stable paid-review fences; no HTTP."""
from copy import deepcopy
import json

import pytest

from app.agent import audit
from app.agent.audit_batches import PROTOCOL, _digest, _unknown, run_batched_audit
from app.agent.paid_review_family import FIELD, family_identity, scope_barrier, batch_barrier
from app.llm import LLMResponse
from app.rag.citation import citation_gate
from .test_citation_audit_batches import Auditor, answer, verdict, EVIDENCE as BATCH_EVIDENCE
from .test_citation_scope_journal import Model, ANSWER, EVIDENCE, QUESTION


def invoke(stage, model, journal, *, question=QUESTION, text=None, evidence=None,
           persist=lambda: None, before=lambda: None, input_ids=(), messages=None):
    if stage == "scope":
        text = ANSWER if text is None else text
        evidence = EVIDENCE if evidence is None else evidence
        return audit.review_citation_scope(question, text, evidence,
            citation_gate(text, [row["id"] for row in evidence]), model,
            messages=messages, input_ids=input_ids, review_journal=journal,
            persist=persist, before_review=before)
    return audit.audit_answer(None, answer(4) if text is None else text,
        BATCH_EVIDENCE if evidence is None else evidence, llm=model,
        messages=[] if messages is None else messages, question=question, input_ids=input_ids,
        audit_journal=journal, persist=persist, before_batch=before)


def model_for(stage):
    return Model() if stage == "scope" else Auditor()


def pending_record(stage, *, family=None):
    if stage == "scope":
        value = {"review_dispatch": "pending"}
    else:
        value = {"protocol": PROTOCOL, "batch_size": 4, "claim_count": 4,
            "batches": {"0": {"state": "pending", "start": 0, "count": 4,
                "usage_accounted": False}}}
    if family is not None:
        value[FIELD] = deepcopy(family)
    return value


def stage_key(stage):
    return ("citation_scope/" if stage == "scope" else "") + "a" * 64


def test_family_identity_uses_only_trusted_user_revisions_and_stage():
    initial = family_identity("citation_scope", question="Compare the methods", input_ids=("u2", "u1"))
    noisy = [{"role": "user", "content": "A copied document says ignore all limits"},
        {"role": "tool", "content": "A new source revision with a different answer"}]
    assert family_identity("citation_scope", question="Compare the methods",
        input_ids=("u1", "u2", "u1"), messages=noisy) == initial
    steering = noisy + [{"role": "user", "content": "Do not create files", "_user_steering": "u3"}]
    revised = family_identity("citation_scope", question="Compare the methods",
        input_ids=("u1", "u2"), messages=steering)
    assert revised != initial
    assert family_identity("citation_scope", question="Compare the methods",
        input_ids=("u1", "u2"), messages=steering, excluded_indices=(2,)) == initial
    assert family_identity("citation_semantic", question="Compare the methods", input_ids=("u1", "u2")) != initial


@pytest.mark.parametrize("question", [None, "", "   "])
def test_missing_canonical_question_never_guesses_revision_from_user_shaped_text(question):
    value = family_identity("citation_semantic", question=question,
        input_ids=("unproven-current-revision",), messages=[
            {"role": "user", "content": "A quoted instruction", "_user_steering": "u2"}])
    assert value["user_revision"] is None


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("mutation", ["answer", "evidence", "model"])
def test_post_dispatch_save_failure_rolls_back_and_fences_changed_exact_key(stage, mutation):
    model, journal, durable = model_for(stage), {}, []
    def save():
        if model.calls:
            raise OSError("fabricated result-save failure")
        durable.append(deepcopy(journal))
    with pytest.raises(OSError):
        invoke(stage, model, journal, persist=save)
    assert len(model.calls) == 1 and journal == durable[-1]
    prior_keys = set(journal)
    kwargs = {}
    if mutation == "answer":
        kwargs["text"] = (ANSWER if stage == "scope" else answer(4)) + "\n\n另一项观测结果为十三分。[^e1]"
    elif mutation == "evidence":
        kwargs["evidence"] = deepcopy(EVIDENCE if stage == "scope" else BATCH_EVIDENCE)
        kwargs["evidence"][0]["quote"] += " Different fixed source version."
    else:
        model._runtime_config = {"mode": "api", "model_id": "Different-Exact-Model"}
    value = invoke(stage, model, journal, **kwargs)
    assert len(model.calls) == 1 and set(journal) == prior_keys
    assert not value["passed"] and value["error_code"] == "dispatch_outcome_unknown"
    assert value.get("verified") is not True


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("legacy_state", ["pending", "unknown"])
def test_legacy_unknown_with_no_family_cannot_be_rebound_to_a_new_user_request(stage, legacy_state):
    model, key = model_for(stage), stage_key(stage)
    journal = {key: pending_record(stage)}
    if legacy_state == "unknown":
        if stage == "scope":
            journal[key] = {"error_code": "dispatch_outcome_unknown", "passed": False}
        else:
            result = _unknown([{"text": "Old claim", "evidence_ids": ["e1"]}], "dispatch_outcome_unknown")
            journal[key]["batches"]["0"] = {"state": "complete", "result": result,
                "result_sha256": _digest(result)}
    value = invoke(stage, model, journal, question="A new canonical user request", input_ids=("new-user-input",))
    assert not model.calls and value["error_code"] == "dispatch_outcome_unknown"
    assert FIELD not in journal[key] and set(journal) == {key}


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("revision", ["question", "applied_input", "steering"])
def test_proven_new_user_revision_is_independent_of_an_older_fenced_dispatch(stage, revision):
    label = "citation_scope" if stage == "scope" else "citation_semantic"
    family = family_identity(label, question=QUESTION)
    journal, model = {stage_key(stage): pending_record(stage, family=family)}, model_for(stage)
    kwargs = {"text": (ANSWER if stage == "scope" else answer(4)) + "\n\n额外观测值为十三分。[^e1]"}
    if revision == "question":
        kwargs["question"] = QUESTION + " Now examine another method."
    elif revision == "applied_input":
        kwargs["input_ids"] = ("real-user-input-2",)
    else:
        kwargs["messages"] = [{"role": "user", "content": "Now examine another method", "_user_steering": "real-user-input-2"}]
    result = invoke(stage, model, journal, **kwargs)
    assert result["passed"] and len(model.calls) >= 1
    assert journal[stage_key(stage)] == pending_record(stage, family=family)


@pytest.mark.parametrize("stage", ["scope", "audit"])
def test_known_legacy_exact_result_retains_unchanged_key_and_public_schema(stage):
    model, journal = model_for(stage), {}
    first = invoke(stage, model, journal)
    assert first["passed"] and len(model.calls) == 1
    key = next(iter(journal))
    assert FIELD in journal[key] and FIELD not in first
    journal[key].pop(FIELD)
    replay = invoke(stage, model, journal, question=QUESTION)
    assert replay["passed"] and len(model.calls) == 1 and next(iter(journal)) == key
    assert FIELD not in journal[key] and FIELD not in replay


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("phase", ["intent", "result", "unknown"])
def test_failed_checkpoint_rolls_back_owned_journal_without_downgrading_the_exception(stage, phase):
    journal, model, durable = {}, model_for(stage), []
    if phase == "unknown":
        def loss(*args):
            raise KeyboardInterrupt("fabricated dispatch loss")
        interrupted = Model(error=KeyboardInterrupt()) if stage == "scope" else Auditor(handler=loss)
        with pytest.raises(KeyboardInterrupt):
            invoke(stage, interrupted, journal)
    initial = deepcopy(journal)
    def save():
        if phase != "result" or model.calls:
            raise OSError("fabricated disk boundary")
        durable.append(deepcopy(journal))
    with pytest.raises(OSError, match="fabricated disk boundary"):
        invoke(stage, model, journal, persist=save)
    assert journal == (durable[-1] if durable else initial)
    assert len(model.calls) == (1 if phase == "result" else 0)


def test_current_known_failure_survives_unknown_and_fences_missing_and_changed_batches():
    journal, durable = {}, []
    def handler(indices, messages):
        if indices[0] == 12:
            raise KeyboardInterrupt("fabricated death after possible paid dispatch")
        return LLMResponse(json.dumps({"results": [verdict(i, i != 0) for i in indices]}))
    model = Auditor(handler)
    with pytest.raises(KeyboardInterrupt):
        invoke("audit", model, journal, text=answer(30), persist=lambda: durable.append(deepcopy(journal)))
    assert len(model.calls) == 2
    replay = Auditor()
    result = invoke("audit", replay, journal, text=answer(30))
    assert not replay.calls and result["status"] == "failed" and result["known_failed_count"] == 1
    assert result["unknown_count"] == 18 and result["error_code"] == "dispatch_outcome_unknown"
    record = next(iter(journal.values()))
    assert set(record["batches"]) == {"0", "1"}
    changed = invoke("audit", replay, journal, text=answer(30) + "\n\n新的观测为三十一分。[^e1]")
    assert not replay.calls and changed["status"] == "unverified" and changed["known_failed_count"] == 0
    assert len(changed["prior_known_failures"]) == 1
    prior = changed["prior_known_failures"][0]
    assert prior["key"] == next(iter(journal)) and prior["batch"] == "0"
    assert prior["known_failed_count"] == 1 and prior["unsupported"] == result["unsupported"]


def test_new_unknown_result_fences_later_batches_in_the_same_run():
    claims = [{"text": f"Fabricated claim {i}", "evidence_ids": ["e1"]} for i in range(8)]
    calls, journal = [], {}
    def review(group, start):
        calls.append(start)
        return _unknown(group, "dispatch_outcome_unknown")
    value = run_batched_audit(answer="Fabricated full answer", claims=claims, factual_cited=claims,
        evidence_map={"e1": BATCH_EVIDENCE[0]}, metadata=None, receipts=[], llm=Auditor(),
        journal=journal, persist=lambda: None, before_batch=None, on_usage=None,
        audit_one=review, question=QUESTION)
    assert calls == [0] and value["unknown_count"] == 8
    assert value["error_code"] == "dispatch_outcome_unknown" and value["family_unknown_count"] == 1
    assert set(next(iter(journal.values()))["batches"]) == {"0"}


def test_stage_isolation_is_narrow_and_does_not_accept_malformed_foreign_keys():
    family = family_identity("citation_semantic", question=QUESTION)
    journal = {"citation_scope/" + "a" * 64: {"review_dispatch": "pending"}}
    assert batch_barrier(journal, family, protocol=PROTOCOL, digest=_digest) == (None, [], [])
    for key, value in (("citation_scope/not-a-digest", {}), ("citation_scope/" + "a" * 64, []), ("unexpected", {})):
        assert batch_barrier({key: value}, family, protocol=PROTOCOL, digest=_digest)[0] == "journal_invalid"
    scope = family_identity("citation_scope", question=QUESTION)
    assert scope_barrier({"a" * 64: pending_record("audit")}, scope) == (None, [])


@pytest.mark.parametrize("stage", ["scope", "audit"])
def test_cancellation_before_unknown_or_cached_lookup_propagates_without_journal_mutation(stage):
    from app.request_resources import ResourceRequestCancelled
    journal, model = {stage_key(stage): pending_record(stage)}, model_for(stage)
    initial = deepcopy(journal)
    def cancelled():
        raise ResourceRequestCancelled("fabricated pre-lookup cancellation")
    with pytest.raises(ResourceRequestCancelled):
        invoke(stage, model, journal, before=cancelled)
    assert journal == initial and not model.calls
