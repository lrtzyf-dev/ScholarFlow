"""Record exact rubric truth without erasing machine/full-answer failures."""
from copy import deepcopy

import pytest

from scripts.review_real_acceptance import reviewed_report


ANSWER = "该点估计无法进行任何不确定性分析。"


def data(*, reviewer_kind="agent", decision="pass", machine=False):
    report = {"kind": "real_acceptance_run", "source_unchanged_during_evaluation": True,
        "cases": [{"id": "ONE", "acceptance": ["原准则一", "原准则二"],
            "machine_receipts_passed": machine, "status": "needs_semantic_review" if machine else "machine_failed",
            "result": {"answer": ANSWER}, "semantic_review": {"status": "pending"}, "passed": False}]}
    review = {"reviewer": "Independent synthetic reviewer", "reviewer_kind": reviewer_kind,
        "independent_review_attestation": True, "case_id": "ONE", "decision": decision,
        "rubric": [{"criterion": text, "passed": True, "note": "Synthetic exact criterion checked"}
            for text in report["cases"][0]["acceptance"]]}
    return report, review


def derive(report, review):
    return reviewed_report(report, review, source_report_sha256="a" * 64, review_sha256="b" * 64)


@pytest.mark.parametrize("reviewer_kind", ["agent", "human"])
def test_semantic_rubric_pass_is_recorded_without_upgrading_failed_machine(reviewer_kind):
    report, review = data(reviewer_kind=reviewer_kind)
    before = deepcopy(report)
    result = derive(report, review)
    case = result["cases"][0]
    assert case["status"] == "machine_failed" and case["passed"] is False
    recorded = case["agent_review" if reviewer_kind == "agent" else "semantic_review"]
    assert recorded["status"] == "passed" and all(row["passed"] for row in recorded["rubric"])
    assert report == before and result["passed"] is False and result["commercial_acceptance"] is False


def finding(**changes):
    return {"kind": "methodological_error", "answer_text": ANSWER,
        "reason": "未报告区间不等于无法在声明假设下进行分析。", "severity": "major",
        "confirmed": True, **changes}


def test_extra_false_claim_can_fail_full_answer_while_original_criteria_remain_true():
    report, review = data(decision="fail", machine=True)
    review["overall_findings"] = [finding()]
    result = derive(report, review)
    case = result["cases"][0]
    assert case["status"] == "agent_review_failed" and not case["passed"]
    assert all(row["passed"] for row in case["agent_review"]["rubric"])
    assert case["agent_review"]["overall_findings"] == review["overall_findings"]


@pytest.mark.parametrize("changes", [
    {"answer_text": "原答案从未出现的句子"}, {"answer_text": ""}, {"reason": ""},
    {"confirmed": False}, {"confirmed": 1}, {"kind": "arbitrary"}, {"severity": "arbitrary"},
    {"injected": "not an allowed review field"},
])
def test_full_answer_finding_needs_strict_current_answer_location(changes):
    report, review = data(decision="fail", machine=True)
    review["overall_findings"] = [finding(**changes)]
    with pytest.raises(ValueError):
        derive(report, review)


def test_all_true_failed_review_still_requires_a_specific_confirmed_error():
    report, review = data(decision="fail", machine=True)
    with pytest.raises(ValueError, match="FailedReviewRequiresFailedCriterion"):
        derive(report, review)


def test_pass_cannot_conceal_confirmed_overall_findings():
    report, review = data(machine=True)
    review["overall_findings"] = [finding()]
    with pytest.raises(ValueError):
        derive(report, review)
