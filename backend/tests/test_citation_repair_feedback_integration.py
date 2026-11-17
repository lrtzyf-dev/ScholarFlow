"""Actual scope reviewer -> existing rewrite feedback; synthetic transport only."""
from copy import deepcopy
import json

import pytest

from app.agent.audit import review_citation_scope
from app.agent.citation_repair_feedback import build_citation_repair_feedback
from app.agent.execution import _citation_repair_messages
from app.llm import LLMResponse
from app.rag.citation import citation_gate


EVIDENCE = [{"id": "e1", "paper_title": "Fabricated pilot", "quote": "12 of 20; no variance was reported.", "page": 1}]
QUESTION = "Explain the limitation using the fabricated pilot."
ANSWER = "样本只有20条，因此不能外推普遍效果。"
REASON = "材料输入与方法推断属于derived，整句须引用相应输入，不能借一般建议豁免。"


class ScopeModel:
    mode = "api"
    def __init__(self, *, raw=None):
        self.calls = []
        self.raw = raw
    def chat(self, history, **kwargs):
        prompt = json.loads(history[-1]["content"])
        self.calls.append(deepcopy(prompt))
        results = [{"index": c["index"], "requires_citation": True, "reason": REASON,
            "kind": "derived", "process_scope": "none", "supports": []}
            for c in prompt["uncited_candidates"]]
        return LLMResponse(self.raw if self.raw is not None else json.dumps({"results": results}))


def scoped(answer=ANSWER, *, model=None, journal=None):
    model = model or ScopeModel()
    ids = [e["id"] for e in EVIDENCE]
    gate = citation_gate(answer, ids)
    return review_citation_scope(QUESTION, answer, EVIDENCE, gate, model, messages=[],
        review_journal=journal, persist=lambda: None, before_review=lambda: None)


def repair_message(answer, gate):
    return _citation_repair_messages([{"role": "user", "content": QUESTION}], answer, gate,
        {"unsupported": []}, ["e1"])[-1]["content"]


def test_verified_scope_reason_reaches_existing_single_rewrite():
    model = ScopeModel()
    gate = scoped(model=model)
    feedback = build_citation_repair_feedback(ANSWER, gate, ["e1"])
    assert feedback["available"] is True
    assert feedback["candidates"][0]["kind"] == "derived"
    message = repair_message(ANSWER, gate)
    assert REASON in message and ANSWER in message
    assert "待核对数据" in message and "第二次自动重写" in message
    assert len(model.calls) == 1 and gate["passed"] is False


def test_cached_scope_is_revalidated_and_bound_without_second_dispatch():
    model, journal = ScopeModel(), {}
    first = scoped(model=model, journal=journal)
    second = scoped(model=model, journal=journal)
    assert first == second and len(model.calls) == 1
    assert build_citation_repair_feedback(ANSWER, second, ["e1"])["available"]


def test_old_scope_with_identical_first_eighty_characters_cannot_guide_changed_answer():
    prefix = "这是一份合成材料中的待审论断" * 8
    old, new = prefix + "原结尾。", prefix + "新结尾。"
    assert old[:80] == new[:80]
    gate = scoped(old)
    assert build_citation_repair_feedback(old, gate, ["e1"])["available"]
    feedback = build_citation_repair_feedback(new, gate, ["e1"])
    assert not feedback["available"] and feedback["candidates"] == []
    assert REASON not in repair_message(new, gate)


@pytest.mark.parametrize("changed", ["reason", "unbound", "unknown"])
def test_changed_or_unknown_scope_does_not_add_trusted_feedback(changed):
    gate = scoped()
    if changed == "reason":
        gate["scoped_review"]["decisions"][0]["reason"] = "Tampered reason"
    elif changed == "unbound":
        gate["scoped_review"].pop("repair_feedback_binding", None)
    else:
        gate["error_code"] = "dispatch_outcome_unknown"
    feedback = build_citation_repair_feedback(ANSWER, gate, ["e1"])
    assert not feedback["available"]
    assert REASON not in repair_message(ANSWER, gate)


def test_invalid_scope_response_never_binds_feedback_or_changes_existing_gate():
    gate = scoped(model=ScopeModel(raw="invalid"))
    assert gate["scope_review_failure"] == "json" and gate["passed"] is False
    assert not build_citation_repair_feedback(ANSWER, gate, ["e1"])["available"]
