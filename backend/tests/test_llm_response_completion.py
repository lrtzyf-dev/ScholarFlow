"""Provider EOF, malformed output and tool JSON never certify a completed call."""
import json

import httpx
import pytest

from app import call_ledger, config, llm


PRIVATE = "test-key-8d6a84dbe89f73ef"
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_base_url", "https://protocol.example.test/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", PRIVATE)
    monkeypatch.setattr(config.settings, "llm_model", "Exact-Model-ID")
    real_client = httpx.Client
    responses = []
    requests = []

    def respond(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        *args, transport=httpx.MockTransport(respond), **kwargs))
    return responses, requests


def sse(*chunks, done=False):
    body = "".join("data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk)) + "\n\n"
                   for chunk in chunks)
    return httpx.Response(200, text=body + ("data: [DONE]\n\n" if done else ""),
                          headers={"content-type": "text/event-stream"})


def stream():
    return llm.APIClient().stream_chat([{"role": "user", "content": "synthetic question"}])


def only_call(state="failed"):
    row, = call_ledger.list_calls()["items"]
    assert row["state"] == state
    assert PRIVATE not in json.dumps(row)
    return row


def test_clean_transport_eof_without_provider_completion_retains_delta_and_usage(provider):
    responses, requests = provider
    responses.append(sse({"choices": [{"delta": {"content": "half answer"}}], "usage": USAGE}))
    iterator = stream()
    assert next(iterator) == ("delta", "answer", "half answer")
    with pytest.raises(llm.IncompleteModelResponse, match="中断"):
        next(iterator)
    assert only_call()["usage"] == USAGE
    assert len(requests) == 1, "the client never blindly resends a partly delivered call"


@pytest.mark.parametrize("done,reason", [(True, None), (False, "stop"), (True, "stop")])
def test_done_marker_or_valid_finish_reason_accepts_compatible_gateway(provider, done, reason):
    responses, _ = provider
    responses.append(sse({"choices": [{"delta": {"content": "answer"}, "finish_reason": reason}]},
                         {"choices": [], "usage": USAGE}, done=done))
    events = list(stream())
    assert events[-1][0] == "final" and events[-1][1].content == "answer"
    assert only_call("completed")["usage"] == USAGE


@pytest.mark.parametrize("reason", ["length", "max_tokens", "content_filter", "future_unknown"])
def test_incomplete_finish_reason_never_completes_even_with_done_marker(provider, reason):
    responses, _ = provider
    responses.append(sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": reason}]},
                         {"choices": [], "usage": USAGE}, done=True))
    iterator = stream()
    assert next(iterator)[2] == "partial"
    with pytest.raises(llm.ModelResponseError):
        list(iterator)
    assert only_call()["usage"] == USAGE


@pytest.mark.parametrize("bad_chunk", [
    "malformed " + PRIVATE, [], {"error": {"message": PRIVATE}},
    {"choices": {}}, {"choices": [None]}, {"choices": [{"delta": []}]},
    {"choices": [{"delta": {"content": {"private": PRIVATE}}}]},
    {"choices": [{"delta": {"tool_calls": {}}}]},
    {"choices": [{"delta": {"tool_calls": [None]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": True}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": -1}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"function": []}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": {}}}]}}]},
])
def test_malformed_sse_or_http_200_error_is_sanitized_failure(provider, bad_chunk):
    responses, _ = provider
    responses.append(sse(bad_chunk, done=True))
    with pytest.raises(llm.ModelResponseError) as caught:
        list(stream())
    assert PRIVATE not in str(caught.value)
    only_call()


@pytest.mark.parametrize("arguments", ['{"path":', "[]", "null", "true", "1", PRIVATE])
def test_invalid_stream_tool_arguments_are_never_replaced_with_empty_object(provider, arguments):
    responses, _ = provider
    responses.append(sse({"choices": [{"delta": {"tool_calls": [{"index": 0,
        "id": "call-1", "function": {"name": "write_file", "arguments": arguments}}]},
        "finish_reason": "tool_calls"}]}, {"choices": [], "usage": USAGE}, done=True))
    with pytest.raises(llm.ModelResponseError) as caught:
        list(stream())
    assert PRIVATE not in str(caught.value)
    assert only_call()["usage"] == USAGE


def test_fragmented_valid_tool_arguments_are_validated_before_final_delivery(provider):
    responses, _ = provider
    responses.append(sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call-1",
            "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1,
            "function": {"arguments": '"資料.txt"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": USAGE}))
    event, = list(stream())
    assert event[0] == "final"
    assert event[1].tool_calls == [{"id": "call-1", "name": "read_file", "arguments": {"path": "資料.txt"}}]
    only_call("completed")


@pytest.mark.parametrize("chunks", [
    [], [{"choices": [{"delta": {"reasoning_content": "thinking only"}}]}],
    [{"choices": [{"delta": {"content": "first"}, "finish_reason": "stop"}]},
     {"choices": [{"delta": {"content": "late"}}]}],
    [{"choices": [{"delta": {"content": "first"}, "finish_reason": "stop"}]},
     {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}],
])
def test_empty_thinking_only_or_conflicting_completion_is_not_deliverable(provider, chunks):
    responses, _ = provider
    responses.append(sse(*chunks, done=True))
    with pytest.raises(llm.ModelResponseError):
        list(stream())
    only_call()


@pytest.mark.parametrize("message,reason", [
    ({"content": "partial"}, "length"),
    ({"content": "filtered"}, "content_filter"),
    ({"content": "only text"}, "unknown"),
    ({"content": "", "reasoning_content": "thinking"}, "stop"),
    ({"content": "", "tool_calls": [{"function": {"name": "write", "arguments": "{"}}]}, "tool_calls"),
    ({"content": "", "tool_calls": [{"function": {"name": "write", "arguments": "[]"}}]}, "tool_calls"),
    ({"content": "", "tool_calls": [{}]}, "tool_calls"),
    ({"content": [], "reasoning_content": "thinking"}, "stop"),
])
def test_non_stream_invalid_or_truncated_response_records_failed_with_actual_usage(provider, message, reason):
    responses, _ = provider
    responses.append(httpx.Response(200, json={"choices": [{"message": message,
                                                          "finish_reason": reason}], "usage": USAGE}))
    with pytest.raises(llm.ModelResponseError):
        llm.APIClient().chat([{"role": "user", "content": "synthetic"}])
    assert only_call()["usage"] == USAGE


def test_non_stream_http_200_error_ignores_private_provider_message(provider):
    responses, _ = provider
    responses.append(httpx.Response(200, json={"error": {"message": PRIVATE}, "usage": USAGE}))
    with pytest.raises(llm.ModelResponseError) as caught:
        llm.APIClient().chat([])
    assert PRIVATE not in str(caught.value)
    assert only_call()["usage"] == USAGE


@pytest.mark.parametrize("streaming", [False, True])
def test_tool_finish_without_actual_calls_is_not_an_answer(provider, streaming):
    responses, _ = provider
    choice = {"finish_reason": "tool_calls"}
    if streaming:
        responses.append(sse({"choices": [{**choice, "delta": {"content": "I will do it"}}]}, done=True))
        action = lambda: list(stream())
    else:
        responses.append(httpx.Response(200, json={"choices": [{**choice, "message": {"content": "I will do it"}}]}))
        action = lambda: llm.APIClient().chat([])
    with pytest.raises(llm.ModelResponseError, match="没有返回工具调用"):
        action()
    only_call()


@pytest.mark.parametrize("bad_usage", [PRIVATE, 0, False, {"completion_tokens": -1},
                                      {"prompt_tokens": PRIVATE}, {"completion_tokens": 1.5}])
@pytest.mark.parametrize("streaming", [False, True])
def test_invalid_provider_usage_never_strands_call_as_started(provider, bad_usage, streaming):
    from app.call_ledger import CallLedgerError
    responses, _ = provider
    if streaming:
        responses.append(sse({"choices": [{"delta": {"content": "answer"}}], "usage": USAGE},
                             {"choices": [], "usage": bad_usage}, done=True))
        action = lambda: list(stream())
    else:
        responses.append(httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}], "usage": bad_usage}))
        action = lambda: llm.APIClient().chat([])
    with pytest.raises(CallLedgerError) as caught:
        action()
    assert PRIVATE not in str(caught.value)
    assert only_call()["usage"] == (USAGE if streaming else {})


@pytest.mark.parametrize("reason", [None, "length"])
def test_real_executor_retains_checkpoint_without_delivering_incomplete_stream(provider, db, monkeypatch, reason):
    from copy import deepcopy
    from app.agent import loop
    from app.models import QARecord

    responses, requests = provider
    responses.append(sse({"choices": [{"delta": {"content": "saved partial output"}, "finish_reason": reason}],
                          "usage": USAGE}, done=reason is not None))
    model = llm.APIClient()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [])
    monkeypatch.setattr(loop, "_load_history", lambda *args, **kwargs: ([], {"total": 0}))
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *args: "")
    monkeypatch.setattr(loop.goal_store, "get_current_goal", lambda: None)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *args, **kwargs: {
        "ran": 0, "blocked": False, "feedback": ""})
    saved = []

    def persist(snapshot):
        saved.append(deepcopy(snapshot))
        db.commit()

    events = list(loop.run_agent(db, "synthetic question", conversation_id="protocol-c",
                                 save_checkpoint=persist))
    assert any(kind == "answer_delta" and event["text"] == "saved partial output" for kind, event in events)
    assert any(kind == "error" and "IncompleteModelResponse" in event["text"] for kind, event in events)
    assert not any(kind in ("answer", "done") for kind, _ in events)
    assert db.query(QARecord).count() == 0
    assert saved[-1]["phase"] == "model"
    assert len(requests) == 1 and only_call()["usage"] == USAGE
