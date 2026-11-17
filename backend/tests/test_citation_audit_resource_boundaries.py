"""Production batched review boundaries; all provider traffic is MockTransport."""
from copy import deepcopy
import json

import httpx
import pytest

from app import call_ledger, resource_budget
from app.agent import audit
from app.llm import APIClient
from app.memory_governance import MemoryScopeChanged
from app.request_resources import ResourceRequestCancelled
from app.runtime_context import bind_runtime_config
from tests.test_citation_audit_batches import USAGE, run, verdict
from tests.test_citation_review_diagnostics import provider  # noqa: F401


HTTPClient = httpx.Client


def reply():
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"results": [verdict(i) for i in range(4)]})}}], "usage": USAGE})


def binding(identity):
    return {"resource_budget_id": identity, "mode": "api",
        "base_url": "https://diagnostic.example.test/v1", "model_id": "Exact-Diagnostic-Model"}


def batches(journal):
    return next(iter(journal.values()))["batches"]


def assert_unknown_without_repurchase(db, model, durable, requests):
    before = len(requests)
    restored = deepcopy(durable[-1])
    value = run(db, model, count=4, audit_journal=restored)
    assert len(requests) == before and value["error_code"] == "dispatch_outcome_unknown"
    assert value["unknown_count"] == 4 and value["known_failed_count"] == 0
    assert not value["passed"] and not value["verified"]


def test_real_batch_admission_denial_is_saved_unsubmitted_and_increase_resumes_exactly_once(db, provider):
    responses, requests = provider
    model, journal, durable, usages = APIClient(synthetic=True), {}, [], []
    identity = resource_budget.create_budget(owner_run_id="batch-admission-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-batch-estimate")
    with bind_runtime_config(binding(identity)):
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            run(db, model, count=4, audit_journal=journal,
                persist=lambda: durable.append(deepcopy(journal)), on_usage=usages.append)
        assert batches(durable[0])["0"]["state"] == "pending"
        assert batches(journal) == batches(durable[-1]) == {} and not usages and not requests
        assert not call_ledger.list_calls(include_synthetic=True)["items"]
        snapshot = resource_budget.budget_snapshot(identity)
        resource_budget.increase_budget(identity, amount_limit="1", expected_revision=snapshot["revision"])
        responses.append(reply())
        value = run(db, model, count=4, audit_journal=journal,
            persist=lambda: durable.append(deepcopy(journal)), on_usage=usages.append)
        assert value["passed"] and value["new_usage"] == USAGE and value["unknown_count"] == 0
        replay = run(db, model, count=4, audit_journal=journal, on_usage=usages.append)
    assert len(requests) == 1 and not responses and usages == [USAGE]
    assert replay["passed"] and replay["new_usage"] == {} and replay["cumulative_usage"] == USAGE
    assert call_ledger.summarize_calls(db=db, include_synthetic=True)["synthetic_call_count"] == 1


@pytest.mark.parametrize("boundary", [MemoryScopeChanged, resource_budget.ResourceBudgetExceeded])
def test_successful_paid_batch_followed_by_resource_boundary_keeps_pending_despite_same_failed_call(db, provider, monkeypatch, boundary):
    responses, requests = provider
    responses.append(reply())
    model, journal, durable = APIClient(synthetic=True), {}, []
    model._call_context.failed_call = None
    original = audit._audit_batch
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        assert model._call_context.failed_call is None
        raise boundary("Synthetic resource boundary after a successful paid review")
    with monkeypatch.context() as patch:
        patch.setattr(audit, "_audit_batch", interrupted)
        with pytest.raises(boundary):
            run(db, model, count=4, audit_journal=journal, persist=lambda: durable.append(deepcopy(journal)))
    assert len(requests) == 1 and batches(journal)["0"]["state"] == "pending"
    assert_unknown_without_repurchase(db, model, durable, requests)


def test_successful_first_call_then_second_admission_denial_keeps_entire_batch_pending(db, provider, monkeypatch):
    responses, requests = provider
    responses.append(reply())
    model, journal, durable = APIClient(synthetic=True), {}, []
    model._call_context.failed_call = None
    original = audit._audit_batch
    identity = resource_budget.create_budget(owner_run_id="batch-second-admission-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-batch-estimate")
    def twice(*args, **kwargs):
        original(*args, **kwargs)
        with bind_runtime_config(binding(identity)):
            original(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(audit, "_audit_batch", twice)
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            run(db, model, count=4, audit_journal=journal, persist=lambda: durable.append(deepcopy(journal)))
    assert model._call_context.failed_call is None and len(requests) == 1
    assert batches(journal)["0"]["state"] == "pending"
    assert_unknown_without_repurchase(db, model, durable, requests)


def test_actual_http_then_cancellation_keeps_unknown_without_rebuy(db, provider, monkeypatch):
    responses, requests = provider
    responses.append(reply())
    model, journal, durable = APIClient(synthetic=True), {}, []
    original = HTTPClient.post
    def after_dispatch(client, *args, **kwargs):
        original(client, *args, **kwargs)
        raise ResourceRequestCancelled("Synthetic cancellation after HTTP dispatch")
    with monkeypatch.context() as patch:
        patch.setattr(HTTPClient, "post", after_dispatch)
        with pytest.raises(ResourceRequestCancelled):
            run(db, model, count=4, audit_journal=journal, persist=lambda: durable.append(deepcopy(journal)))
    assert len(requests) == 1 and batches(journal)["0"]["state"] == "pending"
    assert_unknown_without_repurchase(db, model, durable, requests)


def test_failed_unsubmitted_cleanup_checkpoint_restores_pending_to_match_disk(db, provider):
    model, journal, durable = APIClient(synthetic=True), {}, []
    identity = resource_budget.create_budget(owner_run_id="batch-cleanup-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-batch-estimate")
    def persist():
        if durable:
            raise OSError("Synthetic cleanup checkpoint unavailable")
        durable.append(deepcopy(journal))
    with bind_runtime_config(binding(identity)):
        with pytest.raises(OSError):
            run(db, model, count=4, audit_journal=journal, persist=persist)
        assert batches(journal)["0"] == batches(durable[-1])["0"]
        value = run(db, model, count=4, audit_journal=deepcopy(durable[-1]))
    assert value["unknown_count"] == 4 and value["error_code"] == "dispatch_outcome_unknown"
    assert not provider[1]
