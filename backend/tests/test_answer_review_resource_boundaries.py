"""Actual APIClient admission/dispatch boundaries; every HTTP is synthetic."""
from copy import deepcopy

import httpx
import pytest

from app import call_ledger, request_resources, resource_budget
from app.agent.answer_review_journal import cached_review
from app.llm import APIClient
from app.memory_governance import MemoryScopeChanged
from app.runtime_context import bind_runtime_config
from tests.test_citation_review_diagnostics import provider  # noqa: F401


HTTPClient = httpx.Client
MESSAGES = [{"role": "user", "content": "Synthetic durable review"}]
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def reply():
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": '{"status":"passed"}'}}], "usage": USAGE})


def invoke(model, journal, durable, review=None, persist=None):
    return cached_review(journal, "key", review=review or (lambda: {
        "status": "passed", "usage": model.chat(MESSAGES).usage}),
        persist=persist or (lambda: durable.append(deepcopy(journal))),
        before_review=lambda: None, llm=model)


def restored_unknown(model, durable):
    saved = deepcopy(durable[-1])
    value = invoke(model, saved, durable,
        review=lambda: pytest.fail("A possibly charged review must not be purchased again"))
    assert value["error_code"] == "dispatch_outcome_unknown"


def test_actual_budget_admission_denial_is_durably_unsubmitted_then_explicit_increase_can_resume(db, provider):
    responses, requests = provider
    model, journal, durable = APIClient(synthetic=True), {}, []
    identity = resource_budget.create_budget(owner_run_id="review-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-review-estimate")
    with bind_runtime_config({"resource_budget_id": identity}):
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            invoke(model, journal, durable)
        assert journal == durable[-1] == {} and durable[0]["key"]["review_dispatch"] == "pending"
        assert not requests and not call_ledger.list_calls(include_synthetic=True)["items"]
        snapshot = resource_budget.budget_snapshot(identity)
        resource_budget.increase_budget(identity, amount_limit="1", expected_revision=snapshot["revision"])
        responses.append(reply())
        assert invoke(model, journal, durable)["status"] == "passed"
        assert invoke(model, journal, durable)["status"] == "passed"
    assert len(requests) == 1 and not responses
    assert call_ledger.summarize_calls(db=db, include_synthetic=True)["synthetic_call_count"] == 1


def test_actual_cancelled_admission_removes_only_unsubmitted_pending(db, provider):
    from app import cancel_store
    responses, requests = provider
    model, journal, durable = APIClient(synthetic=True), {}, []
    cancel_store.request_cancel("cancelled-review-owner")
    try:
        with bind_runtime_config({"run_id": "cancelled-review-owner"}):
            with pytest.raises(request_resources.ResourceRequestCancelled):
                invoke(model, journal, durable)
    finally:
        cancel_store.clear("cancelled-review-owner")
    assert journal == durable[-1] == {} and not requests
    responses.append(reply())
    assert invoke(model, journal, durable)["status"] == "passed" and len(requests) == 1


def test_actual_client_pre_request_memory_boundary_can_retry_without_unknown(provider):
    responses, requests = provider
    model, journal, durable = APIClient(synthetic=True), {}, []
    with pytest.raises(MemoryScopeChanged):
        invoke(model, journal, durable, review=lambda: (_ for _ in ()).throw(MemoryScopeChanged("Synthetic memory revision")))
    assert journal == durable[-1] == {} and not requests
    responses.append(reply())
    assert invoke(model, journal, durable)["status"] == "passed" and len(requests) == 1


def test_http_started_then_resource_cancel_stays_unknown_and_never_resends(db, provider):
    responses, requests = provider
    model, journal, durable = APIClient(synthetic=True), {}, []
    # The mock transport executes only after the actual APIClient meter and
    # admission bind. Raising here models cancellation with unknown remote cost.
    responses.append(httpx.Response(200, stream=httpx.ByteStream(b"")))
    original = HTTPClient.post
    def after_dispatch(client, *args, **kwargs):
        original(client, *args, **kwargs)
        raise request_resources.ResourceRequestCancelled("Synthetic cancellation after HTTP dispatch")
    # provider replaces the constructor, so patch the actual retained class.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(HTTPClient, "post", after_dispatch)
        with pytest.raises(request_resources.ResourceRequestCancelled):
            invoke(model, journal, durable)
    assert journal["key"] == {"review_dispatch": "pending"} and len(requests) == 1
    restored_unknown(model, durable)
    assert len(requests) == 1
    row, = call_ledger.list_calls(include_synthetic=True)["items"]
    assert row["state"] == "failed" and row["synthetic"] is True


@pytest.mark.parametrize("boundary", [MemoryScopeChanged, resource_budget.ResourceBudgetExceeded])
def test_successful_http_then_usage_callback_boundary_stays_unknown_with_unchanged_failed_call(provider, boundary):
    responses, requests = provider
    responses.append(reply())
    model, journal, durable = APIClient(synthetic=True), {}, []
    # A production client reused after a previous success already stores None.
    # Keep that exact value unchanged so only the started-attempt receipt can
    # distinguish this success from a pre-request admission rejection.
    model._call_context.failed_call = None
    previous_started = getattr(model._call_context, "last_started_call_id", None)
    def review():
        model.chat(MESSAGES)
        assert model._call_context.failed_call is None
        raise boundary("Synthetic callback after a successful charged call")
    with pytest.raises(boundary):
        invoke(model, journal, durable, review=review)
    assert model._call_context.failed_call is None and model._call_context.last_started_call_id != previous_started
    assert journal["key"] == {"review_dispatch": "pending"} and len(requests) == 1
    restored_unknown(model, durable)
    assert len(requests) == 1


def test_first_http_succeeds_then_second_admission_denied_does_not_clear_whole_review(provider):
    responses, requests = provider
    responses.append(reply())
    model, journal, durable = APIClient(synthetic=True), {}, []
    model._call_context.failed_call = None
    identity = resource_budget.create_budget(owner_run_id="multi-review-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-review-estimate")
    def review():
        model.chat(MESSAGES)
        with bind_runtime_config({"run_id": "multi-review-owner", "resource_budget_id": identity}):
            model.chat(MESSAGES)
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        invoke(model, journal, durable, review=review)
    assert len(requests) == 1 and model._call_context.failed_call is None
    assert journal["key"] == {"review_dispatch": "pending"}
    restored_unknown(model, durable)


@pytest.mark.parametrize("client_kind", ["omitted", "custom", "subclass"])
def test_no_production_client_proof_never_clears_a_resource_boundary(client_kind):
    class Custom:
        pass
    class Derived(APIClient):
        pass
    model = {"omitted": None, "custom": Custom(), "subclass": Derived(synthetic=True)}[client_kind]
    journal, durable = {}, []
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        invoke(model, journal, durable,
            review=lambda: (_ for _ in ()).throw(resource_budget.ResourceBudgetExceeded("Unproven admission")))
    assert journal["key"] == durable[-1]["key"] == {"review_dispatch": "pending"}
    restored_unknown(model, durable)


def test_nonresource_exception_with_actual_client_keeps_conservative_pending(provider):
    model, journal, durable = APIClient(synthetic=True), {}, []
    with pytest.raises(RuntimeError):
        invoke(model, journal, durable, review=lambda: (_ for _ in ()).throw(RuntimeError("Synthetic arbitrary failure")))
    assert journal["key"] == {"review_dispatch": "pending"}
    restored_unknown(model, durable)


@pytest.mark.parametrize("historical_outcome", ["success", "failure"])
def test_prior_call_context_does_not_confuse_a_current_unsubmitted_boundary(provider, historical_outcome):
    responses, requests = provider
    model, journal, durable = APIClient(synthetic=True), {}, []
    responses.append(reply() if historical_outcome == "success" else httpx.Response(400, json={"error": "Synthetic old failure"}))
    if historical_outcome == "success":
        model.chat(MESSAGES)
    else:
        with pytest.raises(httpx.HTTPError):
            model.chat(MESSAGES)
    previous_started = model._call_context.last_started_call_id
    previous_failure = model._call_context.failed_call
    identity = resource_budget.create_budget(owner_run_id="historical-review-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-review-estimate")
    with bind_runtime_config({"resource_budget_id": identity}):
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            invoke(model, journal, durable)
    assert model._call_context.last_started_call_id == previous_started
    assert model._call_context.failed_call is previous_failure
    assert journal == durable[-1] == {} and len(requests) == 1


def test_failed_no_dispatch_cleanup_save_restores_memory_pending_to_match_durable_checkpoint(provider):
    model, journal, durable = APIClient(synthetic=True), {}, []
    def save():
        if durable:
            raise OSError("Synthetic cleanup persistence failure")
        durable.append(deepcopy(journal))
    with pytest.raises(OSError):
        invoke(model, journal, durable,
            review=lambda: (_ for _ in ()).throw(MemoryScopeChanged("Synthetic pre-request memory boundary")), persist=save)
    assert journal["key"] == durable[-1]["key"] == {"review_dispatch": "pending"}
    restored_unknown(model, durable)
