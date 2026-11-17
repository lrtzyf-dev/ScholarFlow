"""Independent full-answer/rubric/machine-layer tests with synthetic inputs."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import review_real_acceptance as subject


ANSWER = "已列出两项观察结果。仅有汇总均值就能证明干预导致改善。"
BAD_SENTENCE = "仅有汇总均值就能证明干预导致改善。"
CRITERIA = ["列出观察结果", "说明材料范围"]


def inputs(*, human=False, machine=True, decision="pass"):
    report = {"kind": "real_acceptance_run", "source_unchanged_during_evaluation": True,
        "cases": [{"id": "INDEPENDENT", "acceptance": CRITERIA.copy(), "result": {"answer": ANSWER},
            "machine_receipts_passed": machine, "status": "needs_semantic_review" if machine is True else "machine_failed",
            "passed": False, "semantic_review": {"status": "pending"}}]}
    review = {"reviewer": "Synthetic independent reader", "reviewer_kind": "human" if human else "agent",
        "independent_review_attestation": True, "case_id": "INDEPENDENT", "decision": decision,
        "rubric": [{"criterion": criterion, "passed": True, "note": "Synthetic exact original criterion inspected"}
            for criterion in CRITERIA]}
    return report, review


def finding(**changes):
    return {"kind": "methodological_error", "answer_text": BAD_SENTENCE,
        "reason": "只有汇总均值不足以识别因果效应。", "severity": "major", "confirmed": True, **changes}


def derive(report, review):
    return subject.reviewed_report(report, review, source_report_sha256="1" * 64, review_sha256="2" * 64)


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("machine", [False, True])
def test_semantic_truth_machine_delivery_and_human_authority_are_separate(human, machine):
    report, review = inputs(human=human, machine=machine)
    raw, original_review = deepcopy(report), deepcopy(review)
    result = derive(report, review)
    case = result["cases"][0]
    saved = case["semantic_review" if human else "agent_review"]
    assert saved["status"] == "passed" and saved["original_rubric_passed"] is True
    assert saved["rubric"] == review["rubric"]
    assert saved["machine_receipts_passed"] is machine
    assert case["passed"] is (human and machine)
    assert case["status"] == ("machine_failed" if not machine else "accepted" if human else "needs_human_review")
    assert result["passed"] is (human and machine) and result["commercial_acceptance"] is False
    assert report == raw and review == original_review


@pytest.mark.parametrize("machine", [None, 1, "true", {}])
def test_truthy_or_absent_machine_receipt_is_never_formal_acceptance(machine):
    report, review = inputs(human=True, machine=machine)
    case = derive(report, review)["cases"][0]
    assert case["semantic_review"]["status"] == "passed"
    assert case["machine_receipts_passed"] == machine
    assert case["passed"] is False and case["status"] == "machine_failed"


def test_full_answer_method_error_fails_without_inventing_original_criterion_failure():
    report, review = inputs(decision="fail")
    review["overall_findings"] = [finding()]
    result = derive(report, review)
    saved = result["cases"][0]["agent_review"]
    assert saved["status"] == "failed" and saved["original_rubric_passed"] is True
    assert [row["passed"] for row in saved["rubric"]] == [True, True]
    assert saved["overall_findings"] == review["overall_findings"]
    assert result["cases"][0]["passed"] is False and result["passed"] is False


@pytest.mark.parametrize("changes", [{"answer_text": "旧稿曾出现、当前终稿没有的句子"}, {"answer_text": ""},
    {"answer_text": None}, {"reason": " \n\t "}, {"reason": "x" * 2001}, {"confirmed": 1},
    {"severity": "fatal"}, {"kind": "stylistic_preference"}, {"external_source": "not a current answer span"}])
def test_finding_requires_exact_current_span_and_strict_five_key_contract(changes):
    report, review = inputs(decision="fail")
    review["overall_findings"] = [finding(**changes)]
    original = deepcopy(report)
    with pytest.raises(ValueError): derive(report, review)
    assert report == original


def test_more_than_32_findings_and_nonlist_findings_are_rejected():
    report, review = inputs(decision="fail")
    for invalid in ([finding()] * 33, {"finding": finding()}):
        review["overall_findings"] = invalid
        with pytest.raises(ValueError): derive(report, review)


def test_semantic_pass_cannot_conceal_confirmed_method_error():
    report, review = inputs()
    review["overall_findings"] = [finding()]
    with pytest.raises(ValueError): derive(report, review)


def test_failed_semantic_review_requires_an_actual_original_or_full_answer_failure():
    report, review = inputs(decision="fail")
    with pytest.raises(ValueError): derive(report, review)
    review["rubric"][1].update(passed=False, note="Synthetic actual missing scope statement")
    assert derive(report, review)["cases"][0]["agent_review"]["original_rubric_passed"] is False


def test_other_failed_case_survives_human_acceptance_of_selected_case():
    report, review = inputs(human=True)
    other = {**deepcopy(report["cases"][0]), "id": "OTHER", "status": "machine_failed",
        "machine_receipts_passed": False}
    report["cases"].append(other)
    result = derive(report, review)
    assert result["cases"][0]["status"] == "accepted"
    assert result["cases"][1] == other and result["passed"] is False


def test_agent_pass_does_not_erase_preexisting_human_failed_review():
    report, review = inputs(human=True, decision="fail")
    review["overall_findings"] = [finding()]
    failed = derive(report, review)
    later = {**deepcopy(review), "reviewer_kind": "agent", "decision": "pass", "overall_findings": []}
    result = derive(failed, later)
    case = result["cases"][0]
    assert case["semantic_review"] == failed["cases"][0]["semantic_review"]
    assert case["agent_review"]["status"] == "passed"
    assert case["status"] == "semantic_failed" and case["passed"] is False


def run_cli(raw, review, output):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(Path(subject.__file__).resolve()), "--run-report", str(raw),
        "--review", str(review), "--output", str(output)], env=env, capture_output=True, text=True, timeout=15)


@pytest.fixture
def cli_files(tmp_path):
    report, review = inputs(human=True, machine=False)
    raw, assessment = tmp_path / "raw.json", tmp_path / "independent.json"
    raw.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assessment.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    return raw, assessment


def test_real_cli_preserves_raw_inputs_and_reports_actual_deriver_hash(cli_files, tmp_path):
    raw, assessment = cli_files
    original = raw.read_bytes(), assessment.read_bytes()
    output = tmp_path / "derived" / "report.json"
    completed = run_cli(raw, assessment, output)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    value = json.loads(output.read_text(encoding="utf-8"))
    saved = value["cases"][0]["semantic_review"]
    assert saved["source_report_sha256"] == hashlib.sha256(original[0]).hexdigest()
    assert saved["review_sha256"] == hashlib.sha256(original[1]).hexdigest()
    assert value["review_deriver_sha256"] == hashlib.sha256(Path(subject.__file__).read_bytes()).hexdigest()
    assert value["cases"][0]["status"] == "machine_failed" and saved["status"] == "passed"
    assert value["passed"] is False and value["commercial_acceptance"] is False
    assert (raw.read_bytes(), assessment.read_bytes()) == original


@pytest.mark.parametrize("destination", ["raw", "review", "existing"])
def test_real_cli_refuses_to_overwrite_original_or_existing_evidence(cli_files, tmp_path, destination):
    raw, assessment = cli_files
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"PRESERVE-EXISTING-REVIEW-EXACTLY")
    original = raw.read_bytes(), assessment.read_bytes(), existing.read_bytes()
    output = {"raw": raw, "review": assessment, "existing": existing}[destination]
    assert run_cli(raw, assessment, output).returncode == 1
    assert (raw.read_bytes(), assessment.read_bytes(), existing.read_bytes()) == original
