"""Official Chat fixtures, synthetic transport only; no real provider calls."""
from copy import deepcopy
import json

import httpx
import pytest

from app.llm import APIClient, LLMResponse, ModelResponseError
from app.providers.chat import adapter_for, stored_assistant
from app.agent.checkpoint import AgentState
from app.conversation_memory import history_messages
from app.models import QARecord


BASE = "https://api.deepseek.com/v1"
KEY = "synthetic-provider-roundtrip-key"
MODEL = "deepseek-flash"
TOOLS = [{"name": "lookup", "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}]
SERVER = [{"index": 0, "reasoning_content": "first-step",
           "calls": [{"index": 0, "id": "srvtoolu-read-1", "type": "deepseek_read_url", "kind": "read_link",
                      "action": {"type": "read_link", "url": "https://example.com/research"},
                      "status": "completed", "page": {"url": "https://example.com/research", "title": "Synthetic"},
                      "signature": "synthetic-opaque-exact+/=", "unknown_future": {"order": [2, 1]}}]}]


class Client(APIClient):
    def _connection(self, reasoning_effort):
        return BASE, KEY, MODEL, reasoning_effort or ""


def response(message, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}],
                                  "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})


def install(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real(*args, transport=httpx.MockTransport(handler), **kwargs))


def test_three_tool_rounds_keep_reasoning_exact_arguments_and_restart_payload(db, monkeypatch):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        turn = len(calls)
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        assert all("_provider" not in m for m in body["messages"])
        for index, message in enumerate(assistants, 1):
            assert message["reasoning_content"] == f"reasoning {index}"
            assert message["tool_calls"][0]["function"]["arguments"] == f'{{ "n" : {index} }}'
            assert "unknown_future" not in message
        if turn <= 3:
            return response({"role": "assistant", "content": "", "reasoning_content": f"reasoning {turn}",
                "unknown_future": {"nested": ["preserve", turn]},
                "tool_calls": [{"id": f"call-{turn}", "type": "function", "function": {
                    "name": "lookup", "arguments": f'{{ "n" : {turn} }}'}}]}, "tool_calls")
        return response({"role": "assistant", "content": "Complete.", "reasoning_content": "final reasoning"})

    install(monkeypatch, handle)
    state = AgentState(question="Synthetic", messages=[{"role": "user", "content": "Synthetic"}])
    for turn in range(1, 4):
        answer = Client().chat(state.messages, tools=TOOLS)
        assert answer.provider_payload["assistant"]["unknown_future"]["nested"] == ["preserve", turn]
        assert "provider_payload" not in answer.to_dict()
        state.messages.append(answer.assistant_message())
        state.messages.append({"role": "tool", "tool_call_id": f"call-{turn}", "content": "Synthetic result"})
        state = AgentState.model_validate(json.loads(state.model_dump_json()))
    final = Client().chat(state.messages, tools=TOOLS)
    assert final.content == "Complete." and len(calls) == 4


@pytest.mark.parametrize("changed", ["endpoint", "model", "key"])
def test_provider_opaque_data_never_crosses_binding(changed):
    original = adapter_for(BASE, MODEL, KEY)
    raw = {"role": "assistant", "content": "Original answer.", "reasoning_content": "private replay",
           "server_tool_calls": SERVER, "unknown_future": {"signed": "keep internally"}}
    answer = LLMResponse("Accepted answer.", reasoning_content="private replay", provider_payload=original.envelope(raw))
    message = answer.assistant_message()
    next_adapter = adapter_for("https://other.example.test/v1" if changed == "endpoint" else BASE,
                              "other-model" if changed == "model" else MODEL,
                              "other-synthetic-key" if changed == "key" else KEY)
    migrated = next_adapter.messages([message])[0]
    assert migrated == {"role": "assistant", "content": "Accepted answer."}
    assert message["_provider"]["assistant"] == raw
    same = original.messages([message])[0]
    assert same["server_tool_calls"] == SERVER
    assert same["content"] == "Accepted answer.", "Never revive an answer rejected by source review"
    assert same["reasoning_content"] == "private replay"
    assert "unknown_future" not in same


def test_final_context_survives_new_conversation_turn_and_database_reload(db):
    adapter = adapter_for(BASE, MODEL, KEY)
    raw = {"role": "assistant", "content": "Previous answer[^e1]", "reasoning_content": "last step",
           "server_tool_calls": SERVER, "annotations": [{"type": "url_citation", "url_citation": {
               "url": "https://example.com/research", "title": "Synthetic", "start_index": 0, "end_index": 8}}]}
    answer = LLMResponse(raw["content"], reasoning_content="last step", provider_payload=adapter.envelope(raw))
    record = QARecord(question="Synthetic prior question", answer=answer.content, provider_context=answer.persisted_context())
    db.add(record)
    db.commit()
    identity = record.id
    db.expunge_all()
    history = history_messages([db.get(QARecord, identity)])
    wired = adapter.messages(history)[1]
    assert wired["server_tool_calls"] == SERVER and wired["reasoning_content"] == "last step"
    assert f"[历史证据:{identity}:e1]" in wired["content"]
    assert "annotations" not in wired, "Display annotations do not become model input arguments"


def test_stream_saves_final_step_reasoning_and_exact_server_context(db, monkeypatch):
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "first-step"}}]},
        {"choices": [{"index": 0, "delta": {}, "server_tool_event": {"type": "step_reasoning_done", "step": 0}}]},
        {"choices": [{"index": 0, "delta": {}, "server_tool_event": {"type": "call_started", "step": 0,
            "call": {"index": 0, "id": "srvtoolu-read-1", "action": {"type": "read_link", "url": "https://example.com/research"}}}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "final-step"}}]},
        {"choices": [{"index": 0, "delta": {"content": "Answer"}}]},
        {"choices": [{"index": 0, "delta": {"server_tool_calls": SERVER, "annotations": [], "unknown_final": {"a": 1}},
                      "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}},
    ]
    wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    install(monkeypatch, lambda request: httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"}))
    result = list(Client().stream_chat([{"role": "user", "content": "Synthetic"}]))[-1][1]
    assert result.reasoning_content == "first-stepfinal-step"
    raw = result.provider_payload["assistant"]
    assert raw["reasoning_content"] == "final-step" and raw["server_tool_calls"] == SERVER
    assert result.provider_payload["stream_extensions"][-1]["delta"]["unknown_final"] == {"a": 1}
    replay = adapter_for(BASE, MODEL, KEY).messages([result.assistant_message()])[0]
    assert replay["server_tool_calls"] == SERVER and replay["reasoning_content"] == "final-step"


def test_only_official_endpoint_uses_deepseek_request_options():
    deepseek = adapter_for(BASE, MODEL, KEY)
    _, _, body = deepseek.request(KEY, [{"role": "user", "content": "test"}], reasoning_effort="none")
    assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body
    _, _, thinking = deepseek.request(KEY, [{"role": "user", "content": "test"}], reasoning_effort="high")
    assert thinking["reasoning_effort"] == "high" and "temperature" not in thinking
    proxy = adapter_for("https://compatible.example.test/v1", MODEL, KEY)
    _, _, generic = proxy.request(KEY, [{"role": "user", "content": "test"}], reasoning_effort="none")
    assert "thinking" not in generic and generic["reasoning_effort"] == "none"


def test_executor_generated_ids_do_not_break_raw_function_argument_bytes():
    adapter = adapter_for(BASE, MODEL, KEY)
    raw = {"role": "assistant", "content": None, "reasoning_content": "",
           "tool_calls": [{"id": "", "type": "function", "function": {"name": "lookup", "arguments": '{ "n": 2 }'}}]}
    result = LLMResponse("", [{"id": "", "name": "lookup", "arguments": {"n": 2}}], provider_payload=adapter.envelope(raw))
    message = result.assistant_message([{"id": "call-recovered", "name": "lookup", "arguments": {"n": 2}}])
    wired = adapter.messages([message])[0]
    assert wired["tool_calls"][0]["id"] == "call-recovered"
    assert wired["tool_calls"][0]["function"]["arguments"] == '{ "n": 2 }'
    assert raw["tool_calls"][0]["id"] == ""


def test_public_history_and_fork_keep_provider_replay_private(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import Conversation
    from app.chat_events import _public
    cid = "private-provider-fork"
    db.add(Conversation(id=cid, title="Synthetic provider history"))
    adapter = adapter_for(BASE, MODEL, KEY)
    context = LLMResponse("Accepted", provider_payload=adapter.envelope({"role": "assistant", "content": "Accepted",
        "reasoning_content": "private-replay-only", "server_tool_calls": SERVER})).persisted_context()
    record = QARecord(conversation_id=cid, question="Synthetic", answer="Accepted", provider_context=context)
    db.add(record); db.commit()
    client = TestClient(app)
    before = client.get(f"/api/conversations/{cid}")
    assert before.status_code == 200 and "private-replay-only" not in before.text and "provider_context" not in before.text
    fork = client.post(f"/api/conversations/{cid}/fork", json={"after_message_id": record.id})
    assert fork.status_code == 200, fork.text
    db.expire_all()
    copied = db.query(QARecord).filter_by(conversation_id=fork.json()["id"]).one()
    assert copied.provider_context == context
    assert "private-replay-only" not in client.get(f"/api/conversations/{copied.conversation_id}").text
    assert _public({"answer": "Accepted", "provider_context": context, "nested": {"signature": "private"}}) == {"answer": "Accepted", "nested": {}}
