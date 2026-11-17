import asyncio
import json

import pytest
from sqlalchemy import select

from app import call_ledger as ledger
from app.db import SessionLocal
from app.ledger_models import CallRecord, CallUsageReceipt


def begin(**kwargs):
    return ledger.begin_call(provider="test-provider", service="chat", endpoint="https://example.test/v1/chat",
                              **kwargs)


def test_admission_is_durable_before_io_and_same_call_id_is_idempotent(db):
    call_id = begin(call_id="stable-call", run_id="run", attempt_id="attempt")
    assert db.get(CallRecord, call_id).state == "started"
    first = ledger.get_call(call_id)
    assert begin(call_id=call_id, run_id="run", attempt_id="attempt") == call_id
    assert ledger.get_call(call_id)["request_id"] == first["request_id"]
    assert ledger.get_call(call_id)["revision"] == first["revision"]
    with pytest.raises(ledger.CallLedgerConflict):
        begin(call_id=call_id, run_id="other-run", attempt_id="attempt")


def test_retry_is_a_distinct_call_with_one_logical_request_and_scope_validation():
    original = begin(run_id="run", attempt_id="attempt")
    retry = begin(run_id="run", attempt_id="attempt", retry_of_call_id=original)
    again = begin(run_id="run", attempt_id="attempt", retry_of_call_id=retry)
    assert len({original, retry, again}) == 3
    snapshots = [ledger.get_call(i) for i in (original, retry, again)]
    assert len({s["request_id"] for s in snapshots}) == 1
    assert [s["retry_index"] for s in snapshots] == [0, 1, 2]
    for changes in ({"run_id": "other", "attempt_id": "attempt"},
                    {"run_id": "run", "attempt_id": "other"},
                    {"run_id": "run", "attempt_id": "attempt", "synthetic": True},
                    {"run_id": "run", "attempt_id": "attempt", "request_id": "different"}):
        with pytest.raises(ledger.CallLedgerError):
            begin(retry_of_call_id=original, **changes)


def test_parent_call_belongs_to_same_run_attempt_and_cannot_be_self():
    parent = begin(run_id="run", attempt_id="attempt")
    child = begin(run_id="run", attempt_id="attempt", parent_call_id=parent)
    assert ledger.get_call(child)["parent_call_id"] == parent
    with pytest.raises(ledger.CallLedgerError):
        begin(run_id="other", attempt_id="attempt", parent_call_id=parent)
    for key in ("parent_call_id", "retry_of_call_id"):
        with pytest.raises(ledger.CallLedgerError):
            begin(call_id="self-call", **{key: "self-call"})


def test_cumulative_stream_and_final_duplicate_are_not_added_twice(db):
    call_id = begin()
    ledger.record_usage(call_id, {"prompt_tokens": 10, "completion_tokens": 2})
    ledger.record_usage(call_id, {"prompt_tokens": 10, "completion_tokens": 5})
    first = ledger.finish_call(call_id, usage={"prompt_tokens": 10, "completion_tokens": 5})
    duplicate = ledger.finish_call(call_id, usage={"prompt_tokens": 10, "completion_tokens": 5})
    assert first["revision"] == duplicate["revision"]
    assert duplicate["actual_usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
    assert len(db.scalars(select(CallUsageReceipt)).all()) == 2
    summary = ledger.summarize_calls()
    assert summary["call_count"] == 1
    assert summary["actual_usage"] == {"prompt_tokens": 10, "completion_tokens": 5}


def test_incremental_receipts_need_stable_ids_and_reject_conflicting_replays():
    call_id = begin()
    with pytest.raises(ledger.CallLedgerError):
        ledger.record_usage(call_id, {"tool_search": 1}, cumulative=False)
    ledger.record_usage(call_id, {"tool_search": 1}, cumulative=False, event_id="event-one")
    first = ledger.record_usage(call_id, {"tool_search": 1}, cumulative=False, event_id="event-one")
    assert first["usage"] == {"tool_search": 1}
    with pytest.raises(ledger.CallLedgerConflict):
        ledger.record_usage(call_id, {"tool_search": 2}, cumulative=False, event_id="event-one")
    assert ledger.record_usage(call_id, {"tool_search": 1}, cumulative=False, event_id="event-two")["usage"] == {"tool_search": 2}
    with pytest.raises(ledger.CallLedgerError):
        ledger.record_usage(call_id, {"tool_search": 2}, cumulative=True)


def test_missing_usage_and_prices_are_unknown_but_actual_zero_is_a_known_fact():
    empty = ledger.finish_call(begin())
    zero = ledger.finish_call(begin(), usage={"prompt_tokens": 0})
    assert empty["usage_source"] == "unknown" and empty["amount"] is None
    assert zero["usage_source"] == "actual" and zero["usage"] == {"prompt_tokens": 0}
    summary = ledger.summarize_calls()
    assert summary["unknown_usage_calls"] == 1
    assert summary["unknown_amount_calls"] == 2
    assert summary["amounts"] == {} and not summary["amount_complete"]


def test_provider_actual_usage_replaces_estimate_without_double_counting():
    call_id = begin()
    ledger.record_usage(call_id, {"input_tokens": 100}, source="estimated")
    final = ledger.finish_call(call_id, usage={"input_tokens": 80})
    assert final["actual_usage"] == {"input_tokens": 80}
    assert final["estimated_usage"] == {"input_tokens": 100}
    assert final["usage_source"] == "actual" and final["usage"] == {"input_tokens": 80}
    summary = ledger.summarize_calls()
    assert summary["actual_usage"] == {"input_tokens": 80} and summary["estimated_usage"] == {}


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "interrupted", "unknown"])
def test_call_terminal_states_are_persistent_and_not_reopened(state):
    call_id = begin()
    first = ledger.finish_call(call_id, state=state)
    assert first["state"] == state and first["finished_at"]
    assert ledger.finish_call(call_id, state=state)["revision"] == first["revision"]
    with pytest.raises(ledger.CallLedgerConflict):
        ledger.finish_call(call_id, state="failed" if state != "failed" else "completed")
    if state == "unknown":
        assert ledger.finish_call(call_id, state="completed", reconcile=True)["state"] == "completed"


def test_synthetic_usage_cannot_enter_real_actual_or_cost_totals():
    ledger.finish_call(begin(run_id="run"), usage={"input_tokens": 10})
    fake = ledger.finish_call(begin(run_id="run", synthetic=True), usage={"input_tokens": 999},
                              price_version="test-rates-v1", unit_prices={"input_tokens": "1"}, currency="USD")
    assert fake["usage_source"] == "estimated" and fake["actual_usage"] == {}
    assert fake["amount"] is None
    default = ledger.summarize_calls(run_id="run")
    assert default["real_call_count"] == 1 and default["synthetic_call_count"] == 0
    assert default["actual_usage"] == {"input_tokens": 10}
    all_calls = ledger.summarize_calls(run_id="run", include_synthetic=True)
    assert all_calls["synthetic_call_count"] == 1
    assert all_calls["synthetic_usage"] == {"input_tokens": 999}
    assert all_calls["actual_usage"] == {"input_tokens": 10} and all_calls["estimated_usage"] == {}


def test_endpoint_and_usage_are_allowlisted_and_sensitive_content_is_not_stored(db):
    secret = "test-key-61e8530c90d01d1f"
    call_id = ledger.begin_call(provider="provider", service="chat", endpoint=
        f"https://user:{secret}@example.test/token/{secret}/v1?q={secret}#{secret}")
    saved = ledger.finish_call(call_id, usage={"input_tokens": 12, "prompt": secret, "api_key": secret,
        "server_tool_use": {"tool_search": 2, "request_body": secret}}, error_kind="HTTP400")
    assert saved["endpoint"] == "https://example.test/[redacted]/v1"
    assert saved["usage"] == {"input_tokens": 12, "tool_search": 2}
    payload = json.dumps(saved)
    assert secret not in payload and "user:" not in payload
    assert secret not in json.dumps(db.get(CallRecord, call_id).actual_usage)
    with pytest.raises(TypeError):
        begin(prompt=secret)
    with pytest.raises(ledger.CallLedgerError):
        begin(model_id=secret)


@pytest.mark.parametrize("usage", [{"input_tokens": -1}, {"input_tokens": True}, {"input_tokens": 1.2},
                                    {"input_tokens": 10**400}, {"audio_seconds": float("nan")},
                                    {"audio_seconds": float("inf")},
                                    {"tool_search": 1, "server_tool_use": {"tool_search": 2}}])
def test_invalid_or_conflicting_counts_are_not_recorded(usage):
    call_id = begin()
    with pytest.raises(ledger.CallLedgerError):
        ledger.record_usage(call_id, usage)
    assert ledger.get_call(call_id)["usage_source"] == "unknown"


def test_decimal_component_estimates_require_a_version_and_do_not_claim_total_charge():
    call_id = begin()
    ledger.record_usage(call_id, {"input_tokens": 3, "tool_search": 2})
    with pytest.raises(ledger.CallLedgerError):
        ledger.finish_call(call_id, unit_prices={"input_tokens": "0.1"}, currency="USD")
    final = ledger.finish_call(call_id, price_version="test-rates-v1", unit_prices={"input_tokens": "0.1"}, currency="USD")
    assert final["amount"] == "0.3" and final["amount_source"] == "estimated_partial"
    assert final["amount_scope"] == "priced_units_only" and final["priced_units"] == ["input_tokens"]
    summary = ledger.summarize_calls()
    assert summary["amounts"] == {"USD": "0.3"} and summary["amount_complete"] is False
    with pytest.raises(ledger.CallLedgerConflict):
        ledger.finish_call(call_id, price_version="changed", unit_prices={"input_tokens": "0.2"}, currency="USD")


def test_missing_priced_units_remain_unknown_and_explicit_zero_rate_is_known_component_estimate():
    unknown = ledger.finish_call(begin(), usage={"input_tokens": 2}, price_version="test-rates-v1",
                                  unit_prices={"output_tokens": "0.1"}, currency="USD")
    assert unknown["amount"] is None
    zero = ledger.finish_call(begin(), usage={"input_tokens": 2}, price_version="test-rates-v1",
                               unit_prices={"input_tokens": "0"}, currency="USD")
    assert zero["amount"] == "0" and zero["amount_source"] == "estimated_partial"


def test_explicit_db_transaction_is_flushed_but_owned_by_caller(db):
    call_id = begin(db=db)
    ledger.finish_call(call_id, usage={"input_tokens": 5}, db=db)
    assert db.get(CallRecord, call_id).state == "completed"
    db.rollback()
    with SessionLocal() as other:
        assert other.get(CallRecord, call_id) is None


def test_context_persists_before_body_and_saves_only_exception_class():
    secret = "test-key-2fc12a3973cc3a62"
    call_id = None
    with pytest.raises(RuntimeError, match=secret):
        with ledger.tracked_call(provider="provider", service="fetch", endpoint="https://example.test") as call:
            call_id = call.call_id
            assert ledger.get_call(call_id)["state"] == "started"
            raise RuntimeError(secret)
    saved = ledger.get_call(call_id)
    assert saved["state"] == "failed" and saved["error_kind"] == "RuntimeError"
    assert secret not in json.dumps(saved)


def test_context_auto_finishes_and_marks_cancellation_without_swallowing_it(db):
    with ledger.tracked_call(provider="provider", service="fetch", endpoint="https://example.test") as call:
        call.usage({"requests": 1})
    assert call.snapshot()["state"] == "completed"
    with pytest.raises(asyncio.CancelledError):
        with ledger.tracked_call(provider="provider", service="fetch", endpoint="https://example.test") as cancelled:
            raise asyncio.CancelledError()
    assert cancelled.snapshot()["state"] == "cancelled"
    with pytest.raises(ledger.CallLedgerError):
        with ledger.tracked_call(provider="provider", service="fetch", endpoint="https://example.test", db=db):
            pass


def test_native_billing_counts_and_display_blocks_are_not_conflated():
    ledger.finish_call(begin(), usage={"web_search_requests": 1, "server_tool_use": {"tool_search": 3, "tool_fetch": 2}})
    counts = ledger.summarize_calls()["actual_usage"]
    assert counts == {"web_search_requests": 1, "tool_search": 3, "tool_fetch": 2}


def test_retry_usage_includes_real_failed_attempt_but_not_repeated_finish():
    first = begin(run_id="run", attempt_id="attempt-one")
    ledger.finish_call(first, state="failed", usage={"input_tokens": 10})
    retry = begin(run_id="run", attempt_id="attempt-one", retry_of_call_id=first)
    ledger.finish_call(retry, usage={"input_tokens": 12})
    ledger.finish_call(retry, usage={"input_tokens": 12})
    another_attempt = begin(run_id="run", attempt_id="attempt-two")
    ledger.finish_call(another_attempt, usage={"input_tokens": 3})
    attempt = ledger.summarize_calls(run_id="run", attempt_id="attempt-one")
    assert attempt["call_count"] == 2 and attempt["actual_usage"] == {"input_tokens": 22}
    assert ledger.summarize_calls(run_id="run")["actual_usage"] == {"input_tokens": 25}


@pytest.mark.parametrize("model_id", ["DeepSeek-V4-flash", "Provider Model (thinking)", "Org/Model: v2 + Fast",
                                      "模型（增强版）", "model_name [preview] @v1.2"])
def test_model_names_keep_provider_case_spaces_and_punctuation(model_id):
    call_id = begin(model_id=model_id)
    assert ledger.get_call(call_id)["model_id"] == model_id


@pytest.mark.parametrize("model_id", ["", "  ", "model\nnext", "model\tname", "model\u2028next", "x" * 257,
                                      "Bearer test-key-419a33ebcfe9234c", "test-key-419a33ebcfe9234c", "model(api_key=synthetic)",
                                      "model?access_token=synthetic"])
def test_invalid_model_names_fail_without_including_the_value(model_id):
    with pytest.raises(ledger.CallLedgerError) as error:
        begin(model_id=model_id)
    if model_id.strip():
        assert model_id not in str(error.value)


def test_call_listing_filters_scope_and_pages_only_sanitized_facts():
    expected = {begin(run_id="run", attempt_id="attempt") for _ in range(3)}
    other = begin(run_id="run", attempt_id="other")
    begin(run_id="different", attempt_id="attempt")
    synthetic = begin(run_id="run", attempt_id="attempt", synthetic=True)
    first = ledger.list_calls(run_id="run", attempt_id="attempt", limit=2)
    assert len(first["items"]) == 2 and first["has_more"] and first["next_offset"] == 2
    second = ledger.list_calls(run_id="run", attempt_id="attempt", limit=2, offset=first["next_offset"])
    assert len(second["items"]) == 1 and not second["has_more"] and second["next_offset"] is None
    assert {item["id"] for page in (first, second) for item in page["items"]} == expected
    assert {item["id"] for item in ledger.list_calls(run_id="run")["items"]} == expected | {other}
    assert synthetic in {item["id"] for item in ledger.list_calls(run_id="run", include_synthetic=True)["items"]}


@pytest.mark.parametrize("values", [{"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1},
                                     {"offset": 1.2}, {"offset": 10**7}, {"include_synthetic": "yes"}])
def test_call_listing_has_strict_bounded_pagination(values):
    with pytest.raises(ledger.CallLedgerError):
        ledger.list_calls(**values)
