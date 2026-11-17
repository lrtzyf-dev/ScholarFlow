"""Independent adversarial boundaries for deterministic citation deletions.

Only synthetic public prose and complete fabricated semantic verdicts are used;
these tests establish mechanics, not truth of model judgments.
"""
from copy import deepcopy

import pytest

from app.agent.citation_local_cleanup import rejected_line_candidate
from app.rag.citation import parse_claims


EVIDENCE = [{"id": "e1", "quote": "Synthetic method has eight heads."}]
GOOD = "正确结果使用8个注意力头。[^e1]"
BAD = "错误结果使用12个注意力头。[^e1]"
TAIL = "另一正确结果保留8个分组。[^e1]"


def complete_audit(answer, evidence_ids=("e1",)):
    claims = [{"text": c["claim"], "evidence_ids": c["evidence_ids"]}
              for c in parse_claims(answer, list(evidence_ids))]
    return {"mode": "llm", "status": "failed", "verified": True, "passed": False,
            "claims": claims, "factual_claims": len(claims), "known_failed_count": 1,
            "unknown_count": 0, "unverified_count": 0, "unverified": [],
            "unsupported": [{"text": BAD.replace("[^e1]", ""),
                             "reason": "Complete synthetic false verdict for this exact claim."}]}


@pytest.mark.parametrize("descendant", [
    "  - 子项正确数据是8。[^e1]",
    "  保留的缩进延续限定这条父项。",
    "\n  - 空行后的子项正确数据是8。[^e1]",
    "\n  空行后的延续限定这条父项。",
    "\t- 制表符子项正确数据是8。[^e1]",
])
def test_parent_list_line_with_descendants_is_not_a_flat_independent_deletion(descendant):
    answer = GOOD + "\n\n- " + BAD + "\n" + descendant + "\n\n" + TAIL
    original = deepcopy(complete_audit(answer))
    assert rejected_line_candidate(answer, EVIDENCE, original) is None
    assert original == complete_audit(answer)


@pytest.mark.parametrize("list_marker", ["- ", "1. ", "2) "])
def test_lazy_unindented_list_continuation_cannot_be_orphaned(list_marker):
    answer = GOOD + "\n\n" + list_marker + BAD + "\n保留的无缩进续行限定父项。\n\n" + TAIL
    assert rejected_line_candidate(answer, EVIDENCE, complete_audit(answer)) is None


@pytest.mark.parametrize("opening,closing", [
    ("<div>", "</div>"),
    ("<pre>", "</pre>"),
    ("<!-- retained example begins", "retained example ends -->"),
    ("<script type=\"text/plain\">", "</script>"),
    ("<details>\n<summary>Example</summary>", "</details>"),
])
def test_rejected_line_inside_html_block_cannot_be_deleted(opening, closing):
    answer = GOOD + "\n\n" + opening + "\n\n" + BAD + "\n\n" + closing + "\n\n" + TAIL
    assert rejected_line_candidate(answer, EVIDENCE, complete_audit(answer)) is None


@pytest.mark.parametrize("field,value", [
    ("unknown_count", False), ("unverified_count", False),
    ("factual_claims", True), ("unverified", None), ("unverified", {}),
    ("passed", 0),
])
def test_schema_invalid_zero_like_fields_are_not_a_complete_verdict(field, value):
    answer = GOOD + "\n\n" + BAD + "\n\n" + TAIL
    audit = complete_audit(answer)
    audit[field] = value
    assert rejected_line_candidate(answer, EVIDENCE, audit) is None


def test_safe_crlf_deletion_keeps_surviving_unicode_and_trailing_bytes_exactly():
    answer = "🙂 " + GOOD + "\r\n\r\n" + BAD + "\r\n\r\n" + TAIL + "  \r\n"
    result = rejected_line_candidate(answer, EVIDENCE, complete_audit(answer))
    assert result is not None
    assert result["answer"] == answer.replace(BAD + "\r\n", "", 1)
    assert result["removals"][0]["text"] == BAD + "\r\n"
    assert result["semantic_verification"] == "not_performed"


def test_audit_for_previous_draft_cannot_delete_in_a_changed_draft():
    answer = GOOD + "\n\n" + BAD + "\n\n" + TAIL
    changed = answer.replace("12", "13")
    assert rejected_line_candidate(changed, EVIDENCE, complete_audit(answer)) is None


def test_implicit_citation_does_not_authorize_removing_parent_marker():
    # Distinct preceding citation is essential: e1 -> e1 would preserve the
    # exact evidence relationship and is not an inheritance counterexample.
    answer = GOOD.replace("e1", "e2") + "\n\n" + BAD + "\n\n- 正确但依赖前段引用的子项。\n\n" + TAIL
    evidence = EVIDENCE + [{"id": "e2", "quote": "A distinct synthetic source."}]
    assert rejected_line_candidate(answer, evidence, complete_audit(answer, ["e1", "e2"])) is None
