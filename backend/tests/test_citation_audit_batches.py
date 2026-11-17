"""Bounded paid-review checkpoints. Synthetic semantic verdicts, no real calls."""
from copy import deepcopy
import json
import re

import httpx
import pytest

from app.agent.audit import audit_answer
from app.agent.audit_batches import MAX_CLAIMS
from app.agent.checkpoint import AgentState, Checkpointer
from app.llm import IncompleteModelResponse, LLMResponse
from app.request_resources import ResourceRequestCancelled
from app.resource_budget import ResourceBudgetExceeded
from tests.test_citation_review_diagnostics import provider  # noqa: F401 -- shared isolated HTTP fixture

EVIDENCE = [{"id": "e1", "quote": "Fabricated observations 1 through 12, all in points.", "paper_title": "Fixed fixture"}]
USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def answer(count=12):
    return "\n\n".join(f"材料报告第{index + 1}个观测值为{index + 1}分。[^e1]" for index in range(count))


def verdict(index, supported=True, kind="source"):
    return {"index": index, "supported": supported, "reason": "Synthetic bounded verdict",
        "support_kind": kind, "process_scope": "none", "supports": []}


class Auditor:
    mode = "api"
    def __init__(self, handler=None, configuration=None):
        self.calls, self.handler = [], handler
        self._runtime_config = configuration or {"mode": "api", "model_id": "Synthetic-Exact-ID", "profile_version": 1}

    def chat(self, messages, **kwargs):
        indices = [int(value) for value in re.findall(r"^\[论断 (\d+)\]", messages[1]["content"], re.M)]
        self.calls.append(indices)
        assert 0 < len(indices) <= MAX_CLAIMS
        assert "full_answer=" in messages[1]["content"]
        if self.handler:
            return self.handler(indices, messages)
        return LLMResponse(json.dumps({"results": [verdict(index) for index in indices]}), usage=USAGE)


def run(db, model, count=12, **kwargs):
    return audit_answer(db, answer(count), EVIDENCE, llm=model, messages=[], **kwargs)


def durable(tmp_path):
    path = tmp_path / "checkpoint.json"
    state = AgentState(question="Compare all fabricated observations", phase="finalize", answer=answer())
    def persist(snapshot):
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    return state, Checkpointer(state, persist), path


def usage_recorder(state):
    def record(usage):
        for key, value in usage.items():
            state.usage[key] += value
    return record


def test_sixty_two_claims_fit_in_six_bounded_batches_with_exact_indices_and_usage(db):
    model = Auditor()
    value = run(db, model, count=62)
    assert model.calls == [list(range(start, min(start + 12, 62))) for start in range(0, 62, 12)]
    assert value["passed"] and value["known_failed_count"] == value["unknown_count"] == 0
    assert MAX_CLAIMS == 12
    assert value["batch_count"] == 6 and value["factual_claims"] == 62
    assert value["new_usage"] == value["cumulative_usage"] == value["usage"] == {key: number * 6 for key, number in USAGE.items()}


def test_semantic_audit_reports_count_only_progress_after_each_bounded_batch(db):
    model, updates = Auditor(), []
    value = run(db, model, count=62, on_progress=updates.append)
    assert value["passed"]
    assert updates[-1]["status"] == "passed" and updates[-1]["completed"] == 62
    assert updates[-1]["total"] == 62 and updates[-1]["total_batches"] == 6
    assert sorted({row["completed"] for row in updates}) == [0, 12, 24, 36, 48, 60, 62]
    assert all(set(row) <= {"phase", "status", "completed", "total", "batch", "total_batches"}
               for row in updates)
    assert not any("观测值" in str(row) for row in updates)


def test_exact_v1_four_claim_checkpoint_restores_without_rebuying_completed_batches(db, monkeypatch):
    from app.agent import audit_batches
    journal, old_model = {}, Auditor()
    monkeypatch.setattr(audit_batches, "MAX_CLAIMS", 4)
    assert run(db, old_model, count=12, audit_journal=journal)["passed"]
    old_key = next(iter(journal))
    assert journal[old_key]["batch_size"] == 4 and len(old_model.calls) == 3

    monkeypatch.setattr(audit_batches, "MAX_CLAIMS", 12)
    no_rebuy = Auditor(lambda *args: pytest.fail("A complete exact v1 batch must be reused"))
    restored = run(db, no_rebuy, count=12, audit_journal=journal)
    assert restored["passed"] and restored["batch_count"] == 3
    assert no_rebuy.calls == [] and journal[old_key]["batch_size"] == 4


def test_exact_v1_four_claim_pending_dispatch_and_family_change_both_remain_fenced(db, monkeypatch):
    from app.agent import audit_batches
    journal, old_model = {}, Auditor(lambda *_: (_ for _ in ()).throw(
        KeyboardInterrupt("synthetic lost dispatch")))
    monkeypatch.setattr(audit_batches, "MAX_CLAIMS", 4)
    with pytest.raises(KeyboardInterrupt):
        run(db, old_model, count=12, audit_journal=journal)
    old_key = next(iter(journal))
    assert journal[old_key]["batch_size"] == 4
    assert journal[old_key]["batches"]["0"]["state"] == "pending"

    monkeypatch.setattr(audit_batches, "MAX_CLAIMS", 12)
    no_rebuy = Auditor(lambda *args: pytest.fail("An old pending dispatch must fence the family"))
    restored = run(db, no_rebuy, count=12, audit_journal=journal)
    assert restored["error_code"] == "dispatch_outcome_unknown"
    assert restored["unknown_count"] == 12 and no_rebuy.calls == []

    changed = run(db, no_rebuy, count=13, audit_journal=journal)
    assert changed["error_code"] == "dispatch_outcome_unknown"
    assert changed["unknown_count"] == 13 and no_rebuy.calls == []


def test_real_quote_only_v1_context_key_reuses_completed_results_without_rebuy(db, monkeypatch):
    from app.agent import audit_batches, audit_source_context
    journal, old_model = {}, Auditor()
    original_evidence = deepcopy(EVIDENCE)
    with monkeypatch.context() as legacy:
        legacy.setattr(audit_batches, "MAX_CLAIMS", 4)
        legacy.setattr(audit_source_context, "enrich_evidence_for_audit",
            lambda db, evidence: {row["id"]: deepcopy(row) for row in evidence})
        old = run(db, old_model, count=12, audit_journal=journal)
    assert old["passed"] and len(old_model.calls) == 3
    old_key = next(iter(journal))
    enriched = {row["id"]: {**deepcopy(row), "audit_quote": row["quote"] + " More verified context."}
                for row in EVIDENCE}
    monkeypatch.setattr(audit_source_context, "enrich_evidence_for_audit", lambda db, evidence: deepcopy(enriched))
    no_rebuy = Auditor(lambda *args: pytest.fail("An old paid quote-only result must remain reusable"))
    restored = run(db, no_rebuy, count=12, audit_journal=journal)
    assert restored["passed"] and restored["batch_count"] == 3
    assert no_rebuy.calls == [] and list(journal) == [old_key]
    assert EVIDENCE == original_evidence


@pytest.mark.parametrize("interrupted", ["missing", "pending"])
def test_real_quote_only_v1_key_with_missing_or_pending_batches_never_rebuys_context(interrupted, db, monkeypatch):
    from app.agent import audit_batches, audit_source_context
    journal, boundaries = {}, []
    def before():
        boundaries.append(True)
        if interrupted == "missing" and len(boundaries) == 3:
            raise ResourceRequestCancelled("synthetic cancellation after first committed batch")
    def old_handler(indices, messages):
        if interrupted == "pending":
            raise KeyboardInterrupt("synthetic lost old dispatch")
        return LLMResponse(json.dumps({"results": [verdict(i) for i in indices]}), usage=USAGE)
    with monkeypatch.context() as legacy:
        legacy.setattr(audit_batches, "MAX_CLAIMS", 4)
        legacy.setattr(audit_source_context, "enrich_evidence_for_audit",
            lambda db, evidence: {row["id"]: deepcopy(row) for row in evidence})
        with pytest.raises((ResourceRequestCancelled, KeyboardInterrupt)):
            run(db, Auditor(old_handler), count=12, audit_journal=journal, before_batch=before)
    old_key = next(iter(journal))
    enriched = {row["id"]: {**deepcopy(row), "audit_quote": row["quote"] + " More verified context."}
                for row in EVIDENCE}
    monkeypatch.setattr(audit_source_context, "enrich_evidence_for_audit", lambda db, evidence: deepcopy(enriched))
    no_rebuy = Auditor(lambda *args: pytest.fail("Changed context cannot buy a missing old batch"))
    restored = run(db, no_rebuy, count=12, audit_journal=journal)
    assert no_rebuy.calls == [] and list(journal) == [old_key]
    assert restored["error_code"] == ("audit_input_changed" if interrupted == "missing" else "dispatch_outcome_unknown")
    assert restored["unknown_count"] == (8 if interrupted == "missing" else 12)
    assert not restored["passed"] and not restored["verified"]


def test_incomplete_batch_does_not_retry_or_erase_separate_known_rejection(db):
    def handler(indices, messages):
        if indices[0] == 0:
            raise IncompleteModelResponse("SYNTHETIC_PRIVATE_RESPONSE")
        return LLMResponse(json.dumps({"results": [verdict(index, index not in (16, 18),
            "uncertain" if index == 18 else "source") for index in indices]}), usage=USAGE)
    model, journal = Auditor(handler), {}
    value = run(db, model, count=30, audit_journal=journal)
    assert len(model.calls) == 3 and value["status"] == "failed" and not value["verified"]
    assert value["known_failed_count"] == 1 and value["unknown_count"] == 13
    assert value["unsupported_ratio"] == 1 / 30
    assert value["error_code"] == "output_incomplete" and "PRIVATE" not in json.dumps(journal)
    again = run(db, model, count=30, audit_journal=journal)
    assert len(model.calls) == 3 and again["new_usage"] == {} and again["known_failed_count"] == 1
    assert again["unknown_count"] == 13 and again["cumulative_usage"] == value["cumulative_usage"]


def test_returning_old_local_indices_in_next_batch_cannot_skip_its_claims(db):
    def handler(indices, messages):
        return LLMResponse(json.dumps({"results": [verdict(index) for index in range(len(indices))]}))
    model = Auditor(handler)
    value = run(db, model, count=20)
    assert value["unknown_count"] == 8 and not value["verified"] and value["known_failed_count"] == 0
    assert len(model.calls) == 2


def test_checkpoint_resume_only_dispatches_missing_batches_and_accounts_usage_once(db, tmp_path):
    state, saver, path = durable(tmp_path)
    boundaries = []
    def before():
        boundaries.append(True)
        if len(boundaries) == 3:
            raise ResourceRequestCancelled("Synthetic cancellation before second batch")
    model = Auditor()
    with pytest.raises(ResourceRequestCancelled):
        run(db, model, count=30, audit_journal=state.citation_audit_batches, persist=saver.save,
            before_batch=before, on_usage=usage_recorder(state))
    assert len(model.calls) == 1
    restored = AgentState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert restored.usage == USAGE
    resumed_saver = Checkpointer(restored, lambda snapshot: path.write_text(json.dumps(snapshot), encoding="utf-8"))
    resumed_model = Auditor()
    value = run(db, resumed_model, count=30, audit_journal=restored.citation_audit_batches,
        persist=resumed_saver.save, on_usage=usage_recorder(restored))
    assert resumed_model.calls == [list(range(12, 24)), list(range(24, 30))]
    assert value["new_usage"] == {key: number * 2 for key, number in USAGE.items()}
    assert value["cumulative_usage"] == restored.usage == {key: number * 3 for key, number in USAGE.items()}
    no_requests = Auditor(lambda *args: pytest.fail("Completed paid reviews must not be rebought"))
    # The synthetic callback is not part of the exact model identity.
    again = run(db, no_requests, count=30, audit_journal=restored.citation_audit_batches,
        persist=resumed_saver.save, on_usage=usage_recorder(restored))
    assert again["new_usage"] == {} and again["passed"]
    assert restored.usage == value["cumulative_usage"]


def test_persist_failure_before_dispatch_prevents_a_paid_request(db):
    model = Auditor()
    def fail():
        raise OSError("Synthetic checkpoint disk unavailable")
    with pytest.raises(OSError):
        run(db, model, audit_journal={}, persist=fail)
    assert not model.calls


@pytest.mark.parametrize("failure", ["after_dispatch", "usage_callback", "result_save"])
def test_uncommitted_dispatch_is_never_automatically_replayed_after_restart(db, tmp_path, failure):
    state, saver, path = durable(tmp_path)
    saves = []
    def persist():
        saves.append(True)
        if failure == "result_save" and len(saves) == 2:
            raise OSError("Synthetic result checkpoint interrupted")
        saver.save()
    def paid(indices, messages):
        if failure == "after_dispatch":
            raise KeyboardInterrupt("Synthetic process died after uncertain request")
        return LLMResponse(json.dumps({"results": [verdict(index) for index in indices]}), usage=USAGE)
    def record(usage):
        if failure == "usage_callback":
            raise RuntimeError("Synthetic usage callback interruption")
        usage_recorder(state)(usage)
    with pytest.raises((KeyboardInterrupt, RuntimeError, OSError)):
        run(db, Auditor(paid), audit_journal=state.citation_audit_batches, persist=persist, on_usage=record)
    restored = AgentState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    model = Auditor()
    value = run(db, model, audit_journal=restored.citation_audit_batches,
        persist=Checkpointer(restored, lambda snapshot: None).save, on_usage=usage_recorder(restored))
    # The unresolved possibly paid operation now fences this whole user-input
    # revision, including missing later batches. Do not purchase around it.
    assert not model.calls
    assert set(next(iter(restored.citation_audit_batches.values()))["batches"]) == {"0"}
    assert value["unknown_count"] == 12 and value["error_code"] == "dispatch_outcome_unknown"
    assert value["known_failed_count"] == 0 and not value["verified"]


def test_custom_model_resource_boundary_propagates_but_unproven_dispatch_is_not_rebought(db):
    journal = {}
    def denied(indices, messages):
        raise ResourceBudgetExceeded("Synthetic admission denied before HTTP")
    with pytest.raises(ResourceBudgetExceeded):
        run(db, Auditor(denied), audit_journal=journal)
    assert next(iter(journal.values()))["batches"]["0"]["state"] == "pending"
    model = Auditor()
    value = run(db, model, audit_journal=journal)
    assert not model.calls and set(next(iter(journal.values()))["batches"]) == {"0"}
    assert value["unknown_count"] == 12 and value["known_failed_count"] == 0
    assert value["error_code"] == "dispatch_outcome_unknown" and not value["passed"]


@pytest.mark.parametrize("changed", ["model", "profile", "credential", "answer", "evidence", "metadata", "receipts"])
def test_semantic_or_frozen_model_change_invalidates_cached_reviews(db, monkeypatch, changed):
    journal, model = {}, Auditor()
    assert run(db, model, count=4, audit_journal=journal)["passed"]
    if changed in ("model", "profile", "credential"):
        model._runtime_config[{"model": "model_id", "profile": "profile_version", "credential": "credential_ref"}[changed]] = "changed"
    kwargs = {"messages": [], "audit_journal": journal}
    text, evidence = answer(4), deepcopy(EVIDENCE)
    if changed == "answer":
        text += "\n\n额外观测值为十三分。[^e1]"
    if changed == "evidence":
        evidence[0]["quote"] += " Changed source."
    if changed == "metadata":
        monkeypatch.setattr("app.agent.citation_scope.verified_source_metadata", lambda *args: {"e1": {"raw_sha256": "changed"}})
    if changed == "receipts":
        monkeypatch.setattr("app.agent.citation_scope.process_receipts", lambda *args: ([{"id": "changed", "content": "different"}], False))
    value = audit_answer(db, text, evidence, llm=model, **kwargs)
    assert len(model.calls) >= 2 and value["passed"] and len(journal) == 2


def test_journal_tamper_or_missing_result_fails_closed_without_a_new_request(db):
    journal, model = {}, Auditor()
    run(db, model, count=4, audit_journal=journal)
    entry = next(iter(journal.values()))["batches"]["0"]
    entry["result"]["passed"] = False
    result = run(db, model, count=4, audit_journal=journal)
    assert len(model.calls) == 1 and result["error_code"] == "journal_invalid" and result["unknown_count"] == 4


def test_failed_length_and_successful_batch_keep_actual_ledger_usage_and_resume_without_http(db, provider):
    from app import call_ledger, llm
    responses, requests = provider
    private = "SYNTHETIC_PRIVATE_PROVIDER_REASONING"
    responses += [httpx.Response(200, json={"usage": USAGE, "choices": [{"finish_reason": "length",
        "message": {"content": '{"results":[', "reasoning_content": private}}]}),
        httpx.Response(200, json={"usage": USAGE, "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"results": [verdict(index) for index in range(12, 20)]}), "reasoning_content": private}}]})]
    journal, accounted, saved = {}, {}, []
    def record(usage):
        for key, count in usage.items():
            accounted[key] = accounted.get(key, 0) + count
    value = run(db, llm.APIClient(), count=20, audit_journal=journal,
        persist=lambda: saved.append(json.loads(json.dumps(journal))), on_usage=record)
    expected = {key: count * 2 for key, count in USAGE.items()}
    assert value["new_usage"] == value["cumulative_usage"] == accounted == expected
    assert value["unknown_count"] == 12 and value["unknown_usage_batches"] == 0
    assert [batch["usage_source"] for batch in value["batches"]] == ["actual", "actual"]
    assert len(requests) == 2 and len(call_ledger.list_calls()["items"]) == 2
    assert call_ledger.summarize_calls()["actual_usage"] == expected
    assert private not in json.dumps(journal) and private not in json.dumps(value)
    restored = json.loads(json.dumps(saved[-1]))
    again = run(db, llm.APIClient(), count=20, audit_journal=restored, on_usage=record)
    assert len(requests) == 2 and again["new_usage"] == {} and accounted == expected
    assert again["cumulative_usage"] == expected and again["error_code"] == "output_incomplete"


def test_missing_provider_usage_stays_unknown_in_batch_summary(db):
    model = Auditor(lambda indices, messages: LLMResponse(json.dumps({"results": [verdict(index) for index in indices]})))
    value = run(db, model, count=4)
    assert value["passed"] and value["new_usage"] == value["cumulative_usage"] == {}
    assert value["unknown_usage_batches"] == 1 and value["batches"][0]["usage_source"] == "unknown"


def test_partially_mutating_usage_callback_cannot_reapply_same_paid_usage_in_memory(db):
    journal, accounted = {}, {}
    interrupted = [False]
    def record(usage):
        for key, value in usage.items():
            accounted[key] = accounted.get(key, 0) + value
        if not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("Synthetic callback failed after updating counters")
    model = Auditor()
    with pytest.raises(RuntimeError):
        run(db, model, count=30, audit_journal=journal, on_usage=record)
    assert len(model.calls) == 1 and accounted == USAGE
    value = run(db, model, count=30, audit_journal=journal, on_usage=record)
    assert len(model.calls) == 3 and value["passed"]
    assert accounted == value["cumulative_usage"] == {key: count * 3 for key, count in USAGE.items()}


def test_actual_paid_ledger_survives_result_checkpoint_failure_without_rebuy(db, provider):
    from app import call_ledger, llm
    responses, requests = provider
    responses.append(httpx.Response(200, json={"usage": USAGE, "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"results": [verdict(index) for index in range(4)]})}}]}))
    journal, committed = {}, []
    def persist():
        if committed:
            raise OSError("Synthetic result checkpoint unavailable")
        committed.append(json.loads(json.dumps(journal)))
    with pytest.raises(OSError):
        run(db, llm.APIClient(), count=4, audit_journal=journal, persist=persist)
    assert len(requests) == 1 and call_ledger.summarize_calls()["actual_usage"] == USAGE
    value = run(db, llm.APIClient(), count=4, audit_journal=committed[0])
    assert len(requests) == 1 and call_ledger.summarize_calls()["actual_usage"] == USAGE
    assert value["error_code"] == "dispatch_outcome_unknown" and value["unknown_usage_batches"] == 1
    assert value["cumulative_usage"] == {}, "Unattributed batch usage is unknown, not invented; durable ledger retains the actual charge"


@pytest.mark.parametrize("field,value", [
    ("run_id", "new-run"), ("attempt_id", "new-attempt"), ("resource_budget_id", "raised-budget"),
    ("resource_policy", {"seconds": 600, "revision": 2}), ("approval_mode", "full_access"),
    ("policy_revision", 9), ("quota_binding", {"revision": 3}),
])
def test_nonmodel_runtime_control_changes_do_not_rebuy_paid_reviews(db, field, value):
    model, journal = Auditor(), {}
    assert run(db, model, count=4, audit_journal=journal)["passed"]
    model._runtime_config[field] = value
    again = run(db, model, count=4, audit_journal=journal)
    assert again["passed"] and again["new_usage"] == {} and len(model.calls) == len(journal) == 1
