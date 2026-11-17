"""Checkpoint/charge mechanics only; every real APIClient HTTP is synthetic."""
from copy import deepcopy
import json

import httpx
import pytest

from app.agent.checkpoint import AgentState
from app.agent.citation_answer_rewrite import apply_citation_answer_rewrite
from app.llm import APIClient, LLMResponse
from tests.test_citation_review_diagnostics import provider  # noqa: F401


PRIVATE = "SYNTHETIC_PRIVATE_PROVIDER_REPLAY"
USAGE = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
REQUEST = [{"role": "system", "content": "Rewrite with exact current evidence"},
    {"role": "user", "content": "TRANSFORMED actual request and feedback"}]
INPUT = {"messages": [{"role": "user", "content": "Original task"}], "applied_input_ids": ["input-1"], "memory_fingerprint": "fixed-memory"}


class Cancelled(RuntimeError):
    pass


class PowerLoss(BaseException):
    pass


class Model:
    mode = "api"
    def __init__(self):
        self.calls = 0
        self._runtime_config = {"mode": "api", "base_url": "https://rewrite.example.test/v1", "model_id": "Exact-Rewrite-Model"}
    def reply(self):
        self.calls += 1
        return LLMResponse("A revised sourced answer.[^e1]", usage=USAGE, reasoning_content=PRIVATE,
            provider_payload={"assistant": {"opaque": PRIVATE}})


def state():
    return AgentState(question="Original task", phase="finalize", answer="Original answer lacks a reference.",
        evidence=[{"id": "e1", "quote": "A fixed source value."}], messages=deepcopy(INPUT["messages"]))


def records(value):
    return {key: item for key, item in value.source_attribution_reviews.items() if key.startswith("citation_rewrite/")}


def invoke(value, model, durable, **options):
    return apply_citation_answer_rewrite(value, llm=model, request_messages=options.pop("request_messages", deepcopy(REQUEST)),
        input_identity=options.pop("input_identity", deepcopy(INPUT)), call=options.pop("call", model.reply if isinstance(model, Model) else lambda: model.chat(REQUEST)),
        persist=options.pop("persist", lambda: durable.append(deepcopy(value.snapshot()))),
        before_review=options.pop("before_review", lambda: None), **options)


def test_durable_response_precedes_adoption_and_private_context_never_enters_public_result():
    value, model, durable = state(), Model(), []
    def call():
        assert next(iter(durable[-1]["source_attribution_reviews"].values()))
        assert any(item.get("review_dispatch") == "pending" for item in durable[-1]["source_attribution_reviews"].values())
        return model.reply()
    result = invoke(value, model, durable, call=call)
    assert result["changed"] and result["semantic_verification"] == "not_performed" and "passed" not in result
    assert PRIVATE not in json.dumps(result) and PRIVATE in json.dumps(value.provider_context)
    assert value.answer == "A revised sourced answer.[^e1]" and value.usage == USAGE and model.calls == 1
    ready = [snapshot for snapshot in durable if any(item.get("status") == "ready" for item in snapshot["source_attribution_reviews"].values())]
    assert ready[0]["answer"] == "Original answer lacks a reference." and ready[0]["usage"]["total_tokens"] == 0
    assert durable[-1]["answer"] == value.answer and durable[-1]["usage"] == USAGE
    assert next(iter(records(value).values()))["answer_applied"] is True
    again = invoke(value, model, durable, call=lambda: pytest.fail("Already adopted revision was repurchased"))
    assert again["status"] == "already_applied" and not again["changed"] and value.usage == USAGE


def test_known_private_response_survives_cancellation_and_restart_then_usage_applies_once():
    value, model, durable = state(), Model(), []
    def check():
        if any(item.get("status") == "ready" for item in records(value).values()):
            raise Cancelled("Synthetic cancellation after durable response")
    with pytest.raises(Cancelled):
        invoke(value, model, durable, before_review=check)
    assert model.calls == 1 and value.usage["total_tokens"] == 0 and PRIVATE not in value.answer
    restored = AgentState.model_validate(durable[-1])
    result = invoke(restored, model, durable, call=lambda: pytest.fail("Saved result was repurchased"))
    assert result["changed"] and restored.usage == USAGE and PRIVATE in json.dumps(restored.provider_context)
    invoke(restored, model, durable, call=lambda: pytest.fail("Saved result was repurchased twice"))
    assert restored.usage == USAGE


def test_cancellation_after_atomic_adoption_keeps_answer_and_usage_once():
    value, model, durable = state(), Model(), []
    def check():
        if value.answer == "A revised sourced answer.[^e1]":
            raise Cancelled("Synthetic cancellation after adoption checkpoint")
    with pytest.raises(Cancelled):
        invoke(value, model, durable, before_review=check)
    assert durable[-1]["answer"] == value.answer and durable[-1]["usage"] == USAGE
    restored = AgentState.model_validate(durable[-1])
    assert invoke(restored, model, durable, call=lambda: pytest.fail("Post-adoption cancellation repurchased"))["status"] == "already_applied"
    assert restored.usage == USAGE and model.calls == 1


@pytest.mark.parametrize("changed_request", [False, True])
def test_uncertain_dispatch_never_rebuys_even_if_regenerated_feedback_changes(changed_request):
    value, model, durable = state(), Model(), []
    def lost():
        model.calls += 1
        raise PowerLoss()
    with pytest.raises(PowerLoss):
        invoke(value, model, durable, call=lost)
    restored = AgentState.model_validate(durable[-1])
    request = deepcopy(REQUEST)
    if changed_request:
        request[-1]["content"] = "A newly worded audit reason must not buy another revision"
    result = invoke(restored, model, durable, request_messages=request,
        call=lambda: pytest.fail("Unknown dispatch repurchased"))
    assert result["error_code"] == "dispatch_outcome_unknown" and not result["changed"]
    assert model.calls == 1 and restored.answer == value.answer


@pytest.mark.parametrize("failed_save", [1, 2, 3, 4])
def test_each_failed_save_restores_memory_to_last_durable_state_without_repurchase(failed_save):
    value, model, durable, saves = state(), Model(), [], []
    original = value.snapshot()
    def persist():
        saves.append(1)
        if len(saves) == failed_save:
            value.elapsed_ms += 17  # Checkpointer.save can update this first.
            raise OSError("Synthetic rewrite checkpoint failure")
        durable.append(deepcopy(value.snapshot()))
    with pytest.raises(OSError):
        invoke(value, model, durable, persist=persist)
    assert value.snapshot() == (durable[-1] if durable else original)
    if failed_save <= 2:
        assert model.calls == 0
        assert invoke(value, model, durable)["changed"] and model.calls == 1
    elif failed_save == 3:
        assert model.calls == 1 and next(iter(records(value).values())) == {"review_dispatch": "pending"}
        result = invoke(value, model, durable, call=lambda: pytest.fail("Lost result was repurchased"))
        assert result["error_code"] == "dispatch_outcome_unknown"
    else:
        assert model.calls == 1 and next(iter(records(value).values()))["status"] == "ready"
        assert invoke(value, model, durable, call=lambda: pytest.fail("Known response was repurchased"))["changed"]
        assert value.usage == USAGE


def test_usage_callback_mutation_then_failure_rolls_back_and_can_resume_saved_response():
    value, model, durable = state(), Model(), []
    def usage(counts):
        value.usage["total_tokens"] += counts["total_tokens"]
        raise RuntimeError("Synthetic local callback interruption")
    with pytest.raises(RuntimeError):
        invoke(value, model, durable, on_usage=usage)
    assert value.snapshot() == durable[-1] and value.usage["total_tokens"] == 0 and model.calls == 1
    invoke(value, model, durable, call=lambda: pytest.fail("Callback failure repurchased response"))
    assert value.usage == USAGE and model.calls == 1


def test_actual_transformed_request_is_bound_to_full_key_but_input_change_creates_new_context():
    first, second, model = state(), state(), Model()
    invoke(first, model, [])
    request = deepcopy(REQUEST); request[-1]["content"] = "A different actual transformed request"
    invoke(second, model, [], request_messages=request)
    assert set(records(first)) != set(records(second))
    value, model, durable = state(), Model(), []
    invoke(value, model, durable)
    model._runtime_config.update(run_id="new-attempt", resource_budget_id="larger-budget", policy_revision=4)
    assert invoke(value, model, durable, call=lambda: pytest.fail("Run controls caused repurchase"))["status"] == "already_applied"
    new_input = deepcopy(INPUT); new_input["applied_input_ids"].append("new-user-correction")
    assert invoke(value, model, durable, input_identity=new_input)["status"] == "already_applied"
    assert model.calls == 2, "A new actual input context can receive its own single revision"
    assert value.usage == {key: count * 2 for key, count in USAGE.items()}


def test_cache_corruption_is_not_adopted_or_rebought():
    value, model, durable = state(), Model(), []
    def stop():
        if any(item.get("status") == "ready" for item in records(value).values()):
            raise Cancelled()
    with pytest.raises(Cancelled):
        invoke(value, model, durable, before_review=stop)
    next(iter(records(value).values()))["answer"] = "Corrupted cached scientific conclusion"
    result = invoke(value, model, durable, call=lambda: pytest.fail("Corrupt receipt caused another purchase"))
    assert result["error_code"] == "rewrite_receipt_invalid" and not result["changed"]
    assert value.answer == "Original answer lacks a reference." and value.usage["total_tokens"] == 0


def test_saved_rewrite_cannot_overwrite_a_different_current_answer_in_same_input_context():
    value, model, durable = state(), Model(), []
    def stop():
        if any(item.get("status") == "ready" for item in records(value).values()):
            raise Cancelled()
    with pytest.raises(Cancelled):
        invoke(value, model, durable, before_review=stop)
    value.answer = "A separately changed current answer."
    result = invoke(value, model, durable, call=lambda: pytest.fail("Changed current answer repurchased"))
    assert result["error_code"] == "rewrite_context_answer_changed" and not result["changed"]
    assert value.answer == "A separately changed current answer." and value.usage["total_tokens"] == 0


def test_invalid_complete_response_is_accounted_once_but_never_semantically_passed_or_applied():
    value, model, durable = state(), Model(), []
    def tools():
        model.calls += 1
        return LLMResponse("", tool_calls=[{"id": "tool", "name": "must-not-execute", "arguments": {}}], usage=USAGE)
    result = invoke(value, model, durable, call=tools)
    assert result["error_code"] == "rewrite_response_invalid" and not result["changed"]
    assert value.answer == "Original answer lacks a reference." and value.usage == USAGE
    invoke(value, model, durable, call=lambda: pytest.fail("Invalid paid response was repurchased"))
    assert model.calls == 1 and value.usage == USAGE


@pytest.mark.parametrize("failure", ["http503", "http429", "timeout"])
def test_real_api_client_failure_is_one_attempt_and_recovery_does_not_retry_or_publish_private_error(db, provider, failure):
    from app.agent.loop import _chat_with_retry
    responses, requests = provider
    class TimeoutStream(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout(PRIVATE)
            yield b""  # pragma: no cover -- mark this as a streaming iterator
    failed_response = (httpx.Response(200, stream=TimeoutStream()) if failure == "timeout"
        else httpx.Response(429 if failure == "http429" else 503, json={"error": PRIVATE}))
    responses.extend([failed_response, httpx.Response(200, json={})])
    value, model, durable = state(), APIClient(synthetic=True), []
    call = lambda: _chat_with_retry(model, REQUEST, tools=None, attempts=1)
    result = invoke(value, model, durable, call=call)
    assert result["error_code"] == "dispatch_outcome_unknown" and len(requests) == 1 and len(responses) == 1
    assert PRIVATE not in json.dumps(result) and PRIVATE not in json.dumps(value.snapshot())
    invoke(value, model, durable, call=lambda: pytest.fail("Transient failure caused automatic paid retry"))
    assert len(requests) == 1


def test_real_admission_denial_can_resume_after_explicit_budget_increase(db, provider):
    from app import resource_budget
    from app.runtime_context import bind_runtime_config
    responses, requests = provider
    value, model, durable = state(), APIClient(synthetic=True), []
    identity = resource_budget.create_budget(owner_run_id="rewrite-budget-owner", amount_limit=".1",
        request_estimate=".2", price_version="synthetic-rewrite-estimate")
    binding = {"resource_budget_id": identity, "mode": "api", "base_url": "https://diagnostic.example.test/v1", "model_id": "Exact-Diagnostic-Model"}
    with bind_runtime_config(binding):
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            invoke(value, model, durable)
        assert not requests and not records(value)
        snapshot = resource_budget.budget_snapshot(identity)
        resource_budget.increase_budget(identity, amount_limit="1", expected_revision=snapshot["revision"])
        responses.append(httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "Revised answer.[^e1]", "reasoning_content": PRIVATE}}], "usage": USAGE}))
        result = invoke(value, model, durable)
    assert result["changed"] and len(requests) == 1 and value.usage == USAGE
    assert PRIVATE in json.dumps(value.provider_context) and PRIVATE not in json.dumps(result)


def test_real_api_complete_private_response_is_saved_before_cancellation_and_restored_without_http(db, provider):
    responses, requests = provider
    responses.append(httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": "A known private response.[^e1]", "reasoning_content": PRIVATE}}], "usage": USAGE}))
    value, model, durable = state(), APIClient(synthetic=True), []
    def check():
        if any(item.get("status") == "ready" for item in records(value).values()):
            raise Cancelled("Synthetic cancellation after real APIClient response checkpoint")
    with pytest.raises(Cancelled):
        invoke(value, model, durable, before_review=check)
    assert len(requests) == 1 and value.usage["total_tokens"] == 0
    restored = AgentState.model_validate(durable[-1])
    result = invoke(restored, model, durable, call=lambda: pytest.fail("Known private response rebought"))
    assert result["changed"] and restored.usage == USAGE and len(requests) == 1
    assert PRIVATE in json.dumps(restored.provider_context) and PRIVATE not in json.dumps(result)
