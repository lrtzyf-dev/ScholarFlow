"""Untrusted numeric JSON, candidate identities and transport diagnostics."""
import json

import httpx
import pytest

from app import call_ledger, config, llm
from app.agent import loop


PRIVATE = "test-key-c3b90da2f16692a4"
URL_PRIVATE = "test-key-c865640b6ee11e27"
USAGE = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_base_url", f"https://edge.example.test/{URL_PRIVATE}/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", PRIVATE)
    monkeypatch.setattr(config.settings, "llm_model", "Synthetic-Case-Model")
    monkeypatch.setattr(config.settings, "reasoning_effort", "")
    real_client, responses, requests = httpx.Client, [], []

    def respond(request):
        requests.append(request)
        response = responses.pop(0)
        return response(request) if callable(response) else response

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(respond), **kwargs))
    return responses, requests


def sse(*chunks, done=True):
    text = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    return httpx.Response(200, text=text + ("data: [DONE]\n\n" if done else ""),
                          headers={"content-type": "text/event-stream"})


def action(streaming):
    client = llm.APIClient()
    return list(client.stream_chat([])) if streaming else client.chat([])


def failed_call(kind):
    row, = call_ledger.list_calls()["items"]
    assert row["state"] == "failed" and row["error_kind"] == kind
    assert PRIVATE not in json.dumps(row) and URL_PRIVATE not in json.dumps(row)
    return row


@pytest.mark.parametrize("arguments", [
    '{"amount":NaN}', '{"amount":Infinity}', '{"amount":-Infinity}',
    '{"amount":1e999}', '{"amount":-1e999}', '{"nested":[{"amount":1e999}]}',
])
@pytest.mark.parametrize("streaming", [False, True])
def test_nonfinite_tool_json_is_rejected_at_every_nesting_level(provider, arguments, streaming):
    responses, _ = provider
    call = {"id": "call-edge", "function": {"name": "synthetic_apply", "arguments": arguments}}
    if streaming:
        responses.append(sse({"choices": [{"delta": {"tool_calls": [{"index": 0, **call}]},
                                             "finish_reason": "tool_calls"}], "usage": USAGE}))
    else:
        responses.append(httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]},
                                                                "finish_reason": "tool_calls"}], "usage": USAGE}))
    with pytest.raises(llm.ModelResponseError):
        action(streaming)
    assert failed_call("ModelResponseError")["usage"] == USAGE


@pytest.mark.parametrize("index", [1, -1, True, "0", 0.0, None])
def test_a_different_or_invalid_candidate_cannot_complete_the_primary_partial_answer(provider, index):
    responses, _ = provider
    responses.append(sse(
        {"choices": [{"index": 0, "delta": {"content": "saved primary partial"}}], "usage": USAGE},
        {"choices": [{"index": index, "delta": {}, "finish_reason": "stop"}]}, done=False))
    stream = llm.APIClient().stream_chat([])
    assert next(stream) == ("delta", "answer", "saved primary partial")
    with pytest.raises(llm.ModelResponseError):
        list(stream)
    assert failed_call("ModelResponseError")["usage"] == USAGE


@pytest.mark.parametrize("choice_identity", [{}, {"index": 0}])
def test_primary_or_omitted_candidate_index_keeps_finish_reason_without_done_compatibility(provider, choice_identity):
    responses, _ = provider
    responses.append(sse({"choices": [{**choice_identity, "delta": {"content": "Complete answer"},
                                        "finish_reason": "stop"}]}, done=False))
    events = list(llm.APIClient().stream_chat([]))
    assert events[-1][0] == "final" and events[-1][1].content == "Complete answer"
    row, = call_ledger.list_calls()["items"]
    assert row["state"] == "completed"


@pytest.mark.parametrize("status", [400, 429, 500, 503])
@pytest.mark.parametrize("streaming", [False, True])
def test_http_status_errors_hide_reason_body_and_url_but_preserve_category_and_retry_signal(provider, status, streaming):
    responses, requests = provider
    responses.append(httpx.Response(status, json={"error": {"message": PRIVATE}},
                                    extensions={"reason_phrase": PRIVATE.encode()}))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        action(streaming)
    error = caught.value
    assert type(error) is httpx.HTTPStatusError
    assert str(status) in str(error)
    assert PRIVATE not in str(error) and URL_PRIVATE not in str(error) and "edge.example.test" not in str(error)
    assert error.request is requests[0] and error.response.status_code == status
    assert error.__suppress_context__ is True
    assert loop._is_retryable(error) is (status in (429, 500, 503))
    failed_call("HTTPStatusError")


@pytest.mark.parametrize("error_type", [
    httpx.ReadError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout,
    httpx.ProxyError, httpx.RemoteProtocolError,
])
@pytest.mark.parametrize("streaming", [False, True])
def test_transport_errors_hide_upstream_text_and_retain_exact_exception_type(provider, error_type, streaming):
    responses, requests = provider

    def failed(request):
        raise error_type(f"{PRIVATE} https://{URL_PRIVATE}@edge.example.test", request=request)

    responses.append(failed)
    with pytest.raises(error_type) as caught:
        action(streaming)
    error = caught.value
    assert type(error) is error_type
    assert PRIVATE not in str(error) and URL_PRIVATE not in str(error) and "edge.example.test" not in str(error)
    assert error.request is requests[0] and error.__suppress_context__ is True
    assert loop._is_retryable(error) is issubclass(error_type, httpx.TimeoutException)
    failed_call(error_type.__name__)


def prepare_executor(monkeypatch, *, specs=()):
    monkeypatch.setattr(loop, "get_llm", lambda: llm.APIClient())
    monkeypatch.setattr(loop, "get_tool_specs", lambda: list(specs))
    monkeypatch.setattr(loop, "_load_history", lambda *args, **kwargs: ([], {"total": 0}))
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *args: "")
    monkeypatch.setattr(loop.goal_store, "get_current_goal", lambda: None)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *args, **kwargs: {
        "ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(config.settings, "approval_mode", "full_access")


def test_real_executor_never_dispatches_nonfinite_number_that_would_bypass_schema_bounds(provider, db, monkeypatch):
    from app.models import QARecord
    responses, requests = provider
    name = "mcp__synthetic__apply"
    spec = {"name": name, "parameters": {"type": "object", "properties": {
        "amount": {"type": "number", "minimum": 0, "maximum": 1}}, "required": ["amount"]}}
    responses.append(sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-nan",
        "function": {"name": name, "arguments": '{"amount":NaN}'}}]}, "finish_reason": "tool_calls"}]}))
    prepare_executor(monkeypatch, specs=[spec])
    monkeypatch.setattr(loop, "execute_tool", lambda *args, **kwargs: pytest.fail("invalid numeric JSON was dispatched"))
    events = list(loop.run_agent(db, "Synthetic action", conversation_id="numeric-edge"))
    assert any(kind == "error" and "ModelResponseError" in event["text"] for kind, event in events)
    assert not any(kind in ("tool_call", "answer", "done") for kind, _ in events)
    assert db.query(QARecord).count() == 0 and len(requests) == 1
    failed_call("ModelResponseError")


def test_real_executor_error_event_hides_provider_status_phrase_and_url(provider, db, monkeypatch):
    responses, _ = provider
    responses.append(httpx.Response(400, text=PRIVATE, extensions={"reason_phrase": PRIVATE.encode()}))
    prepare_executor(monkeypatch)
    events = list(loop.run_agent(db, "Synthetic request", conversation_id="http-edge"))
    errors = [event["text"] for kind, event in events if kind == "error"]
    assert len(errors) == 1 and "HTTPStatusError" in errors[0] and "400" in errors[0]
    assert PRIVATE not in errors[0] and URL_PRIVATE not in errors[0]
    failed_call("HTTPStatusError")
