"""Real APIClient protocol paths using only isolated HTTP MockTransport."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import select

from app import config, llm
from app import call_ledger as ledger
from app.db import SessionLocal
from app.ledger_models import CallRecord, CallUsageReceipt
from app.runtime_context import bind_runtime_config, current_runtime_config


KEY = "test-key-bc97609f50d8ed6f"
PROMPT = "synthetic-private-user-prompt-never-store-in-call-ledger"
ANSWER = "synthetic-private-assistant-body-never-store-in-call-ledger"
MODEL = "Provider Model (Thinking) / Case-V4"
USAGE = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}


@pytest.fixture(autouse=True)
def isolated_model_settings(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_base_url", "https://meter.example.test/V1")
    monkeypatch.setattr(config.settings, "llm_api_key", KEY)
    monkeypatch.setattr(config.settings, "llm_model", MODEL)
    monkeypatch.setattr(config.settings, "reasoning_effort", "")


@pytest.fixture
def transport(monkeypatch):
    binding()  # Initialize the shared parent's identity before threaded callers.
    real_client = httpx.Client
    calls = []
    handler = [lambda request: response(usage=USAGE)]

    def dispatch(request):
        calls.append(request)
        # Admission must be committed and visible before the HTTP transport.
        assert any(item["state"] == "started" for item in ledger.list_calls(include_synthetic=True)["items"])
        return handler[0](request)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(dispatch), **kwargs))
    return calls, handler


def response(*, usage=None):
    body = {"choices": [{"message": {"role": "assistant", "content": ANSWER}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def messages():
    return [{"role": "user", "content": PROMPT}]


def binding(run="meter-run", attempt="meter-attempt"):
    # Request tests use real parent identities; an arbitrary ledger label must
    # not be accepted as authorization for a missing/deleted task.
    from app.models import Conversation
    from app.runtime_models import RuntimeRun, RunAttempt
    from app.goal_ledger import ensure_run_goal
    with SessionLocal() as db:
        cid = "meter-conversation-" + run
        if db.get(Conversation, cid) is None:
            db.add(Conversation(id=cid)); db.flush()
        goal = ensure_run_goal(db, run, cid, "Synthetic metering request")
        row = db.get(RuntimeRun, run)
        if row is None:
            row = RuntimeRun(id=run, kind="foreground", conversation_id=cid, goal_id=goal["id"])
            db.add(row)
        row.current_attempt_id = attempt
        if db.get(RunAttempt, attempt) is None:
            count = db.query(RunAttempt).filter_by(run_id=run).count()
            db.add(RunAttempt(id=attempt, run_id=run, number=count + 1, writer_token="meter-writer-" + attempt, status="running"))
        db.commit()
    return {"run_id": run, "attempt_id": attempt, "conversation_id": cid, "goal_id": goal["id"], "model_id": MODEL}


def sse(*payloads, done=True):
    body = "".join("data: " + json.dumps(payload) + "\n\n" for payload in payloads)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("usage", [USAGE, None, {"prompt_tokens": 0, "completion_tokens": 0}])
def test_success_records_one_actual_call_or_unknown_usage_without_inventing_zero(transport, usage):
    calls, handler = transport
    handler[0] = lambda request: response(usage=usage)
    with bind_runtime_config(binding()):
        result = llm.APIClient().chat(messages())
    assert result.content == ANSWER
    assert len(calls) == 1
    row, = ledger.list_calls(run_id="meter-run", attempt_id="meter-attempt")["items"]
    assert row["state"] == "completed" and row["model_id"] == MODEL
    assert row["usage_source"] == ("actual" if usage else "unknown")
    assert row["usage"] == (usage or {})
    assert row["amount"] is None and row["amount_source"] == "unknown"


def test_stream_cumulative_and_duplicate_final_usage_are_not_added_to_each_other(transport):
    _, handler = transport
    handler[0] = lambda request: sse(
        {"choices": [{"delta": {"reasoning_content": "synthetic reasoning"}}]},
        {"choices": [{"delta": {"content": ANSWER}}], "usage": {"prompt_tokens": 8, "completion_tokens": 1}},
        {"choices": [], "usage": USAGE},
        {"choices": [], "usage": USAGE},
    )
    with bind_runtime_config(binding()):
        events = list(llm.APIClient().stream_chat(messages()))
    assert events[0] == ("delta", "thinking", "synthetic reasoning")
    assert events[-1][0] == "final" and events[-1][1].content == ANSWER
    row, = ledger.list_calls()["items"]
    assert row["service"] == "chat_stream" and row["state"] == "completed"
    assert row["usage"] == USAGE
    assert ledger.summarize_calls()["actual_usage"] == USAGE
    with SessionLocal() as db:
        receipts = db.scalars(select(CallUsageReceipt)).all()
        assert len(receipts) == 2, "distinct cumulative snapshots persist; duplicate final and finish do not add another"
        assert {item.units["completion_tokens"] for item in receipts} == {1, 3}


@pytest.mark.parametrize("failure", ["status", "connect", "invalid_json"])
def test_each_failed_http_attempt_has_a_new_call_and_retry_chain(transport, failure):
    calls, handler = transport
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "connect":
                raise httpx.ConnectError(f"private response {KEY} {PROMPT}", request=request)
            if failure == "invalid_json":
                return httpx.Response(200, text=f"private non-json {KEY}")
            return httpx.Response(503, text=f"private error body {KEY} {PROMPT}")
        return response(usage=USAGE)

    handler[0] = respond
    client = llm.APIClient()
    with bind_runtime_config(binding()):
        with pytest.raises((httpx.HTTPError, ValueError)):
            client.chat(messages())
        assert client.chat(messages()).content == ANSWER
    assert len(calls) == 2
    rows = ledger.list_calls()["items"]
    failed, = [row for row in rows if row["state"] == "failed"]
    completed, = [row for row in rows if row["state"] == "completed"]
    assert failed["id"] != completed["id"]
    assert completed["retry_of_call_id"] == failed["id"] and completed["retry_index"] == 1
    assert completed["request_id"] == failed["request_id"]
    assert ledger.summarize_calls()["call_count"] == 2
    assert ledger.summarize_calls()["unknown_usage_calls"] == 1
    payload = json.dumps(rows)
    assert KEY not in payload and PROMPT not in payload and ANSWER not in payload


def test_different_request_or_attempt_does_not_inherit_a_previous_failure_chain(transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(500, text="synthetic failure")
    client = llm.APIClient()
    with bind_runtime_config(binding(attempt="first-attempt")):
        with pytest.raises(httpx.HTTPStatusError):
            client.chat(messages())
    handler[0] = lambda request: response(usage=USAGE)
    with bind_runtime_config(binding(attempt="second-attempt")):
        client.chat(messages())
    completed, = ledger.list_calls(attempt_id="second-attempt")["items"]
    assert completed["retry_of_call_id"] is None and completed["retry_index"] == 0


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b'data: {"choices": [{"delta": {"content": "partial answer"}}], "usage": {"completion_tokens": 1}}\n\n'
        raise httpx.ReadError(f"synthetic half-stream failure {KEY} {PROMPT}")


def test_half_stream_transport_failure_preserves_attempt_and_does_not_claim_completion(transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(200, stream=BrokenStream(),
                                               headers={"content-type": "text/event-stream"})
    client = llm.APIClient()
    with bind_runtime_config(binding()):
        stream = client.stream_chat(messages())
        assert next(stream) == ("delta", "answer", "partial answer")
        with pytest.raises(httpx.ReadError):
            next(stream)
    row, = ledger.list_calls()["items"]
    assert row["state"] == "failed" and row["error_kind"] == "ReadError"
    assert row["usage"] == {"completion_tokens": 1}


def test_failed_stream_retry_creates_another_billable_attempt_in_the_same_request_chain(transport):
    _, handler = transport
    handler[0] = lambda request: httpx.Response(200, stream=BrokenStream(),
                                               headers={"content-type": "text/event-stream"})
    client = llm.APIClient()
    with bind_runtime_config(binding()):
        with pytest.raises(httpx.ReadError):
            list(client.stream_chat(messages()))
        handler[0] = lambda request: sse({"choices": [{"delta": {"content": ANSWER}}]},
                                        {"choices": [], "usage": USAGE})
        assert list(client.stream_chat(messages()))[-1][1].content == ANSWER
    rows = ledger.list_calls()["items"]
    failed, = [row for row in rows if row["state"] == "failed"]
    retried, = [row for row in rows if row["state"] == "completed"]
    assert retried["retry_of_call_id"] == failed["id"]
    assert retried["request_id"] == failed["request_id"]
    assert ledger.summarize_calls()["actual_usage"]["completion_tokens"] == 4


def test_consumer_closing_a_live_generator_records_cancelled_not_completed(transport):
    _, handler = transport
    handler[0] = lambda request: sse(
        {"choices": [{"delta": {"content": "first"}}]},
        {"choices": [{"delta": {"content": "second"}}]},
        {"choices": [], "usage": USAGE},
    )
    with bind_runtime_config(binding()):
        stream = llm.APIClient().stream_chat(messages())
        assert next(stream) == ("delta", "answer", "first")
        stream.close()
    row, = ledger.list_calls()["items"]
    assert row["state"] == "cancelled" and row["usage_source"] == "unknown"


def test_live_usage_is_durable_before_stream_completion_or_consumer_cancellation(transport):
    _, handler = transport
    handler[0] = lambda request: sse(
        {"choices": [{"delta": {"content": "partial"}}], "usage": {"completion_tokens": 2}},
        {"choices": [], "usage": USAGE},
    )
    with bind_runtime_config(binding()):
        stream = llm.APIClient().stream_chat(messages())
        assert next(stream) == ("delta", "answer", "partial")
        active, = ledger.list_calls()["items"]
        assert active["state"] == "started" and active["usage"] == {"completion_tokens": 2}
        stream.close()
    cancelled, = ledger.list_calls()["items"]
    assert cancelled["state"] == "cancelled" and cancelled["usage"] == {"completion_tokens": 2}


def test_private_credentials_prompts_and_raw_bodies_never_enter_the_ledger(transport):
    calls, handler = transport
    handler[0] = lambda request: response(usage={**USAGE, "prompt": PROMPT, "api_key": KEY,
        "server_tool_use": {"tool_search": 2, "body": ANSWER}})
    with bind_runtime_config(binding()):
        llm.APIClient().chat(messages())
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"
    assert PROMPT in calls[0].content.decode()
    with SessionLocal() as db:
        payload = json.dumps([{column.name: getattr(row, column.name) for column in CallRecord.__table__.columns}
                              for row in db.scalars(select(CallRecord))], default=str)
    for private in (KEY, PROMPT, ANSWER):
        assert private not in payload
    assert "authorization" not in payload.lower()
    assert ledger.list_calls()["items"][0]["usage"]["tool_search"] == 2


def test_runtime_binding_is_restored_and_scoped_calls_can_be_queried_independently(transport):
    client = llm.APIClient()
    assert current_runtime_config() is None
    for run, attempt in (("run-a", "attempt-a"), ("run-b", "attempt-b")):
        with bind_runtime_config(binding(run=run, attempt=attempt)):
            client.chat(messages())
    assert current_runtime_config() is None
    first, = ledger.list_calls(run_id="run-a")["items"]
    second, = ledger.list_calls(run_id="run-b")["items"]
    assert first["attempt_id"] == "attempt-a" and second["attempt_id"] == "attempt-b"
    assert first["retry_of_call_id"] is None and second["retry_of_call_id"] is None


def test_frozen_client_identity_is_used_when_no_execution_context_exists(transport, monkeypatch):
    from app import model_profiles

    monkeypatch.setattr(model_profiles, "resolve_model_config", lambda value: {
        "base_url": "https://meter.example.test/V1", "api_key": KEY,
        "model_id": MODEL, "reasoning_effort": ""})
    client = llm.APIClient(runtime_config=binding(run="frozen-run", attempt="frozen-attempt"))
    client.chat(messages())
    row, = ledger.list_calls()["items"]
    assert row["run_id"] == "frozen-run" and row["attempt_id"] == "frozen-attempt"


def test_explicit_synthetic_http_fixtures_are_separated_from_real_call_statistics(transport):
    with bind_runtime_config(binding()):
        llm.APIClient(synthetic=True).chat(messages())
    assert ledger.list_calls()["items"] == []
    row, = ledger.list_calls(include_synthetic=True)["items"]
    assert row["synthetic"] and row["usage_source"] == "estimated" and row["actual_usage"] == {}
    summary = ledger.summarize_calls(include_synthetic=True)
    assert summary["actual_usage"] == {} and summary["synthetic_usage"] == USAGE


def test_shared_client_threads_keep_same_payload_failure_and_retry_chains_independent(transport):
    _, handler = transport
    client = llm.APIClient()
    worker = threading.local()
    a_failed, b_failed, a_done = (threading.Event() for _ in range(3))
    observed = {"a": [], "b": []}
    lock = threading.Lock()

    def respond(request):
        label = worker.label
        active = [row for row in ledger.list_calls()["items"] if row["state"] == "started"]
        assert len(active) == 1
        with lock:
            observed[label].append(active[0]["id"])
            number = len(observed[label])
        return httpx.Response(503, text="synthetic first attempt") if number == 1 else response(usage=USAGE)

    handler[0] = respond

    def run(label):
        worker.label = label
        with bind_runtime_config(binding()):
            if label == "b":
                assert a_failed.wait(10)
            with pytest.raises(httpx.HTTPStatusError):
                client.chat(messages())
            if label == "a":
                a_failed.set()
                assert b_failed.wait(10)
                assert client.chat(messages()).content == ANSWER
                a_done.set()
            else:
                b_failed.set()
                assert a_done.wait(10)
                assert client.chat(messages()).content == ANSWER
        assert current_runtime_config() is None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, label) for label in ("a", "b")]
        for future in futures:
            future.result(timeout=30)
    for label in ("a", "b"):
        first, retry = [ledger.get_call(call_id) for call_id in observed[label]]
        assert first["state"] == "failed" and retry["state"] == "completed"
        assert retry["retry_of_call_id"] == first["id"]
        assert retry["request_id"] == first["request_id"]
    assert ledger.get_call(observed["a"][0])["request_id"] != ledger.get_call(observed["b"][0])["request_id"]
    assert ledger.summarize_calls()["call_count"] == 4


def test_shared_client_concurrent_runs_keep_context_local_run_and_attempt_identity(transport):
    _, handler = transport
    barrier = threading.Barrier(2)
    client = llm.APIClient()

    def respond(request):
        barrier.wait(timeout=10)
        return response(usage=USAGE)

    handler[0] = respond

    def run(label):
        with bind_runtime_config(binding(run=f"run-{label}", attempt=f"attempt-{label}")):
            assert client.chat(messages()).content == ANSWER
        assert current_runtime_config() is None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, label) for label in ("a", "b")]
        for future in futures:
            future.result(timeout=30)
    for label in ("a", "b"):
        row, = ledger.list_calls(run_id=f"run-{label}")["items"]
        assert row["attempt_id"] == f"attempt-{label}"
        assert row["state"] == "completed" and row["usage"] == USAGE
