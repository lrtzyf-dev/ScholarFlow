"""Annotations must survive recovery and still undergo ordinary source audit."""
from copy import deepcopy
import json

import pytest

from app.agent import loop, audit
from app.agent.checkpoint import AgentState
from app.agent.execution import _citation_marker_annotation
from app.llm import LLMResponse
from app.rag.citation import citation_gate
from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint


HEADER = "**附件编码方法的实际记录**"
FACT = "模型采用8个注意力头完成编码处理。[^e1]"


def scope_gate(answer, evidence):
    gate = citation_gate(answer, [e["id"] for e in evidence])
    gate["scoped_review"] = {"verified": True, "protocol": "citation_scope_with_process_receipts_v1",
        "original_total_claims": gate["total_claims"], "original_marked_claims": gate["marked_claims"],
        "original_unsupported_count": len(gate["unsupported"]), "decisions": [
            {"index": i, "text": text, "kind": "source", "requires_citation": True}
            for i, text in enumerate(gate["unsupported"])]}
    return gate


class Model:
    mode = "api"

    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        if "candidates" in messages[-1]["content"]:
            return LLMResponse(json.dumps({"edits": [{"candidate_index": 0,
                "text": HEADER, "evidence_ids": ["e1"]}]}))
        return LLMResponse(HEADER + "[^e1]\n\n" + FACT)


def bare_state():
    return AgentState(question="按附件解释方法", answer=HEADER + "\n\n" + FACT,
        evidence=[{"id": "e1", "quote": "模型采用8个注意力头完成编码处理。"}])


def test_annotation_result_committed_before_cancellation_is_reused_without_new_request():
    state, model, durable = bare_state(), Model(), []
    gate = scope_gate(state.answer, state.evidence)
    def check():
        if any(receipt.get("changed") for receipt in state.source_attribution_reviews.values()):
            raise RuntimeError("synthetic cancellation after receipt commit")
    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        _citation_marker_annotation(state, gate, model, persist=lambda: durable.append(state.snapshot()),
            before_review=check, on_usage=lambda usage: None)
    assert len(model.calls) == 1 and state.answer == HEADER + "\n\n" + FACT
    restored = AgentState.model_validate(durable[-1])
    result = _citation_marker_annotation(restored, gate, model, persist=lambda: None,
        before_review=lambda: None, on_usage=lambda usage: None)
    assert result["changed"] and len(model.calls) == 1
    assert restored.answer == HEADER + "[^e1]\n\n" + FACT


def test_pending_dispatch_stays_unknown_and_does_not_purchase_again():
    state, model = bare_state(), Model()
    gate = scope_gate(state.answer, state.evidence)
    durable = []
    def fail_after_pending():
        durable.append(state.snapshot())
        raise OSError("synthetic durable-store failure")
    with pytest.raises(OSError):
        _citation_marker_annotation(state, gate, model, persist=fail_after_pending,
            before_review=lambda: None, on_usage=lambda usage: None)
    restored = AgentState.model_validate(durable[-1])
    result = _citation_marker_annotation(restored, gate, model, persist=lambda: None,
        before_review=lambda: None, on_usage=lambda usage: None)
    assert not model.calls and result["error_code"] == "dispatch_outcome_unknown"
    assert restored.answer == state.answer


@pytest.mark.parametrize("post_annotation_false", [False, True])
def test_full_executor_reaudits_annotations_and_retains_a_real_negative_verdict(db, monkeypatch, post_annotation_false):
    cid, evidence = _fixed_evidence(db)
    state = AgentState(phase="finalize", question="按附件解释方法并给出处", conversation_id=cid,
        answer=HEADER + "\n\n" + FACT, evidence=[evidence], evidence_counter=1,
        runtime_config={"mode": "api", "model_id": "synthetic"}, plan_source="requirements",
        messages=[{"role": "user", "content": "按附件解释方法并给出处"}])
    model, reviewed, saved = Model(), [], []
    def review(_db, answer, _evidence, **kwargs):
        reviewed.append(answer)
        failed = post_annotation_false and HEADER + "[^e1]" in answer
        return {"mode": "synthetic", "passed": not failed, "verified": True,
            "unsupported_ratio": 1.0 if failed else 0.0, "known_failed_count": int(failed),
            "unsupported": [{"text": HEADER, "reason": "synthetic source mismatch"}] if failed else []}
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "audit_answer", review)
    monkeypatch.setattr(audit, "review_citation_scope",
        lambda question, answer, evidence, gate, llm, **kw: scope_gate(answer, evidence))
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    events = list(loop.run_agent(db, state.question, conversation_id=cid,
        checkpoint=_current_checkpoint(db, state), save_checkpoint=lambda value: saved.append(value)))
    result = next(value for kind, value in events if kind == "answer")
    assert reviewed[0] == state.answer
    assert reviewed[1] == HEADER + "[^e1]\n\n" + FACT
    if post_annotation_false:
        assert result["refusal"] == 0 and result["completed"] is False
        assert result["audit"]["known_failed_count"] == 1
    else:
        assert result["refusal"] == 0
    assert len(model.calls) == (3 if post_annotation_false else 1)
    if not post_annotation_false:
        assert result["answer"] == reviewed[1] and result["completed"]
    assert any(any(key.startswith("citation_marker/") for key in snapshot["source_attribution_reviews"])
        for snapshot in saved)
