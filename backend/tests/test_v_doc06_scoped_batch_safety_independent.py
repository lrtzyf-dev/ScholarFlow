"""Fixed-input scope-bound batch acceptance oracle and production regressions.

The independent test-local oracle permits collateral reduction only for this
exact scope's verified independent exemptions. Production repair and replay
must agree, with an individual checked edit for every substantive reduction.
These are annotation-only checks, not semantic verification.
"""
from collections import Counter
from copy import deepcopy

import pytest

from app.agent import citation_marker_repair as repair
from app.rag.citation import citation_gate
from tests.test_v_doc06_marker_mapping_independent import (inputs, proposal, SyntheticAnnotationModel,
    forbid_external_http)  # noqa: F401 -- no external traffic in design probe


def _oracle_scope_bound_batch(answer, evidence, gate, indices):
    """Experimental acceptance predicate, not a production repair path."""
    ids = [item["id"] for item in evidence]
    baseline = citation_gate(answer, ids)
    eligible = repair._eligible(gate, baseline)
    if (not indices or any(type(i) is not int or i not in eligible for i in indices)
            or len(set(indices)) != len(indices) or baseline["invalid_markers"]):
        return {"accepted": False, "reason": "unbound_or_ineligible_edit"}
    edits = []
    for index in indices:
        item = proposal(answer, gate, index)
        span = repair._complete_span(answer, item["text"])
        if (not span or span[2][:80] != eligible[index]
                or any(span[0] < prior["end"] and prior["start"] < span[1] for prior in edits)):
            return {"accepted": False, "reason": "invalid_original_span"}
        edits.append({"start": span[0], "end": span[1], "insert_at": span[1], "inserted_markers": "[^e1]"})
    repaired = repair._insert(answer, edits)
    after = citation_gate(repaired, ids)
    if after["invalid_markers"] or after["total_claims"] != baseline["total_claims"]:
        return {"accepted": False, "reason": "changed_claim_universe"}
    before_counter, after_counter = Counter(baseline["unsupported"]), Counter(after["unsupported"])
    removed, added = before_counter - after_counter, after_counter - before_counter
    selected = Counter(eligible[index] for index in indices)
    independent = Counter(decision["text"] for decision in gate["scoped_review"]["decisions"]
        if decision["requires_citation"] is False and decision["kind"] == "independent")
    if added or selected - removed:
        return {"accepted": False, "reason": "not_exact_selected_reduction"}
    collateral = removed - selected
    if collateral - independent:
        return {"accepted": False, "reason": "unedited_nonindependent_inheritance",
            "forbidden_removed": list((collateral - independent).elements())}
    return {"accepted": True, "answer": repaired, "selected_removed": list(selected.elements()),
        "independent_collateral": list(collateral.elements()), "still_unsupported": after["unsupported"],
        "annotation_only": True, "semantic_verification": "not_performed"}


def proposed_scope_bound_batch(answer, evidence, gate, indices):
    expected = _oracle_scope_bound_batch(answer, evidence, gate, indices)
    model = SyntheticAnnotationModel([proposal(answer, gate, index) for index in indices])
    actual = repair.repair_citation_markers(answer, evidence, gate, model)
    assert actual["changed"] is expected["accepted"], (expected, actual)
    assert actual["annotation_only"] and actual["semantic_verification"] == "not_performed"
    assert "passed" not in actual and "verified" not in actual
    if expected["accepted"]:
        assert actual["answer"] == expected["answer"]
        replay = repair.reuse_citation_marker_annotation(answer, evidence, gate, actual)
        assert replay["changed"] and replay["answer"] == actual["answer"]
        assert replay["semantic_verification"] == "not_performed"
    else:
        assert actual["answer"] == answer and not actual["edits"]
    return expected


@pytest.mark.parametrize("indices", [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2)])
def test_one_or_two_real_table_rows_cannot_inherit_to_any_unedited_source_row(indices):
    answer, evidence, gate, _ = inputs()
    result = proposed_scope_bound_batch(answer, evidence, gate, indices)
    assert not result["accepted"] and result["reason"] == "unedited_nonindependent_inheritance"
    assert any("| A |" in text or "| B |" in text for text in result["forbidden_removed"])


def test_all_three_real_table_rows_can_only_add_the_already_independent_header_as_collateral():
    answer, evidence, gate, _ = inputs()
    result = proposed_scope_bound_batch(answer, evidence, gate, (0, 1, 2))
    assert result["accepted"]
    assert len(result["selected_removed"]) == 3
    assert result["independent_collateral"] == [gate["scoped_review"]["decisions"][1]["text"]]
    assert result["semantic_verification"] == "not_performed"


@pytest.mark.parametrize("kind", ["process", "source", "derived", "mixed", "uncertain", "unknown"])
def test_no_process_substantive_or_unknown_exemption_can_supply_table_collateral_permission(kind):
    answer, evidence, gate, _ = inputs()
    gate = deepcopy(gate)
    gate["scoped_review"]["decisions"][1]["kind"] = kind
    assert gate["scoped_review"]["decisions"][1]["requires_citation"] is False
    result = proposed_scope_bound_batch(answer, evidence, gate, (0, 1, 2))
    assert not result["accepted"]


@pytest.mark.parametrize("indices", [(3,), (3, 4), (3, 5), (0, 1, 2, 3)])
def test_real_introduction_requires_its_own_insertions_on_both_derived_group_bullets(indices):
    answer, evidence, gate, _ = inputs()
    result = proposed_scope_bound_batch(answer, evidence, gate, indices)
    assert not result["accepted"] and result["reason"] == "unedited_nonindependent_inheritance"
    assert any("**A" in text or "**B" in text for text in result["forbidden_removed"])


@pytest.mark.parametrize("indices", [(3, 4, 5), (0, 1, 2, 3, 4, 5), (0, 1, 2, 3, 4, 5, 6)])
def test_real_complete_batches_can_annotate_each_substantive_claim_and_only_exempt_independent_collateral(indices):
    answer, evidence, gate, _ = inputs()
    result = proposed_scope_bound_batch(answer, evidence, gate, indices)
    assert result["accepted"] and result["annotation_only"]
    assert len(result["selected_removed"]) == len(indices)
    assert len(result["independent_collateral"]) == (1 if 0 in indices else 0)
    assert gate["unsupported"][7] in result["still_unsupported"]
    assert result["semantic_verification"] == "not_performed"


def test_mixed_real_long_sentence_cannot_join_a_safe_batch_and_stale_scope_cannot_authorize_any_batch():
    answer, evidence, gate, _ = inputs()
    assert not proposed_scope_bound_batch(answer, evidence, gate, tuple(range(8)))["accepted"]
    for mutation in ("verified", "text", "count"):
        stale = deepcopy(gate)
        if mutation == "verified":
            stale["scoped_review"]["verified"] = 1
        elif mutation == "text":
            stale["scoped_review"]["decisions"][1]["text"] += " forged"
        else:
            stale["scoped_review"]["original_total_claims"] += 1
        result = proposed_scope_bound_batch(answer, evidence, stale, (0, 1, 2))
        assert not result["accepted"] and result["reason"] == "unbound_or_ineligible_edit"


@pytest.mark.parametrize("kind", ["process", "source", "derived", "mixed", "uncertain", "unknown"])
def test_cached_safe_table_batch_rechecks_the_current_independent_collateral_permission(kind):
    answer, evidence, gate, _ = inputs()
    original = repair.repair_citation_markers(answer, evidence, gate,
        SyntheticAnnotationModel([proposal(answer, gate, index) for index in (0, 1, 2)]))
    assert original["changed"]
    current = deepcopy(gate)
    current["scoped_review"]["decisions"][1]["kind"] = kind
    replay = repair.reuse_citation_marker_annotation(answer, evidence, current, original)
    assert not replay["changed"] and replay["answer"] == answer and not replay["edits"]


def test_cached_batch_cannot_drop_one_substantive_edit_and_rely_on_table_inheritance():
    answer, evidence, gate, _ = inputs()
    original = repair.repair_citation_markers(answer, evidence, gate,
        SyntheticAnnotationModel([proposal(answer, gate, index) for index in (0, 1, 2)]))
    assert original["changed"]
    forged = deepcopy(original)
    forged["edits"] = forged["edits"][:2]
    forged["answer"] = repair._insert(answer, forged["edits"])
    replay = repair.reuse_citation_marker_annotation(answer, evidence, gate, forged)
    assert not replay["changed"] and replay["reason"] == "cached_batch_mismatch"
    assert replay["answer"] == answer
