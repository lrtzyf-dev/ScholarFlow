"""Auxiliary-review request payloads through APIClient; no network or real key."""
from copy import deepcopy
import json

import httpx
import pytest

from app import config, llm
from app.agent.audit import audit_answer, review_citation_scope
from app.agent.model_review import model_review_identity, review_chat_options
from app.rag.citation import citation_gate
from tests.test_citation_review_diagnostics import provider  # noqa: F401 -- isolated MockTransport fixture
from tests.test_citation_scope_receipts import FACT, EVIDENCE


def response(content):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}})


@pytest.mark.parametrize("stage", ["audit", "scope"])
@pytest.mark.parametrize("effort", ["", "none", "low", "high", "max"])
def test_official_deepseek_review_payload_uses_low_only_when_not_explicit(db, provider, monkeypatch, stage, effort):
    responses, requests = provider
    monkeypatch.setattr(config.settings, "llm_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(config.settings, "llm_model", "deepseek-flash")
    monkeypatch.setattr(config.settings, "reasoning_effort", effort)
    if stage == "audit":
        responses.append(response(json.dumps({"results": [{"index": 0, "supported": True,
            "reason": "Synthetic source verdict", "support_kind": "source", "process_scope": "none", "supports": []}]})))
        assert audit_answer(db, FACT, EVIDENCE, llm=llm.APIClient(), messages=[])["passed"]
    else:
        text = FACT + "\n\n该材料报告实验准确率达到百分之九十九。"
        gate = citation_gate(text, ["e1"])
        assert len(gate["unsupported"]) == 1
        responses.append(response(json.dumps({"results": [{"index": 0, "requires_citation": True,
            "reason": "Synthetic source requirement", "kind": "source", "process_scope": "none", "supports": []}]})))
        reviewed = review_citation_scope("比较研究", text, EVIDENCE, gate, llm.APIClient(), messages=[])
        assert reviewed["scoped_review"]["verified"] and not reviewed["passed"]
    body = json.loads(requests[0].content)
    assert body["model"] == "deepseek-flash" and body["max_tokens"] == 8192
    if effort == "none":
        assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == (effort or "low") and "thinking" not in body
    assert len(requests) == 1


@pytest.mark.parametrize("base", ["https://compatible.example.test/v1", "https://api.deepseek.com.evil.test/v1",
    "https://api.deepseek.com:8443/v1", "https://api.deepseek.com/private", "http://api.deepseek.com/v1"])
def test_same_model_name_on_other_endpoint_does_not_receive_deepseek_review_options(db, provider, monkeypatch, base):
    responses, requests = provider
    monkeypatch.setattr(config.settings, "llm_base_url", base)
    monkeypatch.setattr(config.settings, "llm_model", "deepseek-flash")
    monkeypatch.setattr(config.settings, "reasoning_effort", "")
    responses.append(response('{"results":[{"index":0,"supported":true}]}'))
    assert review_chat_options(llm.APIClient()) == {}
    assert audit_answer(db, FACT, EVIDENCE, llm=llm.APIClient())["passed"]
    body = json.loads(requests[0].content)
    assert "reasoning_effort" not in body and "thinking" not in body


def test_unknown_custom_llm_is_not_treated_as_official_by_model_name():
    class Custom:
        _runtime_config = {"base_url": "https://api.deepseek.com/v1", "model_id": "deepseek-flash", "reasoning_effort": ""}
    assert review_chat_options(Custom()) == {}


def test_frozen_binding_effort_and_reasoning_capability_are_respected(monkeypatch):
    monkeypatch.setattr(config.settings, "reasoning_effort", "high")
    binding = {"version": 1, "mode": "api", "base_url": "https://api.deepseek.com/v1",
        "model_id": "deepseek-flash", "credential_ref": "SYNTHETIC_PRIVATE_REFERENCE", "reasoning_effort": ""}
    assert review_chat_options(llm.APIClient(runtime_config=binding)) == {"reasoning_effort": "low"}
    for effort in ("none", "low", "high", "max"):
        assert review_chat_options(llm.APIClient(runtime_config={**binding, "reasoning_effort": effort})) == {}
    assert review_chat_options(llm.APIClient(runtime_config={**binding, "capabilities": {"reasoning": "unsupported"}})) == {}


def test_review_identity_excludes_runtime_controls_but_covers_model_and_effective_options():
    binding = {"version": 1, "mode": "api", "base_url": "https://api.deepseek.com/v1",
        "model_id": "deepseek-flash", "credential_ref": "SYNTHETIC_PRIVATE_REFERENCE", "reasoning_effort": ""}
    model = llm.APIClient(runtime_config=binding)
    before = model_review_identity(model)
    model._runtime_config.update(run_id="another-run", attempt_id="another-attempt", resource_budget_id="larger",
        resource_policy={"revision": 2}, quota_binding={"revision": 3}, approval_mode="full_access", policy_revision=9)
    assert model_review_identity(model) == before
    assert before["review_chat_options"] == {"reasoning_effort": "low"}
    assert "PRIVATE" not in json.dumps(before)
    for field, value in (("model_id", "Other-Model"), ("base_url", "https://other.example.test/v1"),
            ("credential_ref", "different-reference"), ("reasoning_effort", "high"), ("profile_version", 9),
            ("capabilities", {"tools": "unsupported"})):
        changed = deepcopy(binding); changed[field] = value
        assert model_review_identity(llm.APIClient(runtime_config=changed)) != before


@pytest.mark.parametrize("base", ["https://api.deepseek.com/v1", "https://compatible.example.test/v1"])
def test_explicit_max_is_normalized_and_forwarded_exactly_without_provider_guessing(db, provider, monkeypatch, base):
    from app.model_connection import normalize_effort
    responses, requests = provider
    monkeypatch.setattr(config.settings, "llm_base_url", base)
    monkeypatch.setattr(config.settings, "reasoning_effort", normalize_effort(" MAX "))
    responses.append(response('{"results":[{"index":0,"supported":true}]}'))
    assert audit_answer(db, FACT, EVIDENCE, llm=llm.APIClient())["passed"]
    body = json.loads(requests[0].content)
    assert body["reasoning_effort"] == "max" and "thinking" not in body
