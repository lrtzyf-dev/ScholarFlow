"""Attach an explicit independent rubric review without overwriting raw runs."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

REVIEW_PROTOCOL = "independent_rubric_machine_and_full_answer_v2"


def _overall_findings(review, case):
    findings = review.get("overall_findings", [])
    answer = (case.get("result") or {}).get("answer", "")
    if not isinstance(findings, list) or len(findings) > 32:
        raise ValueError("InvalidFullAnswerFindings")
    for item in findings:
        if (not isinstance(item, dict)
                or set(item) != {"kind", "answer_text", "reason", "severity", "confirmed"}
                or item["kind"] not in {"methodological_error", "factual_error", "unsupported_claim", "constraint_violation"}
                or item["severity"] not in {"minor", "major", "blocking"}
                or item["confirmed"] is not True
                or not isinstance(item["answer_text"], str) or not 1 <= len(item["answer_text"]) <= 8192
                or not isinstance(answer, str) or item["answer_text"] not in answer
                or not isinstance(item["reason"], str) or not item["reason"].strip() or len(item["reason"]) > 2000):
            raise ValueError("FullAnswerFindingNeedsExactCurrentAnswerAndReason")
    return findings


def reviewed_report(report,review,*,source_report_sha256,review_sha256):
    result=deepcopy(report)
    if result.get("kind")!="real_acceptance_run"or not result.get("source_unchanged_during_evaluation"):
        raise ValueError("OnlyUnchangedRealRunsCanBeReviewed")
    if not isinstance(review.get("reviewer"),str)or not review["reviewer"].strip():
        raise ValueError("ExplicitIndependentReviewerRequired")
    reviewer_kind=review.get("reviewer_kind")
    if reviewer_kind not in ("human","agent"):raise ValueError("ExplicitReviewerKindRequired")
    if review.get("independent_review_attestation")is not True:raise ValueError("IndependentReviewAttestationRequired")
    cases=[c for c in result["cases"]if c["id"]==review.get("case_id")]
    if len(cases)!=1:raise ValueError("ReviewCaseNotInRun")
    case=cases[0]
    if case.get("status")not in ("needs_semantic_review","machine_failed","semantic_failed","accepted","needs_human_review","agent_review_failed"):
        raise ValueError("BlockedOrUnexecutedCaseCannotBeAccepted")
    rubric=review.get("rubric")
    if (not isinstance(rubric,list)or len(rubric)!=len(case["acceptance"])
        or [r.get("criterion")for r in rubric]!=case["acceptance"]
        or any(type(r.get("passed"))is not bool or not isinstance(r.get("note"),str)or not r["note"].strip()for r in rubric)):
        raise ValueError("EveryExactCriterionNeedsAReasonedReview")
    decision=review.get("decision")
    if decision not in ("pass","fail"):raise ValueError("InvalidSemanticDecision")
    findings = _overall_findings(review, case)
    rubric_passed = all(r["passed"] for r in rubric)
    if decision=="pass"and(not rubric_passed or findings):
        raise ValueError("FailedSemanticCriterionOrFullAnswerCannotPass")
    if decision=="fail"and rubric_passed and not findings:raise ValueError("FailedReviewRequiresFailedCriterionOrExactFullAnswerFinding")
    recorded={**deepcopy(review),"status":"passed"if decision=="pass"else"failed",
        "source_report_sha256":source_report_sha256,"review_sha256":review_sha256,
        "review_protocol": REVIEW_PROTOCOL, "original_rubric_passed": rubric_passed,
        "machine_receipts_passed": case.get("machine_receipts_passed") is True}
    if reviewer_kind=="agent":
        case["agent_review"]=recorded
        # Engineering/semantic review by another agent is useful evidence,
        # never a human gold label or formal semantic acceptance.
        if case.get("semantic_review",{}).get("reviewer_kind")!="human":
            case["passed"]=False
            case["status"]="needs_human_review"if decision=="pass"else"agent_review_failed"
    else:
        case["semantic_review"]=recorded
        case["passed"]=decision=="pass" and case.get("machine_receipts_passed") is True
        case["status"]="accepted"if case["passed"]else"semantic_failed"
    # Semantic truth and machine delivery are separate observations. Never
    # falsify an exact original criterion just to preserve a machine failure.
    # Even a human rubric pass cannot accept a partially delivered task.
    if case.get("machine_receipts_passed") is not True:
        case["passed"] = False
        case["status"] = "machine_failed"
    result["semantic_acceptance"]="human_reviewed_selected_cases"if all(c.get("semantic_review",{}).get("reviewer_kind")=="human"
        and c.get("semantic_review",{}).get("status")in ("passed","failed")for c in result["cases"])else"pending_independent_human_review"
    result["passed"]=bool(result["cases"])and all(c.get("passed")is True for c in result["cases"])
    result["commercial_acceptance"]=False
    result["review_deriver_protocol"] = REVIEW_PROTOCOL
    result["acceptance_scope"]="Only these selected runs; not the full 60-case, two-provider, native or release acceptance."
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-report",type=Path,required=True)
    p.add_argument("--review",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    if args.output.resolve()in (args.run_report.resolve(),args.review.resolve()):raise ValueError("PreserveOriginalRunAndReview")
    if args.output.exists():raise ValueError("PreserveExistingReviewedReport")
    original=args.run_report.read_bytes();review=args.review.read_bytes()
    value=reviewed_report(json.loads(original),json.loads(review),source_report_sha256=hashlib.sha256(original).hexdigest(),review_sha256=hashlib.sha256(review).hexdigest())
    value["review_deriver_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"selected_cases_passed":value["passed"],"commercial_acceptance":False}))


if __name__=="__main__":
    try:main()
    except Exception as error:
        print(json.dumps({"status":"failed","error_kind":type(error).__name__}));raise SystemExit(1)
