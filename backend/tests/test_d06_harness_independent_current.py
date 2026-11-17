"""Independent D06 adversaries; every provider and event stream is synthetic."""
from copy import deepcopy
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import threading
import time

import httpx
import pytest

from scripts import verify_real_acceptance as harness
from scripts import review_real_acceptance as deriver


RUN = "chat_independent_observer"
CID = "independent-observer-conversation"
ATTEMPT = "independent-attempt-1"
ANSWER = "Synthetic public final answer."
PRIVATE = "SYNTHETIC-PRIVATE-REASONING-NOT-A-RECEIPT"


def response(events):
    return httpx.Response(200, text="\n\n".join("data:" + json.dumps(e) for e in events))


def event_fixture():
    initial = {"kind": "snapshot", "run_id": RUN, "conversation_id": CID,
        "attempt_id": ATTEMPT, "status": "running", "terminal": False,
        "cursor": 0, "seq": 0, "partial_answer": ""}
    delta = {"kind": "answer_delta", "run_id": RUN, "attempt_id": ATTEMPT,
        "seq": 1, "text": ANSWER}
    end = {"kind": "stream_end", "run_id": RUN, "attempt_id": ATTEMPT,
        "seq": 2, "status": "completed", "terminal": True}
    terminal = {**initial, "status": "completed", "terminal": True,
        "cursor": 2, "seq": 2, "partial_answer": ANSWER}
    return [initial, delta, end], [terminal]


def receipt(live, terminal, **changes):
    args = dict(run_id=RUN, conversation_id=CID, answer=ANSWER, request_after=0,
        call_count_before=2, call_count_after=2)
    return harness.observer_receipt(response(live), response(terminal), **{**args, **changes})


def test_missing_initial_attempt_cannot_certify_outputs_from_two_different_attempts():
    live, terminal = event_fixture()
    live[0]["attempt_id"] = None
    live[1]["attempt_id"] = "unrelated-attempt-A"
    live[2]["attempt_id"] = "unrelated-attempt-B"
    observed = receipt(live, terminal)
    assert observed["live_followup_verified"] is False, observed


def test_terminal_get_from_different_attempt_cannot_certify_original_attempt_no_replay():
    live, terminal = event_fixture()
    terminal[0]["attempt_id"] = "different-restarted-attempt"
    observed = receipt(live, terminal)
    assert observed["live_followup_verified"] is True
    assert observed["terminal_no_replay_verified"] is False, observed


@pytest.fixture
def production_events():
    """Collect the real production generator; mutations below remain explicit."""
    from app.chat_events import RunChannel
    from app.api.chat import _observe
    channel = RunChannel(RUN, CID, attempt_id=ATTEMPT, durable=False)
    initial, queue = channel.subscribe()
    channel.broadcast(channel.record("thinking", {"text": PRIVATE}))
    channel.broadcast(channel.record("answer_delta", {"text": ANSWER}))
    channel.broadcast(channel.record("answer", {"answer": ANSWER}))
    channel.broadcast(channel.end("completed"))

    async def collect(snapshot, observer):
        return "".join([frame async for frame in _observe(channel, observer, snapshot=snapshot)])

    live = harness.events_from(httpx.Response(200, text=asyncio.run(collect(initial, queue))))
    terminal, queue = channel.subscribe()
    final = harness.events_from(httpx.Response(200, text=asyncio.run(collect(terminal, queue))))
    return live, final


def test_production_generator_binds_public_output_and_omits_private_text(production_events):
    observed = receipt(*production_events)
    assert observed["live_followup_verified"] is True, observed
    assert observed["terminal_no_replay_verified"] is True, observed
    assert observed["later_output_events"] == 2
    assert PRIVATE not in json.dumps(observed)


@pytest.mark.parametrize("mutation", ["foreign_run", "foreign_conversation", "foreign_attempt",
    "old_output", "no_output", "wrong_answer", "terminal_first", "no_terminal"])
def test_production_stream_mutations_cannot_certify_live_followup(production_events, mutation):
    live, final = deepcopy(production_events)
    output = next(e for e in live if e["kind"] == "answer_delta")
    if mutation == "foreign_run": output["run_id"] = "chat_unrelated_run"
    elif mutation == "foreign_conversation": output["conversation_id"] = "other-conversation"
    elif mutation == "foreign_attempt": output["attempt_id"] = "other-attempt"
    elif mutation == "old_output": output["seq"] = live[0]["cursor"]
    elif mutation == "no_output": live = [e for e in live if e["kind"] not in ("answer_delta", "answer")]
    elif mutation == "wrong_answer":
        for e in live:
            if e["kind"] == "answer": e["answer"] = "Different final output."
    elif mutation == "terminal_first": live[0].update(terminal=True, status="completed")
    elif mutation == "no_terminal": live = [e for e in live if e["kind"] != "stream_end"]
    observed = receipt(live, final)
    assert observed["live_followup_verified"] is False, observed


@pytest.mark.parametrize("before,after", [(2, 3), (True, True), (2.0, 2.0), (-1, -1)])
def test_no_replay_requires_exact_nonnegative_unchanged_terminal_count(production_events, before, after):
    observed = receipt(*production_events, call_count_before=before, call_count_after=after)
    assert observed["live_followup_verified"] is True
    assert observed["terminal_no_replay_verified"] is False


def test_actual_observer_endpoint_follows_same_run_without_any_model_dispatch(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import chat_events, call_ledger
    from app.api.chat import router
    from app.llm import APIClient
    from app.models import ForegroundRun
    db.add(ForegroundRun(id=RUN, conversation_id=CID, question="Synthetic observer probe", status="running"))
    db.commit()
    channel = chat_events.begin(RUN, CID, attempt_id=ATTEMPT, durable=True)
    attempts = []

    def forbidden(*a, **k):
        attempts.append(True)
        raise AssertionError("Observer tried to start a model")

    monkeypatch.setattr(APIClient, "chat", forbidden)
    monkeypatch.setattr(APIClient, "stream_chat", forbidden)
    failures = []

    def producer():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not channel.subscribers:
            time.sleep(.005)
        if not channel.subscribers:
            failures.append("Observer never subscribed")
        channel.broadcast(channel.record("answer_delta", {"text": ANSWER}))
        channel.broadcast(channel.record("answer", {"answer": ANSWER}))
        channel.broadcast(channel.end("completed"))

    app = FastAPI()
    app.include_router(router)
    worker = threading.Thread(target=producer)
    before = call_ledger.summarize_calls(db=db)["call_count"]
    worker.start()
    with TestClient(app) as client:
        live = client.get(f"/api/chat/runs/{RUN}/events", params={"conversation_id": CID, "after": 0})
        worker.join(6)
        final = client.get(f"/api/chat/runs/{RUN}/events", params={"conversation_id": CID, "after": 0})
    after = call_ledger.summarize_calls(db=db)["call_count"]
    assert not worker.is_alive() and not failures and not attempts
    observed = harness.observer_receipt(live, final, run_id=RUN, conversation_id=CID,
        answer=ANSWER, request_after=0, call_count_before=before, call_count_after=after)
    assert observed["live_followup_verified"] is True, observed
    assert observed["terminal_no_replay_verified"] is True, observed


@pytest.fixture
def actual_budget_client(monkeypatch):
    """Keep the actual APIClient, meter and journal, substitute only HTTP."""
    from app import config
    from app.llm import APIClient
    monkeypatch.setattr(config.settings, "llm_base_url", "https://independent.example.test/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", "synthetic-independent-credential")
    monkeypatch.setattr(config.settings, "llm_model", "Synthetic-Independent-Model")
    budget = harness.EvaluationRequestBudget(1, 30, {"independent.example.test"})
    actual_client = httpx.Client
    entered, responses = [], []

    def dispatch(transport, request):
        entered.append(request.url.host)
        result = responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    limited = budget.wrap_transport(dispatch)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: actual_client(*a,
        transport=httpx.MockTransport(lambda request: limited(None, request)), **k))
    monkeypatch.setattr(APIClient, "_meter", budget.wrap_meter(APIClient._meter))
    return APIClient(synthetic=True), budget, entered, responses


def invoke_review(client, journal, durable, review=None):
    from app.agent.answer_review_journal import cached_review
    return cached_review(journal, "review", review=review or (lambda: {
        "status": "passed", "text": client.chat([{"role": "user", "content": "Synthetic input"}]).content}),
        persist=lambda: durable.append(deepcopy(journal)), before_review=lambda: None, llm=client)


def model_reply():
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": "Synthetic successful review"}}], "usage": {"prompt_tokens": 2,
        "completion_tokens": 3, "total_tokens": 5}})


def assert_unknown_never_redispatches(client, journal, durable, entered):
    count = len(entered)
    assert journal == durable[-1] == {"review": {"review_dispatch": "pending"}}
    result = invoke_review(client, deepcopy(journal), durable,
        review=lambda: (_ for _ in ()).throw(AssertionError("Unknown review bought again")))
    assert result["error_code"] == "dispatch_outcome_unknown"
    assert len(entered) == count


def test_real_client_pre_meter_denial_clears_only_new_unsubmitted_review(actual_budget_client):
    from app.resource_budget import ResourceBudgetExceeded
    from app.call_ledger import list_calls
    client, budget, entered, responses = actual_budget_client
    budget.metered_model_attempts = 1
    journal, durable = {}, []
    with pytest.raises(ResourceBudgetExceeded): invoke_review(client, journal, durable)
    assert durable == [{"review": {"review_dispatch": "pending"}}, {}]
    assert journal == {} and entered == [] and list_calls(include_synthetic=True)["items"] == []
    assert budget.snapshot()["admission_denials"][0]["boundary"] == "model_meter_admission"


def test_real_client_started_meter_transport_denial_stays_unknown(actual_budget_client):
    from app.resource_budget import ResourceBudgetExceeded
    from app.call_ledger import list_calls
    client, budget, entered, responses = actual_budget_client
    budget.real_provider_calls = 1
    journal, durable = {}, []
    with pytest.raises(ResourceBudgetExceeded): invoke_review(client, journal, durable)
    assert entered == []
    assert_unknown_never_redispatches(client, journal, durable, entered)
    row, = list_calls(include_synthetic=True)["items"]
    assert row["state"] == "failed" and client._call_context.last_started_call_id == row["id"]
    denial, = budget.snapshot()["admission_denials"]
    assert denial["dispatch_started"] is False and denial["proof_scope"] == "this_admission_only_not_prior_calls_in_the_review"


def test_real_client_lost_response_keeps_uncertain_dispatch(actual_budget_client):
    client, budget, entered, responses = actual_budget_client
    responses.append(httpx.ReadTimeout("Synthetic response lost"))
    journal, durable = {}, []
    with pytest.raises(httpx.ReadTimeout): invoke_review(client, journal, durable)
    assert entered == ["independent.example.test"] and budget.real_provider_calls == 1
    assert budget.snapshot()["admission_denials"] == []
    assert_unknown_never_redispatches(client, journal, durable, entered)


def test_real_success_before_second_admission_denial_cannot_erase_whole_review(actual_budget_client):
    from app.resource_budget import ResourceBudgetExceeded
    client, budget, entered, responses = actual_budget_client
    responses.append(model_reply())
    journal, durable = {}, []

    def twice():
        client.chat([{"role": "user", "content": "First synthetic call"}])
        client.chat([{"role": "user", "content": "Second denied call"}])

    with pytest.raises(ResourceBudgetExceeded): invoke_review(client, journal, durable, review=twice)
    assert len(entered) == 1 and client._call_context.failed_call is None
    assert_unknown_never_redispatches(client, journal, durable, entered)


def test_prior_success_does_not_make_new_pre_meter_denial_unknown(actual_budget_client):
    from app.resource_budget import ResourceBudgetExceeded
    client, budget, entered, responses = actual_budget_client
    responses.append(model_reply())
    client.chat([{"role": "user", "content": "Historical synthetic success"}])
    old = client._call_context.last_started_call_id
    journal, durable = {}, []
    with pytest.raises(ResourceBudgetExceeded): invoke_review(client, journal, durable)
    assert client._call_context.last_started_call_id == old and len(entered) == 1
    assert journal == durable[-1] == {}
