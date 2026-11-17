"""Explicit capability probes share real adapters with synthetic HTTP."""
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app import model_capabilities as probes, model_profiles
from app.db import SessionLocal
from app.models import ModelCredential, ModelProfile


@pytest.fixture
def target(db, monkeypatch):
    row = model_profiles.save_profile(db, {"name": "Capability Test", "model_id": "Exact-Case-Model",
        "base_url": "https://capability.example.test/v1", "api_key": "synthetic-capability-secret"})
    db.commit()
    profile_id = row.id
    original_init = probes._Client.__init__
    def synthetic(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._synthetic = True
    monkeypatch.setattr(probes._Client, "__init__", synthetic)
    calls, real = [], httpx.Client

    def normal(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"},
                                                       "finish_reason": "stop"}]})
    handler = [normal]
    def dispatch(request):
        calls.append(json.loads(request.content))
        assert json.loads(request.content)["model"] == "Exact-Case-Model"
        return handler[0](request)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real(*args, transport=httpx.MockTransport(dispatch), **kwargs))
    return profile_id, calls, handler


def test_preview_is_local_and_text_probe_updates_only_text(db, target):
    profile_id, calls, _ = target
    preview = probes.preview(profile_id)
    assert preview["remote_requests"] == 0 and calls == []
    assert db.query(ModelCredential).count() == 1
    assert "synthetic-capability-secret" not in json.dumps(preview)
    result = probes.run(profile_id, "text", preview["expected_config"])
    assert result["status"] == "supported" and result["request_count"] == 1
    refreshed = probes.preview(profile_id)
    assert refreshed["capabilities"] == {**model_profiles.unknown_capabilities(), "text": "supported"}
    assert refreshed["results"]["text"]["diagnostic_id"] == result["diagnostic_id"]
    assert db.query(ModelCredential).count() == 1


def test_tools_probe_executes_three_ordered_rounds_and_keeps_reasoning(db, target):
    profile_id, calls, handler = target
    def reply(request):
        body = json.loads(request.content)
        stage = len(calls)
        for index, message in enumerate([m for m in body["messages"] if m["role"] == "assistant"], 1):
            assert message["reasoning_content"] == f"Synthetic stage {index}"
            assert "_provider" not in message
        message = {"role": "assistant", "content": "OK" if stage == 4 else "",
                   "reasoning_content": f"Synthetic stage {stage}"}
        if stage < 4:
            message["tool_calls"] = [{"id": f"stage-{stage}", "type": "function", "function": {
                "name": "capability_echo", "arguments": json.dumps({"stage": stage})}}]
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop" if stage == 4 else "tool_calls"}]})
    handler[0] = reply
    result = probes.run(profile_id, "tools", probes.preview(profile_id)["expected_config"])
    assert result["status"] == "supported" and result["request_count"] == 4
    assert len(calls) == 4
    assert probes.preview(profile_id)["capabilities"]["text"] == "unknown"


def test_streaming_requires_real_delta_and_complete_final(db, target):
    profile_id, _, handler = target
    body = "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n"
    handler[0] = lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    result = probes.run(profile_id, "streaming", probes.preview(profile_id)["expected_config"])
    assert result["status"] == "supported" and result["request_count"] == 1
    handler[0] = lambda request: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                                              headers={"content-type": "text/event-stream"})
    failed = probes.run(profile_id, "streaming", probes.preview(profile_id)["expected_config"])
    assert failed["status"] == "unknown" and failed["failure_kind"] == "protocol"


def test_image_probe_transmits_only_synthetic_red_square_and_verifies_meaning(db, target):
    profile_id, _, handler = target
    def reply(request):
        parts = json.loads(request.content)["messages"][0]["content"]
        png = base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(png)) as image:
            assert image.size == (32, 32) and image.getpixel((0, 0)) == (255, 0, 0)
        return httpx.Response(200, json={"choices": [{"message": {"content": "red"}, "finish_reason": "stop"}]})
    handler[0] = reply
    assert probes.run(profile_id, "vision", probes.preview(profile_id)["expected_config"])["status"] == "supported"
    handler[0] = lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "blue"}}]})
    assert probes.run(profile_id, "vision", probes.preview(profile_id)["expected_config"])["status"] == "unknown"


def test_reasoning_probe_does_not_mark_answer_only_as_reasoning_support(db, target):
    profile_id, _, handler = target
    handler[0] = lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "391"}}]})
    assert probes.run(profile_id, "reasoning", probes.preview(profile_id)["expected_config"])["status"] == "unknown"
    handler[0] = lambda request: httpx.Response(200, json={"choices": [{"message": {
        "content": "391", "reasoning_content": "Synthetic calculation"}}]})
    assert probes.run(profile_id, "reasoning", probes.preview(profile_id)["expected_config"])["status"] == "supported"


@pytest.mark.parametrize("code,expected", [(401, "authentication"), (403, "authentication"), (402, "balance"), (429, "rate_limit"), (400, "provider"), (503, "provider")])
def test_failures_do_not_invent_unsupported_capability_or_expose_provider_text(db, target, code, expected):
    profile_id, _, handler = target
    handler[0] = lambda request: httpx.Response(code, json={"error": {"message": "PRIVATE synthetic-capability-secret", "code": "unknown"}})
    result = probes.run(profile_id, "text", probes.preview(profile_id)["expected_config"])
    assert result["status"] == "unknown" and result["failure_kind"] == expected
    assert "PRIVATE" not in json.dumps(result) and "synthetic-capability-secret" not in json.dumps(result)


def test_explicit_capability_rejection_is_distinct(db, target):
    profile_id, _, handler = target
    handler[0] = lambda request: httpx.Response(400, json={"error": {"code": "capability_not_supported", "param": "vision"}})
    result = probes.run(profile_id, "vision", probes.preview(profile_id)["expected_config"])
    assert result["status"] == "unsupported" and result["failure_kind"] == "capability"


def test_changed_preview_sends_zero_requests(db, target):
    profile_id, calls, _ = target
    token = probes.preview(profile_id)["expected_config"]
    row = db.get(ModelProfile, profile_id)
    row.version += 1
    row.model_id = "Changed-Model"
    db.commit()
    with pytest.raises(probes.ConfigurationChanged):
        probes.run(profile_id, "text", token)
    assert calls == []


def test_revocation_between_tool_rounds_stops_new_calls_and_does_not_save_success(db, target):
    profile_id, calls, handler = target
    def revoke(request):
        with SessionLocal() as other:
            row = other.get(ModelProfile, profile_id)
            row.enabled, row.version = 0, row.version + 1
            other.commit()
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "first", "type": "function", "function": {"name": "capability_echo", "arguments": '{"stage":1}'}}]},
            "finish_reason": "tool_calls"}]})
    handler[0] = revoke
    result = probes.run(profile_id, "tools", probes.preview(profile_id)["expected_config"])
    assert result["status"] == "unknown" and result["failure_kind"] == "configuration_changed"
    assert len(calls) == 1
    db.expire_all()
    assert db.get(ModelProfile, profile_id).capabilities["tools"] == "unknown"
