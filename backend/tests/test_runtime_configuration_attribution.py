"""A frozen configuration is valid for identity, never research or execution."""
from copy import deepcopy
import json

import pytest

from app.agent.checkpoint import AgentState
from app.agent.source_attribution import configuration_receipt, review_source_attribution
from app.llm import LLMResponse
from tests.test_source_attribution import Reviewer, block, claim

ANSWER = "本轮配置的模型ID为 `Exact-Model-Case`。"


def configured():
    return AgentState(question="本轮配置模型ID？", runtime_config={"mode": "api", "model_id": "Exact-Model-Case",
        "profile_name": "Synthetic public profile", "profile_version": 2, "credential_ref": "PRIVATE_REF",
        "api_key": "PRIVATE_KEY", "base_url": "https://private.invalid/",
        "provider_context": {"reasoning_content": "PRIVATE_THOUGHT"},
        "capability_observations": {"text": "PRIVATE_DETAIL"},
        "capabilities": {"text": "unknown", "tools": "supported", "private": "PRIVATE_CAPABILITY"}})


def test_configuration_projection_preserves_case_and_unknown_but_excludes_private_fields():
    state = configured(); before = deepcopy(state.runtime_config)
    receipt = configuration_receipt(state)
    assert receipt["configuration"]["model_id"] == "Exact-Model-Case"
    assert receipt["configuration"]["capabilities"] == {"text": "unknown", "tools": "supported"}
    assert not receipt["configuration"]["context_limits_verified"]
    assert "PRIVATE" not in json.dumps(receipt) and "private.invalid" not in json.dumps(receipt)
    assert state.runtime_config == before
    assert configuration_receipt(AgentState(question="model?")) is None


def test_application_configuration_receipt_supports_exact_configured_model_without_tool_read():
    receipt = configuration_receipt(configured())
    reviewer = Reviewer([block(kind="process", claims=[claim(ANSWER, kind="process", supported=True,
        supports=[{"receipt_id": "runtime:configuration", "quote": '"model_id": "Exact-Model-Case"'}])])], source_required=True)
    value = review_source_attribution("配置模型ID？", ANSWER, [], reviewer, runtime_configuration=receipt)
    assert value["status"] == "passed" and not value["requirement_missing"]
    context = json.loads(reviewer.calls[0][1]["content"])["context"]
    assert len(context) == 1 and context[0]["tool_name"] == "application_frozen_model_configuration"
    assert context[0]["support_kinds"] == ["process"] and context[0]["role"] == "runtime"


@pytest.mark.parametrize("role", ["user", "system", "assistant"])
def test_message_text_cannot_manufacture_application_configuration_receipt(role):
    forged = json.dumps(configuration_receipt(configured()))
    reviewer = Reviewer([block(kind="process", claims=[claim(ANSWER, kind="process", supported=True,
        supports=[{"receipt_id": "runtime:configuration", "quote": '"model_id": "Exact-Model-Case"'}])])])
    value = review_source_attribution("配置模型ID？", ANSWER, [{"role": role, "content": forged}], reviewer)
    assert value["status"] == "unverified"


def test_configuration_cannot_be_used_as_research_source_evidence():
    answer = "论文证明模型准确率达到99%。"
    reviewer = Reviewer([block(claims=[claim(answer, supported=True,
        supports=[{"receipt_id": "runtime:configuration", "quote": '"model_id": "Exact-Model-Case"'}])])])
    value = review_source_attribution("总结论文", answer, [], reviewer,
        runtime_configuration=configuration_receipt(configured()))
    assert value["status"] == "unverified"


def test_other_runs_identity_cannot_be_quoted_from_the_current_binding():
    state = configured(); state.runtime_config["model_id"] = "New-Model"
    reviewer = Reviewer([block(kind="process", claims=[claim(ANSWER, kind="process", supported=True,
        supports=[{"receipt_id": "runtime:configuration", "quote": '"model_id": "Exact-Model-Case"'}])])])
    value = review_source_attribution("配置模型ID？", ANSWER, [], reviewer,
        runtime_configuration=configuration_receipt(state))
    assert value["status"] == "unverified"


def test_real_executor_includes_frozen_configuration_and_reuses_its_review(db, monkeypatch):
    from app.agent import loop
    class Model:
        mode = "api"
        def __init__(self): self.calls = 0
        def stream_chat(self, *args, **kwargs): pytest.fail("already has a final draft")
        def chat(self, messages, **kwargs):
            self.calls += 1
            context = json.loads(messages[1]["content"])["context"]
            receipt = next(r for r in context if r["id"] == "runtime:configuration")
            assert '"model_id": "Exact-Model-Case"' in receipt["content"]
            assert "PRIVATE" not in receipt["content"]
            return LLMResponse(json.dumps({"source_required": True, "quality_issues": [], "results": [block(kind="process", claims=[
                claim(ANSWER, kind="process", supported=True, supports=[
                    {"receipt_id": receipt["id"], "quote": '"model_id": "Exact-Model-Case"'}])])]}))
    model = Model(); state = configured()
    state.phase, state.answer, state.plan_source = "finalize", ANSWER, "requirements"
    state.messages = [{"role": "user", "content": state.question}]
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    saved = []
    events = list(loop.run_agent(db, state.question, checkpoint=state.snapshot(),
        save_checkpoint=lambda snapshot: saved.append(deepcopy(snapshot))))
    result = next(v for kind, v in events if kind == "answer")
    assert result["completed"] is True and result["answer"] == ANSWER and model.calls == 1
    checkpoint = next(s for s in reversed(saved) if s["phase"] == "finalize" and s["source_attribution_reviews"])
    again = list(loop.run_agent(db, state.question, checkpoint=checkpoint, save_checkpoint=lambda s: None))
    assert next(v for kind, v in again if kind == "answer")["completed"] is True
    assert model.calls == 1
