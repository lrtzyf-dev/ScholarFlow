"""Post-revision goal regression and paid boundaries; no real model traffic.

Synthetic verdicts exercise schema, provenance, resource and checkpoint rules.
They do not prove that a real model recognizes a table's meaning.
"""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app import call_ledger, request_resources, resource_budget
from app.agent import rewritten_answer_quality as subject, source_attribution as source
from app.llm import APIClient, LLMResponse
from app.runtime_context import bind_runtime_config
from tests.test_citation_review_diagnostics import provider  # noqa: F401


QUESTION = "读取固定表格并报告随访人数。"
ANSWER = "当前无法确认随访人数。[^e1]"
USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
CONTEXT = {"evidence": [{"id": "e1", "source_id": "fixed-source", "revision_id": "fixed-version",
    "quote": "Follow-up participants\tCount\nCompleted follow-up\t36"}],
    "metadata": {"e1": {"table_path": "/document/body/tbl[1]"}}}


def messages():
    return [{"role": "user", "content": QUESTION},
        {"role": "assistant", "tool_calls": [{"id": "fixed-read", "function": {
            "name": "read_attached_source", "arguments": '{"source_id":"fixed-source"}'}}]},
        {"role": "tool", "tool_call_id": "fixed-read", "content": json.dumps({
            "status": "ready", "source_id": "fixed-source", "revision_id": "fixed-version",
            "text": CONTEXT["evidence"][0]["quote"]})}]


def issue(*, confirmed=True, text=ANSWER, user_constraint=None):
    return {"kind": "unresolved_request", "text": text, "related_text": None,
        "user_constraint": user_constraint or {"receipt_id": "question", "quote": "报告随访人数"},
        "reason": "同版本表格已提供随访参与者和36，最终仍未给出所问人数。", "confirmed": confirmed}


class Model:
    mode = "api"

    def __init__(self, issues=()):
        self.calls = []
        self.data = {"quality_issues": list(issues)}
        self.raw = None
        self.tools = []
        self.error = None
        self._runtime_config = {"mode": "api", "model_id": "Synthetic-Quality-Model"}

    def chat(self, messages, **options):
        self.calls.append((deepcopy(messages), deepcopy(options)))
        if self.error:
            raise self.error
        return LLMResponse(self.raw if self.raw is not None else json.dumps(self.data, ensure_ascii=False),
            tool_calls=self.tools, usage=USAGE)


def invoke(model, journal=None, *, saved=None, **changes):
    journal = {} if journal is None else journal
    saved = [] if saved is None else saved
    args = {"journal": journal, "persist": lambda: saved.append(deepcopy(journal)),
        "before_review": lambda: None, "evidence_context": deepcopy(CONTEXT)}
    args.update(changes)
    answer = args.pop("answer", ANSWER)
    history = args.pop("messages", messages())
    return subject.review_rewritten_quality(QUESTION, answer, history, model, **args)


def test_source_reviewer_current_policy_and_contract_are_explicitly_bound():
    # D04's prior literal hashes remain in its preserved failing report and
    # frozen source. D06 deliberately changes shared quality policy; that must
    # invalidate known policy results while unknown user-family intents fence
    # dispatch independently of these exact contract digests.
    assert hashlib.sha256(source.review_instructions().encode()).hexdigest() == \
        "ef42d1d93efd532c5efdea34b2469a61514474de48bc0bf9071e43489e7c43db"
    assert source.review_contract_id() == "bb0a8a08b8cb45a83a4ee0b97a64133fc5eb7a99fcf48508a69c63c302390c78"
    assert source.quality_instructions() in subject.quality_only_instructions()


def test_one_quality_call_sees_actual_sources_and_uses_quality_only_protocol():
    model = Model([issue()])
    result = invoke(model)
    assert len(model.calls) == 1
    request, options = model.calls[0]
    payload = json.loads(request[1]["content"])
    assert payload["question"] == QUESTION and payload["answer"] == ANSWER
    assert payload["evidence_context"] == CONTEXT
    tool, = (r for r in payload["context"] if r["role"] == "tool")
    assert tool["tool_name"] == "read_attached_source" and "Completed follow-up" in tool["content"]
    assert "results" not in payload and "blocks" not in payload and "tools" not in options
    assert result["quality_status"] == "failed" and result["quality_verified"]
    assert result["blocks_completion"] and result["known_failed_count"] == 1
    assert result["unresolved"][0]["answer_spans"] == [{"start": 0, "end": len(ANSWER)}]


@pytest.mark.parametrize("confirmed,status,blocked", [(True, "failed", True), (None, "unverified", True), (False, "passed", False)])
def test_four_state_quality_never_promotes_uncertain_issue_to_confirmed(confirmed, status, blocked):
    result = invoke(Model([issue(confirmed=confirmed)]))
    assert result["quality_status"] == status and result["blocks_completion"] is blocked
    assert bool(result["unresolved"]) is (confirmed is True)


def test_not_assessed_has_no_invented_problem_or_paid_intent_then_budget_can_resume():
    journal, model = {}, Model()
    result = invoke(model, journal, allow_dispatch=False)
    assert result["quality_status"] == "not_assessed" and result["quality_reason"] == "run_budget_exhausted"
    assert result["blocks_completion"] and not result["quality_issues"] and not result["unresolved"]
    assert not model.calls and journal == {}
    assert invoke(model, journal)["quality_status"] == "passed" and len(model.calls) == 1


@pytest.mark.parametrize("mode", ["offline", "limit"])
def test_unavailable_review_is_explicitly_not_assessed(mode, monkeypatch):
    model, journal = Model(), {}
    if mode == "offline":
        model.mode = "mock"
        model._runtime_config["mode"] = "mock"
    else:
        monkeypatch.setattr(subject, "MAX_ANSWER_CHARS", 1)
    result = invoke(model, journal)
    assert result["quality_status"] == "not_assessed" and result["blocks_completion"]
    assert not result["quality_issues"] and not model.calls and not journal


def test_known_failures_survive_unknown_and_are_distinct_from_current_observations():
    known = issue()
    model = Model([issue(confirmed=None)])
    result = invoke(model, known_issues=[known])
    assert result["quality_status"] == "unverified" and result["quality_issues"][0]["confirmed"] is None
    assert result["unresolved"] == [known] and result["known_failed_count"] == 1
    passed = invoke(Model(), known_issues=[known])
    assert passed["quality_status"] == "passed" and not passed["unresolved"] and not passed["blocks_completion"]


def test_no_budget_keeps_confirmed_failure_without_fabricating_current_issue():
    result = invoke(Model(), known_issues=[issue()], allow_dispatch=False)
    assert result["quality_status"] == "not_assessed" and not result["quality_issues"]
    assert result["unresolved"] == [issue()] and result["blocks_completion"]


@pytest.mark.parametrize("bad", ["missing", "old_source_protocol", "duplicate", "nan", "not_list", "extra", "tools", "not_exact", "fake_user", "source_as_user"])
def test_invalid_quality_or_forged_user_provenance_cannot_pass(bad):
    model = Model()
    if bad == "missing":
        model.data = {}
    elif bad == "old_source_protocol":
        model.data = {"source_required": False, "results": [], "quality_issues": []}
    elif bad == "duplicate":
        model.raw = '{"quality_issues":[],"quality_issues":[]}'
    elif bad == "nan":
        model.raw = '{"quality_issues":NaN}'
    elif bad == "not_list":
        model.data = {"quality_issues": None}
    elif bad == "extra":
        model.data = {"quality_issues": [], "passed": True}
    elif bad == "tools":
        model.tools = [{"id": "injected-operation"}]
    elif bad == "not_exact":
        model.data = {"quality_issues": [issue(text="不存在的原句")]}
    elif bad == "fake_user":
        model.data = {"quality_issues": [issue(user_constraint={"receipt_id": "question", "quote": "虚构限制"})]}
    else:
        model.data = {"quality_issues": [issue(user_constraint={"receipt_id": "context:2", "quote": "Completed follow-up"})]}
    result = invoke(model, known_issues=[issue()])
    assert result["quality_status"] == "unverified" and result["blocks_completion"]
    assert result["unresolved"] == [issue()] and len(model.calls) == 1


def test_partial_invalid_schema_keeps_located_confirmed_issue_and_never_certifies_complete_review():
    model = Model([issue(), {"bad": True}])
    result = invoke(model)
    assert result["quality_status"] == "failed" and not result["quality_verified"]
    assert result["known_failed_count"] == 1 and result["blocks_completion"]


def test_truncated_actual_context_cannot_retire_a_previously_confirmed_problem(monkeypatch):
    monkeypatch.setattr(source, "MAX_CONTEXT_CHARS", 100)
    result = invoke(Model(), known_issues=[issue()])
    assert result["quality_status"] == "unverified" and not result["quality_verified"]
    assert result["quality_reason"] == "context_truncated" and result["unresolved"] == [issue()]


def test_exact_result_reused_without_budget_and_known_issue_updates_do_not_rebuy():
    model, journal, usage = Model(), {}, []
    first = invoke(model, journal, on_usage=usage.append)
    second = invoke(model, journal, known_issues=[issue()], allow_dispatch=False, on_usage=usage.append)
    assert first["quality_status"] == second["quality_status"] == "passed"
    assert not second["unresolved"] and len(model.calls) == 1 and usage == [USAGE]


def test_known_old_policy_pass_is_rechecked_once_but_family_and_repair_allowance_stay_stable(monkeypatch):
    model, journal = Model(), {}
    family = subject.review_family_key(QUESTION, messages())
    allowance = source.quality_repair_context_key(QUESTION, messages())
    invoke(model, journal)
    policy = subject.quality_only_instructions()
    monkeypatch.setattr(subject, "quality_only_instructions", lambda: policy + " New bounded synthetic policy.")
    invoke(model, journal)
    invoke(model, journal)
    assert len(model.calls) == 2
    assert subject.review_family_key(QUESTION, messages()) == family
    assert source.quality_repair_context_key(QUESTION, messages()) == allowance


@pytest.mark.parametrize("change", ["policy", "answer", "model", "sources"])
@pytest.mark.parametrize("old_state", ["pending", "dispatch_unknown"])
def test_unknown_family_is_not_rebought_after_any_result_identity_change(monkeypatch, change, old_state):
    model = Model()
    family = subject.review_family_key(QUESTION, messages())
    journal = {"source_review/" + family + "/" + "a" * 64:
        {"review_dispatch": "pending"} if old_state == "pending" else {"error_code": "dispatch_outcome_unknown"}}
    changes = {}
    if change == "policy":
        monkeypatch.setattr(subject, "PROTOCOL", 999)
    elif change == "answer":
        changes["answer"] = "一个不同的最终答案。[^e1]"
    elif change == "model":
        model._runtime_config["model_id"] = "Changed-Synthetic-Model"
    else:
        changes["evidence_context"] = {"evidence": [{"revision_id": "changed-fixed-version"}]}
    result = invoke(model, journal, known_issues=[issue()], **changes)
    assert result["error_code"] == "dispatch_outcome_unknown" and result["blocks_completion"]
    assert result["quality_status"] == "unverified" and result["unresolved"] == [issue()]
    assert not model.calls


def test_only_a_new_actual_user_revision_changes_the_stable_family():
    assert subject.review_family_key(QUESTION, messages(), input_ids=["a", "b"]) == \
        subject.review_family_key(QUESTION, messages(), input_ids=["b", "a", "a"])
    assert subject.review_family_key(QUESTION, messages(), input_ids=["a"]) != \
        subject.review_family_key(QUESTION, messages(), input_ids=["a", "new-steering"])


def test_transport_error_keeps_pending_and_resume_changed_answer_never_rebuys():
    model, journal, saved = Model(), {}, []
    model.error = httpx.ReadTimeout("SYNTHETIC-PRIVATE-TRANSPORT-BODY")
    first = invoke(model, journal, saved=saved)
    assert first["error_code"] == "dispatch_outcome_unknown" and len(model.calls) == 1
    assert next(iter(journal.values())) == {"review_dispatch": "pending"}
    assert "SYNTHETIC-PRIVATE" not in json.dumps(first)
    model.error = None
    second = invoke(model, journal, answer="来源提供36人。[^e1]")
    assert second["error_code"] == "dispatch_outcome_unknown" and len(model.calls) == 1


@pytest.mark.parametrize("failed_save", [1, 2])
def test_persistence_failure_preserves_last_durable_state_and_paid_intent(failed_save):
    model, journal, durable, saves = Model(), {}, [], []
    def persist():
        saves.append(1)
        if len(saves) == failed_save:
            raise OSError("Synthetic quality checkpoint failure")
        durable.append(deepcopy(journal))
    with pytest.raises(OSError):
        invoke(model, journal, persist=persist)
    assert journal == (durable[-1] if durable else {})
    if failed_save == 1:
        assert not model.calls and not journal
    else:
        assert len(model.calls) == 1 and next(iter(journal.values())) == {"review_dispatch": "pending"}
        assert invoke(model, journal)["error_code"] == "dispatch_outcome_unknown" and len(model.calls) == 1


def test_unknown_conversion_save_failure_keeps_pending():
    family = subject.review_family_key(QUESTION, messages())
    journal = {"source_review/" + family + "/" + "b" * 64: {"review_dispatch": "pending"}}
    before = deepcopy(journal)
    def persist():
        raise OSError("Synthetic unknown checkpoint failure")
    with pytest.raises(OSError):
        invoke(Model(), journal, persist=persist)
    assert journal == before


def test_result_and_usage_saved_before_cancellation_then_reused_once():
    model, journal, durable, usage = Model(), {}, [], []
    class Cancelled(Exception):
        pass
    def check():
        if any(value.get("quality_status") == "passed" for value in journal.values()):
            raise Cancelled()
    with pytest.raises(Cancelled):
        invoke(model, journal, saved=durable, before_review=check, on_usage=usage.append)
    assert len(model.calls) == 1 and usage == [USAGE] and durable[-1] == journal
    assert invoke(model, journal, on_usage=usage.append)["quality_status"] == "passed"
    assert len(model.calls) == 1 and usage == [USAGE]


def reply():
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": '{"quality_issues":[]}'}}], "usage": USAGE})


def test_exact_api_client_budget_denial_does_not_spend_dispatch_family(db, provider):
    responses, requests = provider
    model, journal, durable = APIClient(synthetic=True), {}, []
    identity = resource_budget.create_budget(owner_run_id="rewrite-quality-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-quality-review")
    with bind_runtime_config({"resource_budget_id": identity, "mode": "api",
            "base_url": "https://diagnostic.example.test/v1", "model_id": "Exact-Diagnostic-Model"}):
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            invoke(model, journal, saved=durable)
        assert not requests and journal == durable[-1] == {}
        snapshot = resource_budget.budget_snapshot(identity)
        resource_budget.increase_budget(identity, amount_limit="1", expected_revision=snapshot["revision"])
        responses.append(reply())
        assert invoke(model, journal)["quality_status"] == "passed"
        assert invoke(model, journal)["quality_status"] == "passed"
    assert len(requests) == 1 and call_ledger.summarize_calls(db=db, include_synthetic=True)["synthetic_call_count"] == 1


def test_incomplete_model_binding_is_not_assessed_and_cannot_claim_a_paid_intent(provider):
    _, requests = provider
    model, journal = APIClient(synthetic=True), {}
    with bind_runtime_config({"resource_budget_id": "incomplete-synthetic-config"}):
        result = invoke(model, journal)
    assert result["quality_status"] == "not_assessed" and result["error_code"] == "quality_input_invalid"
    assert result["blocks_completion"] and not journal and not requests


def test_actual_http_success_then_usage_boundary_keeps_unknown_never_rebuys(provider):
    responses, requests = provider
    responses.append(reply())
    model, journal = APIClient(synthetic=True), {}
    def usage(_):
        raise request_resources.ResourceRequestCancelled("Synthetic cancellation after paid response")
    with pytest.raises(request_resources.ResourceRequestCancelled):
        invoke(model, journal, on_usage=usage)
    assert len(requests) == 1 and next(iter(journal.values())) == {"review_dispatch": "pending"}
    assert invoke(model, journal)["error_code"] == "dispatch_outcome_unknown" and len(requests) == 1
