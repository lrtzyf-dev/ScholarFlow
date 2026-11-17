"""Invalid reviews remain unknown, with bounded diagnostic codes, no raw echoes."""
from copy import deepcopy

import pytest

from app.agent.source_attribution import validate_quality_issues
from app.agent.rewritten_answer_quality import _finish


QUESTION = "预算100元，不添置设备。"
ANSWER = "购买150元设备。"


def valid_issue():
    return {"kind": "constraint_violation", "text": ANSWER, "related_text": None,
            "user_constraint": {"receipt_id": "question", "quote": "预算100元"},
            "reason": "超过预算。", "confirmed": True}


@pytest.mark.parametrize("change,code", [
    ({"kind": []}, "invalid_kind"),
    ({"text": "非当前答案、含私密诊断原文"}, "answer_excerpt_not_found"),
    ({"user_constraint": None}, "user_constraint_required"),
    ({"user_constraint": {"receipt_id": "tool:secret", "quote": "密钥"}},
     "constraint_user_receipt_not_found"),
    ({"user_constraint": {"receipt_id": "question", "quote": "未说过的约束"}},
     "constraint_quote_not_found"),
    ({"confirmed": 1}, "invalid_confirmation"),
])
def test_diagnostics_distinguish_protocol_failures_without_relaxing_them(change, code):
    item = {**valid_issue(), **change}
    value = validate_quality_issues([item], ANSWER, QUESTION, [])
    assert value["quality_status"] == "unverified"
    assert value["quality_verified"] is False and value["quality_issues"] == []
    assert value["quality_protocol_errors"] == [{"index": 0, "code": code}]
    finished = _finish(value, ())
    assert finished["blocks_completion"] is True
    assert finished["quality_protocol_errors"] == [{"index": 0, "code": code}]


def test_confirmed_failure_and_malformed_second_row_are_both_preserved():
    value = validate_quality_issues([valid_issue(), {"secret": "not a review issue"}],
                                    ANSWER, QUESTION, [])
    assert value["quality_status"] == "failed" and value["quality_verified"] is False
    assert value["quality_protocol_errors"] == [{"index": 1, "code": "invalid_issue_keys"}]
    assert len(value["quality_issues"]) == 1 and value["quality_issues"][0]["confirmed"] is True
    assert _finish(value, ())["blocks_completion"] is True


def test_empty_list_remains_a_known_pass_and_error_metadata_is_not_shared():
    value = validate_quality_issues([], ANSWER, QUESTION, [])
    assert value["quality_status"] == "passed" and value["quality_verified"] is True
    assert value["quality_protocol_errors"] == []
    failed = validate_quality_issues([{"invalid": True}], ANSWER, QUESTION, [])
    before = deepcopy(failed)
    finished = _finish(failed, ())
    finished["quality_protocol_errors"][0]["code"] = "changed"
    assert failed == before


def test_missing_and_wrong_root_list_have_distinct_unknown_diagnostics():
    missing = validate_quality_issues(None, ANSWER, QUESTION, [])
    wrong = validate_quality_issues({}, ANSWER, QUESTION, [])
    assert missing["quality_protocol_errors"] == [{"index": None, "code": "missing_list"}]
    assert wrong["quality_protocol_errors"] == [{"index": None, "code": "invalid_list"}]
    assert missing["quality_status"] == wrong["quality_status"] == "unverified"


def test_public_diagnostics_preserve_only_bounded_protocol_metadata():
    from scripts.verify_real_acceptance import public_diagnostics

    value = public_diagnostics([], {"answer_quality": {
        "status": "unverified", "protocol_errors": [{"index": 0,
            "code": "constraint_quote_not_found", "provider_raw": "private"}],
        "provider_raw": "private"}})
    assert value["final_answer_quality"] == {"status": "unverified",
        "protocol_errors": [{"index": 0, "code": "constraint_quote_not_found"}]}
