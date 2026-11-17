"""Native Chat contract fixtures; every transport and credential is synthetic."""
from copy import deepcopy
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import service_tools, service_tool_config
from app.db import SessionLocal
from app.models import Conversation, ConversationOptions
from app.provider_models import ProviderOperationContext
from app.providers import deepseek_research as native
from app.runtime_context import bind_runtime_config
from app.service_tool_models import ServiceToolInvocation

KEY = "synthetic-native-research-key"
CID = "native-research-conversation"
SIGNATURE = "synthetic-exact-opaque+/="
SERVER = [{"index": 0, "reasoning_content": "private early reasoning", "calls": [{
    "index": 0, "id": "server-read", "type": "deepseek_read_url", "kind": "read_link", "status": "completed",
    "action": {"type": "read_link", "url": "https://example.com/paper"},
    "page": {"title": "Synthetic page", "url": "https://example.com/paper"}, "signature": SIGNATURE}]}]
ANSWER = "研究😀结果"
ANNOTATIONS = [{"type": "url_citation", "url_citation": {
    "url": "https://example.com/paper", "title": "Synthetic page", "start_index": 2, "end_index": 5}}]


def events():
    call = {"id": "server-read", "type": "deepseek_read_url", "action": {"url": "https://example.com/paper"}, "status": "completed", "signature": SIGNATURE}
    values = [
        {"delta": {"reasoning_content": "private early reasoning"}},
        {"delta": {}, "server_tool_event": {"type": "step_reasoning_done", "step": 0}},
        {"delta": {}, "server_tool_event": {"type": "call_started", "step": 0, "call": call}},
        {"delta": {}, "server_tool_event": {"type": "call_completed", "step": 0, "call": call}},
        {"delta": {"reasoning_content": "private final reasoning"}},
        {"delta": {"content": ANSWER}},
        {"delta": {"server_tool_calls": SERVER, "annotations": ANNOTATIONS}, "finish_reason": "stop"},
    ]
    return "".join("data: " + json.dumps({"choices": [dict(index=0, **v)]}) + "\n\n" for v in values) + "data: [DONE]\n\n"


@pytest.fixture
def setup(db, monkeypatch):
    db.add(Conversation(id=CID))
    db.add(ConversationOptions(conversation_id=CID, approval_mode="always", revision=1))
    service_tool_config.save_configuration(db, {"expected_version": 0, "enabled_services": ["research"], "api_key": KEY})
    db.commit()
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer " + KEY
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        return httpx.Response(200, text=events(), headers={"Content-Type": "text/event-stream"})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(handle)))
    return {"conversation_id": CID, "approval_mode": "always", "policy_revision": 1}, calls


def execute(db, binding, args=None, key="native:test"):
    args = args or {"query": "Synthetic research"}
    prepared = service_tools.prepare_deepseek_tool(db, "deepseek_research", args,
        conversation_id=CID, binding=binding, operation_key=key)
    captured = []
    with bind_runtime_config(binding):
        iterator = service_tools.execute_deepseek_tool_events(db, "deepseek_research", args, prepared=prepared)
        try:
            while True:
                captured.append(next(iterator))
        except StopIteration as complete:
            result = json.loads(complete.value)
    return captured, result, prepared


def test_native_phase_streams_public_activities_then_durable_report_and_replays(db, setup):
    binding, calls = setup
    captured, result, prepared = execute(db, binding)
    assert [v["kind"] for v in captured] == ["commentary", "commentary", "provider_report"]
    assert len(calls) == 1 and calls[0]["tools"] == [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}]
    assert result["citations"][0]["excerpt"] == "😀结果", "Offsets are Unicode code points"
    assert result["evidence"] == [] and result["sources"] == [] and result["original_text_verified"] is False
    assert SIGNATURE not in json.dumps(captured) + json.dumps(result)
    assert "private final reasoning" not in json.dumps(captured)
    with SessionLocal() as check:
        saved = check.get(ProviderOperationContext, prepared.receipt_id)
        assistant = saved.payload["messages"][-1]["_provider"]["assistant"]
        assert assistant["server_tool_calls"] == SERVER
        assert assistant["reasoning_content"] == "private final reasoning"
    replay_events, replay, _ = execute(db, binding)
    assert replay_events == [] and replay["replayed_receipt"] and len(calls) == 1


def test_native_followup_is_bound_and_roundtrips_exact_provider_context(db, setup):
    binding, calls = setup
    _, first, _ = execute(db, binding)
    _, second, _ = execute(db, binding, {"query": "Continue synthetic", "followup_id": first["research_id"]}, "native:second")
    previous = calls[1]["messages"][-2]
    assert previous["server_tool_calls"] == SERVER and previous["reasoning_content"] == "private final reasoning"
    assert "_provider" not in previous and second["research_id"] != first["research_id"]
    with SessionLocal.begin() as check:
        service_tool_config.save_configuration(check, {"expected_version": 1, "enabled_services": ["research"]})
    with pytest.raises(service_tool_config.ServiceConfigurationError, match="同一服务"):
        execute(db, binding, {"query": "new config", "followup_id": first["research_id"]}, "native:third")
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [
    {"url": "javascript:alert(1)", "start_index": 0, "end_index": 1},
    {"url": "https://example.com", "start_index": 0, "end_index": 600},
    {"url": "https://example.com", "start_index": True, "end_index": 1},
])
def test_native_invalid_citation_is_not_promoted(bad):
    with pytest.raises(ValueError):
        native.public_citations([{"type": "url_citation", "url_citation": bad}], ANSWER)


def test_native_activity_never_exposes_unknown_private_fields():
    call = deepcopy(SERVER[0]["calls"][0])
    call["reasoning_content"] = "private"
    public = native.public_activity({"type": "call_completed", "call": call})
    assert SIGNATURE not in json.dumps(public) and "private" not in json.dumps(public)
    assert native.public_activity({"type": "step_reasoning_done", "step": 0}) is None


def test_native_disabled_preview_is_local_and_api_accepts_explicit_setting(db):
    assert not any(s["name"] == "deepseek_research" for s in service_tools.available_deepseek_tool_specs())
    client = TestClient(app)
    response = client.put("/api/service-tools/deepseek", json={"expected_version": 0, "enabled_services": ["research"], "api_key": KEY})
    assert response.status_code == 200 and KEY not in response.text
    assert any(s["name"] == "deepseek_research" for s in service_tools.available_deepseek_tool_specs())


def test_both_new_services_require_explicit_switches_and_revoke_locally(db):
    client = TestClient(app)
    response = client.put("/api/service-tools/deepseek", json={"expected_version": 0, "enabled_services": ["research", "files"], "api_key": KEY})
    assert response.status_code == 200
    assert {s["name"] for s in service_tools.available_deepseek_tool_specs()} == {"deepseek_research", "deepseek_file_read"}
    assert client.delete("/api/service-tools/deepseek/credential?expected_version=1").status_code == 200
    assert service_tools.available_deepseek_tool_specs() == []


def test_native_budget_pause_before_dispatch_keeps_receipt_prepared(db, setup, monkeypatch):
    from contextlib import contextmanager
    from app import request_resources
    from app.resource_budget import ResourceBudgetExceeded
    binding, calls = setup
    @contextmanager
    def denied(**kwargs):
        raise ResourceBudgetExceeded("Synthetic user budget reached")
        yield
    monkeypatch.setattr(request_resources, "request_admission", denied)
    with pytest.raises(ResourceBudgetExceeded) as raised:
        execute(db, binding)
    assert raised.value.service_request_not_dispatched and not calls
    with SessionLocal() as check:
        assert check.query(ServiceToolInvocation).one().state == "prepared"


def test_real_executor_forwards_native_activity_before_delivery_and_keeps_private_payload_out(db, setup, monkeypatch):
    from app import approval_store, mcp_policy
    from app.agent import loop
    from app.agent.checkpoint import AgentState
    from app.llm import LLMResponse
    binding, calls = setup
    monkeypatch.setattr(mcp_policy, "load_policy", lambda: {"disabled_tools": [], "enabled_tools": [], "tool_rules": {}})
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(approval_store, "_pending", {})
    class Model:
        def stream_chat(self, messages, tools=None):
            if not any(m["role"] == "tool" for m in messages):
                yield "final", LLMResponse("", [{"id": "native-executor-call", "name": "deepseek_research", "arguments": {"query": "Synthetic research"}}])
            else:
                yield "final", LLMResponse("本轮流程核验结束。")
    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    binding = {**binding, "mode": "mock", "model_id": "Synthetic-Unrelated-Parent"}
    from app.goal_ledger import ensure_run_goal
    from app.runtime_models import RuntimeRun
    goal = ensure_run_goal(db, "native-executor", CID, "q")
    db.add(RuntimeRun(id="native-executor", kind="foreground", conversation_id=CID, goal_id=goal["id"]))
    db.commit()
    snapshot = AgentState(phase="model", question="q", conversation_id=CID, runtime_config=binding).snapshot()
    emitted = []
    for kind, event in loop.run_agent(db, "q", conversation_id=CID, run_id="native-executor", checkpoint=snapshot):
        emitted.append((kind, event))
        if kind == "approval_request":
            assert not calls and event["arguments"]["protocol"] == "openai_chat_native_tools"
            approval_store.resolve(event["approval_id"], "approve")
    order = [kind for kind, _ in emitted]
    assert len(calls) == 1 and order.index("commentary") < order.index("provider_report") < order.index("tool_result") < order.index("answer")
    assert SIGNATURE not in json.dumps(emitted) and "private final reasoning" not in json.dumps(emitted)
