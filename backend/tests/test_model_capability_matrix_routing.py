"""Exact profile diagnosis and production routing; all provider HTTP is synthetic."""
from contextlib import contextmanager
import json
import re
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import model_capabilities as probes, model_profiles as profiles
from app.db import SessionLocal
from app.llm import APIClient
from app.main import app
from app.model_capability_policy import CapabilityUnavailable
from app.models import DesktopState, ModelProfile
from app.runtime_context import bind_runtime_config


def response(content="OK", **extra):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], **extra})


def stream(tool=None):
    chunks = []
    if tool:
        chunks.append({"choices": [{"index": 0, "delta": {}, "server_tool_event": {"type": "call_completed", "call": {
            "id": "synthetic-native-call", "type": tool, "status": "completed", "action": {"queries": ["synthetic test"]}}}}]})
    chunks.append({"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]})
    return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")


@pytest.fixture
def target(db, monkeypatch):
    row = profiles.save_profile(db, {"name": "Capability Matrix", "model_id": "Exact-Model-Case",
        "base_url": "https://matrix.example.test/v1", "api_key": "synthetic-matrix-key"})
    db.commit()
    init = probes._Client.__init__
    def synthetic(self, *a, **k):
        init(self, *a, **k)
        self._synthetic = True
    monkeypatch.setattr(probes._Client, "__init__", synthetic)
    calls, handler, original = [], [lambda request: response()], httpx.Client
    def dispatch(request):
        calls.append(request)
        return handler[0](request)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(dispatch)}))
    return row.id, calls, handler


def probe(target, capability, diagnostic_id=None):
    pid, _calls, _handler = target
    return probes.run(pid, capability, probes.preview(pid)["expected_config"], diagnostic_id)


def official(db, target):
    row = db.get(ModelProfile, target[0])
    profiles.save_profile(db, {"base_url": "https://api.deepseek.com/v1", "api_key": "synthetic-matrix-key"}, row)
    db.commit()


def test_nine_independent_probes_preview_never_calls_provider(db, target):
    value = probes.preview(target[0])
    assert len(value["probes"]) == 9 and value["remote_requests"] == 0 and not target[1]
    assert set(value["capabilities"]) == set(probes.PROBES)
    assert all(v == "unknown" for v in value["capabilities"].values())
    assert "synthetic-matrix-key" not in json.dumps(value)


@pytest.mark.parametrize("content,status", [('{"answer":17,"ok":true}', "supported"),
    ('{"answer":17,"ok":1}', "unknown"), ('{"answer":true,"ok":true}', "unknown"),
    ('Here is JSON: {"answer":17,"ok":true}', "unknown"), ('{}', "unknown")])
def test_structured_requires_actual_json_object_with_correct_types(db, target, content, status):
    def handle(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == "Exact-Model-Case"
        return response(content)
    target[2][0] = handle
    result = probe(target, "structured")
    assert result["status"] == status and result["request_count"] == 1
    assert result["endpoint"] == "https://matrix.example.test/v1/chat/completions"
    assert result["profile_version"] == 1 and result["tested_at"]


@pytest.mark.parametrize("usage,status", [(None, "unknown"), ({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, "supported"),
    ({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 9}, "unknown"),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "unknown")])
def test_usage_is_confirmed_independently_and_missing_is_not_zero(db, target, usage, status):
    target[2][0] = lambda request: response(**({"usage": usage} if usage else {}))
    result = probe(target, "usage")
    assert result["status"] == status and result["request_count"] == 1
    # Synthetic HTTP exercises production usage parsing without inventing a
    # real-provider bill. The ledger intentionally labels its counts estimated.
    assert result["calls"][0]["synthetic"] is True
    assert result["calls"][0]["usage_source"] == ("estimated" if usage else "unknown")
    if status == "supported": assert result["observations"]["usage_scope"] == "chat_nonstream_tokens"


@pytest.mark.parametrize("capability", ["files", "native_tools"])
def test_unimplemented_native_protocol_is_explicit_without_sending_key_elsewhere(db, target, capability):
    result = probe(target, capability)
    assert result["status"] == "unsupported" and result["failure_kind"] == "adapter_protocol"
    assert result["request_count"] == 0 and not target[1]
    assert "独立搜索/OCR" in result["detail"]


def test_native_requires_actual_search_and_read_activities_on_exact_profile(db, target):
    official(db, target)
    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "Exact-Model-Case" and len(body["tools"]) == 1
        return stream(body["tools"][0]["type"])
    target[2][0] = handle
    result = probe(target, "native_tools")
    assert result["status"] == "supported" and result["request_count"] == 2
    assert result["observations"]["confirmed_native_tools"] == ["deepseek_web_search", "deepseek_read_url"]


def test_native_text_without_actual_tool_activity_stays_unknown(db, target):
    official(db, target)
    target[2][0] = lambda request: stream()
    result = probe(target, "native_tools")
    assert result["status"] == "unknown" and result["request_count"] == 1


def file_handler(target, after_upload=None, fail_delete=False):
    tokens = []
    def handle(request):
        if request.url.path == "/files" and request.method == "POST":
            raw = request.content.decode()
            assert 'name="purpose"' in raw and 'name="expires_after[seconds]"\r\n\r\n3600' in raw
            tokens.append(re.search(r"Token: (synthetic-[a-f0-9]+)", raw).group(1))
            if after_upload: after_upload()
            return httpx.Response(200, json={"id": "file-capability-synthetic", "object": "file", "bytes": 80,
                "created_at": 1, "expires_at": 3601, "filename": "capability-test.md", "purpose": "user_data"})
        if request.method == "DELETE":
            return httpx.Response(503) if fail_delete else httpx.Response(200, json={"id": "file-capability-synthetic", "deleted": True})
        body = json.loads(request.content)
        assert body["model"] == "Exact-Model-Case" and tokens[-1] not in json.dumps(body)
        assert body["messages"][0]["content"][1] == {"type": "file", "file_id": "file-capability-synthetic"}
        return response(tokens[-1])
    target[2][0] = handle


def test_files_upload_understand_random_token_and_delete_with_three_metered_calls(db, target):
    official(db, target)
    file_handler(target)
    result = probe(target, "files")
    assert result["status"] == "supported" and result["request_count"] == 3
    assert result["remote_file"]["state"] == "deleted"
    assert [r.method for r in target[1]] == ["POST", "POST", "DELETE"]
    assert not probes.preview(target[0])["cleanup_operations"]


def test_files_cancel_after_upload_stops_chat_and_preserves_manual_cleanup(db, target, monkeypatch):
    official(db, target)
    did = "capability-" + "1" * 32
    record = probes._record_remote
    def cancel_after_receipt(client, value):
        record(client, value)
        if value.get("state") == "available": probes.cancel(target[0], did)
    monkeypatch.setattr(probes, "_record_remote", cancel_after_receipt)
    file_handler(target)
    result = probe(target, "files", did)
    assert result["status"] == "unknown" and result["failure_kind"] == "cancelled"
    assert result["request_count"] == 1 and len(target[1]) == 1
    assert result["remote_file"]["state"] == "cleanup_pending"
    pending = probes.preview(target[0])["cleanup_operations"]
    assert pending[0]["diagnostic_id"] == did
    cleanup = probes.cleanup(target[0], did)
    assert cleanup["remote_file"]["state"] == "deleted" and cleanup["request_count"] == 1
    assert not probes.preview(target[0])["cleanup_operations"]
    assert probes.cleanup(target[0], did)["request_count"] == 0


def test_cancel_during_upload_keeps_unknown_remote_identity_and_never_guesses_delete(db, target):
    official(db, target)
    did = "capability-" + "7" * 32
    file_handler(target, after_upload=lambda: probes.cancel(target[0], did))
    result = probe(target, "files", did)
    assert result["failure_kind"] == "cancelled" and result["remote_file"]["state"] == "upload_pending"
    assert not result["remote_file"].get("id") and result["request_count"] == 1
    with pytest.raises(profiles.ModelConfigurationError): probes.cleanup(target[0], did)
    assert len(target[1]) == 1


def test_file_cleanup_failure_does_not_claim_remote_deletion(db, target):
    official(db, target)
    file_handler(target, fail_delete=True)
    result = probe(target, "files")
    assert result["status"] == "supported" and result["remote_file"]["state"] == "cleanup_pending"
    assert probes.preview(target[0])["cleanup_operations"]


def test_cancel_before_request_starts_is_zero_cost_and_never_overwrites_known_result(db, target):
    pid = target[0]
    did = "capability-" + "2" * 32
    row = db.get(ModelProfile, pid)
    row.capabilities = {**profiles.unknown_capabilities(), "tools": "supported"}; db.commit()
    probes.cancel(pid, did)
    result = probe(target, "tools", did)
    assert result["failure_kind"] == "cancelled" and not target[1]
    assert probes.preview(pid)["capabilities"]["tools"] == "supported"


def test_cancel_between_tool_rounds_never_dispatches_next_call(db, target):
    did = "capability-" + "3" * 32
    def handle(request):
        probes.cancel(target[0], did)
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "first", "type": "function", "function": {"name": "capability_echo", "arguments": '{"stage":1}'}}]}, "finish_reason": "tool_calls"}]})
    target[2][0] = handle
    result = probe(target, "tools", did)
    assert result["failure_kind"] == "cancelled" and result["request_count"] == 1
    assert len(target[1]) == 1 and probes.preview(target[0])["capabilities"]["tools"] == "unknown"


def test_cancel_while_waiting_for_real_shared_queue_dispatches_zero_http(db, target, monkeypatch):
    from app import request_resources as resources
    monkeypatch.setattr(resources, "scheduler", resources.FairRequestScheduler(default_limit=1))
    did, results = "capability-" + "4" * 32, []
    from hashlib import sha256
    with bind_runtime_config({"run_id": "synthetic-queue-holder"}):
        with resources.request_admission(provider="matrix.example.test", endpoint="https://matrix.example.test/v1/chat/completions",
                credential_fingerprint=sha256(b"synthetic-matrix-key").hexdigest()):
            thread = threading.Thread(target=lambda: results.append(probe(target, "text", did)))
            thread.start()
            deadline = time.monotonic() + 8
            while not resources.scheduler.snapshot(did)["items"]:
                assert time.monotonic() < deadline
                time.sleep(.03)
            probes.cancel(target[0], did)
            thread.join(timeout=8)
            assert not thread.is_alive()
    assert results[0]["failure_kind"] == "cancelled" and not target[1]
    assert results[0]["request_count"] == 0 and not resources.scheduler.snapshot()["items"]


def test_same_diagnostic_identity_cannot_repeat_paid_requests(db, target):
    did = "capability-" + "5" * 32
    assert probe(target, "text", did)["status"] == "supported"
    with pytest.raises(profiles.ModelConfigurationError): probe(target, "text", did)
    assert len(target[1]) == 1


def runtime(db, target, **capabilities):
    row = db.get(ModelProfile, target[0])
    row.capabilities = {**profiles.unknown_capabilities(), **capabilities}
    db.commit()
    return profiles.capture_run_config(db, None, selected_profile_id=row.id)


def test_known_nonstreaming_routes_same_model_and_one_actual_metered_chat(db, target):
    binding = runtime(db, target, streaming="unsupported")
    events = list(APIClient(runtime_config=binding, synthetic=True).stream_chat([{"role": "user", "content": "hello"}]))
    assert events[0] == ("delta", "answer", "OK") and events[-1][0] == "final"
    assert len(target[1]) == 1 and "stream" not in json.loads(target[1][0].content)
    assert json.loads(target[1][0].content)["model"] == "Exact-Model-Case"
    assert binding["capability_notices"] and binding["profile_version"] == 1


@pytest.mark.parametrize("feature,messages,options", [
    ("vision", [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,synthetic"}}]}], {}),
    ("files", [{"role": "user", "content": [{"type": "file", "file_id": "file-synthetic"}]}], {}),
    ("tools", [{"role": "user", "content": "hello"}], {"tools": [{"name": "synthetic", "parameters": {"type": "object"}}]}),
])
def test_known_unsupported_input_is_never_silently_dropped_or_sent(db, target, feature, messages, options):
    binding = runtime(db, target, **{feature: "unsupported"})
    with pytest.raises(CapabilityUnavailable) as failure:
        APIClient(runtime_config=binding).chat(messages, **options)
    assert failure.value.capability == feature and not target[1]


def test_unknown_feature_is_attempted_and_profile_edit_invalidates_all_diagnostics(db, target):
    value = probe(target, "text")
    assert value["status"] == "supported"
    row = db.get(ModelProfile, target[0]); db.refresh(row)
    profiles.save_profile(db, {"name": "renamed"}, row); db.commit()
    preview = probes.preview(row.id)
    assert not preview["results"] and all(v == "unknown" for v in preview["capabilities"].values())
    target[2][0] = lambda request: stream()
    binding = profiles.capture_run_config(db, None, selected_profile_id=row.id)
    assert list(APIClient(runtime_config=binding).stream_chat([{"role": "user", "content": "hello"}]))[-1][0] == "final"
    assert json.loads(target[1][-1].content)["stream"] is True


def test_api_cancel_tombstone_validates_operation_and_profile_scope(db, target):
    with TestClient(app) as client:
        did = "capability-" + "6" * 32
        assert client.post(f"/api/model-profiles/{target[0]}/capabilities/{did}/cancel").status_code == 200
        result = client.post(f"/api/model-profiles/{target[0]}/capabilities", json={
            "capability": "usage", "expected_config": probes.preview(target[0])["expected_config"], "diagnostic_id": did})
        assert result.status_code == 200 and result.json()["failure_kind"] == "cancelled"
        assert client.post(f"/api/model-profiles/different/capabilities/{did}/cancel").status_code == 404
    assert not target[1]


def test_main_executor_keeps_hello_usable_without_advertising_unsupported_tools(db, target, monkeypatch):
    from app.agent import loop
    from app.models import Conversation
    from app.runtime_context import current_runtime_config
    db.add(Conversation(id="matrix-conversation")); db.commit()
    binding = runtime(db, target, tools="unsupported", streaming="unsupported")
    monkeypatch.setattr(loop, "get_llm", lambda: APIClient(runtime_config=current_runtime_config(), synthetic=True))
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    events = list(loop.run_agent(db, "你好", conversation_id="matrix-conversation", runtime_config=binding))
    assert any(kind == "answer" for kind, _value in events), events
    assert any(value.get("reason") == "model_capability_route" for _kind, value in events)
    assert target[1] and not any(kind == "tool_call" for kind, _value in events)
    assert any(any("不支持本地工具" in str(m.get("content")) for m in json.loads(r.content)["messages"]) for r in target[1])
    for request in target[1]:
        body = json.loads(request.content)
        assert body["model"] == "Exact-Model-Case" and not body.get("tools")


def test_child_retains_exact_parent_model_and_explicit_text_only_policy(db, target, monkeypatch):
    from app.agent import subagent
    from app.goal_ledger import ensure_run_goal
    from app.models import Conversation
    from app.runtime_models import RuntimeRun, RunAttempt
    db.add(Conversation(id="matrix-parent")); db.commit()
    binding = runtime(db, target, tools="unsupported")
    goal = ensure_run_goal(db, "matrix-parent-run", "matrix-parent", "Synthetic child observation")
    db.add(RuntimeRun(id="matrix-parent-run", kind="foreground", conversation_id="matrix-parent", goal_id=goal["id"], current_attempt_id="matrix-attempt"))
    db.add(RunAttempt(id="matrix-attempt", run_id="matrix-parent-run", number=1, writer_token="matrix-writer", status="running"))
    db.commit()
    binding.update(run_id="matrix-parent-run", conversation_id="matrix-parent", goal_id=goal["id"], attempt_id="matrix-attempt")
    with bind_runtime_config(binding):
        result = subagent.run_subagents(db, APIClient(runtime_config=binding, synthetic=True), [{"topic": "Synthetic text observation"}])
    assert result[0]["outcome"] == "partial" and len(target[1]) == 1
    body = json.loads(target[1][0].content)
    assert not body.get("tools") and any("不支持本地工具" in str(m.get("content")) for m in body["messages"])


def test_shared_native_service_does_not_inherit_parent_profile_capability_rejections(db, target):
    from app.providers.deepseek_research import ResearchClient
    target[2][0] = lambda request: stream("deepseek_web_search")
    # A service's separate, authorized fixed model is not the parent model. Its
    # runtime still shares the task ledger/budget, never the parent's feature claim.
    with bind_runtime_config({"capabilities": {"native_tools": "unsupported", "streaming": "unsupported"}}):
        result = list(ResearchClient("synthetic-service-key", authorization_check=lambda: True, synthetic=True).stream_chat([
            {"role": "user", "content": "Synthetic native research test"}]))
    assert result[-1][0] == "final" and len(target[1]) == 1
    body = json.loads(target[1][0].content)
    assert body["stream"] and body["model"] == "deepseek-flash" and body["tools"]


def test_model_snapshot_keeps_time_endpoint_id_and_version_of_independent_diagnosis(db, target):
    result = probe(target, "text")
    binding = profiles.capture_run_config(db, None, selected_profile_id=target[0])
    evidence = binding["capability_observations"]["text"]
    assert evidence["diagnostic_id"] == result["diagnostic_id"] and evidence["tested_at"] == result["tested_at"]
    assert evidence["endpoint"] == result["endpoint"] and evidence["model_id"] == "Exact-Model-Case"
    assert "synthetic-matrix-key" not in json.dumps(profiles.public_runtime_config(binding))


def test_restart_never_repeats_diagnosis_and_keeps_known_remote_cleanup(db, target, monkeypatch):
    official(db, target)
    did = "capability-" + "8" * 32
    preview = probes.preview(target[0])
    with SessionLocal() as reader:
        values = probes._values(reader, target[0])
    probes._start(target[0], "files", did)
    client = probes._Client(values, preview["expected_config"], did)
    probes._record_remote(client, {"id": "file-capability-synthetic", "state": "available", "requested_ttl_seconds": 3600})
    monkeypatch.setattr(probes, "_OWNER", "synthetic-new-process-owner")
    pending = probes.preview(target[0])["cleanup_operations"]
    assert pending[0]["diagnostic_id"] == did and pending[0]["remote_file"]["state"] == "cleanup_pending"
    assert not target[1]
    with SessionLocal() as reader:
        assert reader.get(DesktopState, probes._operation_key(did)).value["interrupted"]
    file_handler(target)
    assert probes.cleanup(target[0], did)["remote_file"]["state"] == "deleted"
    assert [r.method for r in target[1]] == ["DELETE"]


@pytest.mark.parametrize("status,kind", [(401, "authentication"), (403, "authentication"), (402, "balance"), (429, "rate_limit")])
def test_file_service_failure_does_not_become_unsupported_or_repeat_upload(db, target, status, kind):
    official(db, target)
    target[2][0] = lambda request: httpx.Response(status, json={"error": {"message": "PRIVATE synthetic-matrix-key"}})
    result = probe(target, "files")
    assert result["status"] == "unknown" and result["failure_kind"] == kind
    assert result["request_count"] == 1 and len(target[1]) == 1
    assert "PRIVATE" not in json.dumps(result) and "synthetic-matrix-key" not in json.dumps(result)


def test_native_search_alone_does_not_verify_reading_links(db, target):
    official(db, target)
    def handle(request):
        name = json.loads(request.content)["tools"][0]["type"]
        return stream(name if name == "deepseek_web_search" else None)
    target[2][0] = handle
    result = probe(target, "native_tools")
    assert result["status"] == "unknown" and result["request_count"] == 2
    assert result["observations"]["confirmed_native_tools"] == ["deepseek_web_search"]


@pytest.mark.parametrize("streaming", [False, True])
def test_text_only_route_cannot_execute_an_unadvertised_returned_tool(db, target, streaming):
    binding = runtime(db, target, tools="unsupported")
    message = {"role": "assistant", "content": "", "tool_calls": [{"id": "forged", "type": "function", "function": {
        "name": "write_project_file", "arguments": '{"path":"must-not-write.txt","content":"denied"}'}}]}
    if streaming:
        chunk = {"choices": [{"index": 0, "delta": message, "finish_reason": "tool_calls"}]}
        target[2][0] = lambda request: httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    else:
        target[2][0] = lambda request: httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]})
    model = APIClient(runtime_config=binding, synthetic=True)
    with pytest.raises(CapabilityUnavailable):
        list(model.stream_chat([{"role": "user", "content": "hello"}])) if streaming else model.chat([{"role": "user", "content": "hello"}])
    assert len(target[1]) == 1


def test_known_unsupported_json_and_unknown_schema_fail_before_http(db, target):
    binding = runtime(db, target, structured="unsupported")
    with pytest.raises(CapabilityUnavailable):
        APIClient(runtime_config=binding, provider_options={"response_format": {"type": "json_object"}}).chat([{"role": "user", "content": "JSON"}])
    from app.providers.chat import ProviderContractError
    with pytest.raises(ProviderContractError):
        APIClient(provider_options={"response_format": {"type": "json_schema", "json_schema": {}}})._build_request(
            __import__('app.providers.chat', fromlist=['adapter_for']).adapter_for("https://matrix.example.test/v1", "Exact-Model-Case", "synthetic-key"),
            "synthetic-key", [{"role": "user", "content": "JSON"}])
    assert not target[1]
