"""Independent recovery/cancellation counterexamples; no provider requests."""
from copy import deepcopy

import pytest

from app.agent.checkpoint import AgentState
from app.agent.execution import _citation_marker_annotation
from tests.test_citation_marker_execution import bare_state, scope_gate, Model, HEADER, FACT


def test_pending_annotation_cannot_be_rebought_after_scope_reason_wording_changes():
    state, model, durable = bare_state(), Model(), []
    gate = scope_gate(state.answer, state.evidence)
    def stop_after_pending():
        durable.append(state.snapshot())
        raise OSError("Synthetic store boundary before HTTP")
    with pytest.raises(OSError):
        _citation_marker_annotation(state, gate, model, persist=stop_after_pending,
            before_review=lambda: None, on_usage=lambda _: None)
    restored = AgentState.model_validate(durable[-1])
    reclassified = deepcopy(gate)
    reclassified["scoped_review"]["decisions"][0]["reason"] = "Same source requirement, another explanation."
    result = _citation_marker_annotation(restored, reclassified, model, persist=lambda: None,
        before_review=lambda: None, on_usage=lambda _: None)
    assert not model.calls and not result.get("changed"), "Volatile scope wording bypassed an unknown paid dispatch"
    assert result["error_code"] == "dispatch_outcome_unknown"


def test_cached_annotation_rechecks_cancellation_before_adopting_saved_answer():
    state, model, durable = bare_state(), Model(), []
    gate = scope_gate(state.answer, state.evidence)
    _citation_marker_annotation(state, gate, model, persist=lambda: durable.append(state.snapshot()),
        before_review=lambda: None, on_usage=lambda _: None)
    # The receipt was saved before adopting its annotation into the answer.
    saved = next(s for s in durable if s["answer"] == HEADER + "\n\n" + FACT
                 and any(v.get("changed") for v in s["source_attribution_reviews"].values()))
    restored = AgentState.model_validate(saved)
    def cancelled():
        raise RuntimeError("Synthetic cancellation on cached adoption")
    with pytest.raises(RuntimeError, match="cached adoption"):
        _citation_marker_annotation(restored, gate, model, persist=lambda: None,
            before_review=cancelled, on_usage=lambda _: None)
    assert len(model.calls) == 1 and restored.answer == saved["answer"]


def test_cached_source_annotation_cannot_survive_a_current_process_exemption():
    state, model, durable = bare_state(), Model(), []
    gate = scope_gate(state.answer, state.evidence)
    _citation_marker_annotation(state, gate, model, persist=lambda: durable.append(state.snapshot()),
        before_review=lambda: None, on_usage=lambda _: None)
    saved = next(s for s in durable if s["answer"] == HEADER + "\n\n" + FACT
                 and any(v.get("changed") for v in s["source_attribution_reviews"].values()))
    restored = AgentState.model_validate(saved)
    current = deepcopy(gate)
    current["unsupported"] = []
    current["scoped_review"]["decisions"][0].update(kind="process", requires_citation=False)
    result = _citation_marker_annotation(restored, current, model, persist=lambda: None,
        before_review=lambda: None, on_usage=lambda _: None)
    assert not result.get("changed") and restored.answer == saved["answer"] and len(model.calls) == 1
