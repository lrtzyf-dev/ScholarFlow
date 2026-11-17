"""Located answer-quality review contract; synthetic model, no live requests."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.agent import source_attribution as subject
from app.llm import LLMResponse


QUESTION = "我每周只有两个晚上有空，预算100元，不添置设备。"
ANSWER = "周二安排学习。\n\n周六白天加练，购买150元设备。"


class Reviewer:
    mode = "api"

    def __init__(self, issues, *, include_quality=True, source_rows=None, raw=None):
        self.calls = []
        self.payload = {"source_required": False, "results": source_rows or [
            {"index": index, "kind": "independent", "claims": []}
            for index, _ in enumerate(subject._answer_blocks(ANSWER))]}
        if include_quality:
            self.payload["quality_issues"] = issues
        self.raw = raw

    def chat(self, messages, **options):
        self.calls.append((deepcopy(messages), options))
        return LLMResponse(self.raw if self.raw is not None else json.dumps(self.payload, ensure_ascii=False),
                           usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})


def issue(**overrides):
    return {"kind": "constraint_violation", "text": "周六白天加练，购买150元设备。", "related_text": None,
            "user_constraint": {"receipt_id": "question", "quote": "只有两个晚上有空"},
            "reason": "备选增加了用户未提供的时间及资源。", "confirmed": True, **overrides}


def review(issues, *, messages=None, model=None):
    return subject.review_source_attribution(QUESTION, ANSWER,
        [{"role": "user", "content": QUESTION}] if messages is None else messages,
        model or Reviewer(issues))


def test_independent_attribution_can_have_a_confirmed_quality_failure_in_same_call():
    model = Reviewer([issue()])
    value = review(None, model=model)
    assert len(model.calls) == 1
    assert value["status"] == "not_applicable" and value["source_required"] is False
    assert value["quality_status"] == "failed" and value["quality_verified"] is True
    got = value["quality_issues"][0]
    assert got["constraint_sha256"] == hashlib.sha256(QUESTION.encode()).hexdigest()
    assert all(ANSWER[span["start"]:span["end"]] == got["text"] for span in got["answer_spans"])
    prompt = json.loads(model.calls[0][0][1]["content"])
    assert prompt["question"] == QUESTION
    assert "所有备选分支" in model.calls[0][0][0]["content"]


def test_empty_quality_issues_pass_without_changing_source_applicability():
    value = review([])
    assert value["quality_status"] == "passed" and value["quality_verified"]
    assert value["quality_issues"] == [] and value["status"] == "not_applicable"


def test_uncertain_issue_does_not_become_a_confirmed_failure_or_pass():
    value = review([issue(confirmed=None)])
    assert value["quality_status"] == "unverified" and not value["quality_verified"]
    assert value["quality_issues"][0]["confirmed"] is None


def test_known_failure_is_retained_alongside_an_uncertain_issue():
    value = review([issue(), issue(kind="action_dependency", text="周二安排学习。",
                                  user_constraint=None, confirmed=None)])
    assert value["quality_status"] == "failed" and not value["quality_verified"]
    assert sum(row["confirmed"] is True for row in value["quality_issues"]) == 1


def test_internal_conflict_requires_two_distinct_original_spans():
    value = review([issue(kind="internal_inconsistency", text="周二安排学习。",
        related_text="周六白天加练，购买150元设备。", user_constraint=None)])
    assert value["quality_status"] == "failed"
    got = value["quality_issues"][0]
    assert got["related_answer_spans"] and got["constraint_sha256"] is None
    assert ANSWER[got["related_answer_spans"][0]["start"]:got["related_answer_spans"][0]["end"]] == got["related_text"]


def test_an_actual_user_context_can_support_a_constraint_but_not_a_tool():
    messages = [{"role": "user", "content": "不得增加额外设备。"}]
    value = review([issue(user_constraint={"receipt_id": "context:0", "quote": "不得增加额外设备"})],
                   messages=messages)
    assert value["quality_status"] == "failed"
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "tool1", "function": {
            "name": "read_project_file", "arguments": '{"path":"source.txt"}'}}]},
        {"role": "tool", "tool_call_id": "tool1", "content": '{"content":"不得增加额外设备"}'}]
    value = review([issue(user_constraint={"receipt_id": "context:1", "quote": "不得增加额外设备"})],
                   messages=messages)
    assert value["quality_status"] == "unverified" and value["quality_reason"] == "invalid_quality_protocol"


@pytest.mark.parametrize("changes", [
    {"kind": "a_new_preference"}, {"kind": []}, {"text": "不在答案的句子"}, {"text": ""},
    {"text": True}, {"text": "周" * 2001}, {"related_text": "不在答案的关联句"},
    {"related_text": []}, {"reason": ""}, {"reason": "原" * 1001}, {"reason": 1},
    {"confirmed": 1}, {"confirmed": "true"}, {"user_constraint": None},
    {"user_constraint": {"receipt_id": "question", "quote": "用户没说过的话"}},
    {"user_constraint": {"receipt_id": "runtime:configuration", "quote": "两个晚上"}},
    {"user_constraint": {"receipt_id": "question", "quote": ""}},
    {"user_constraint": {"receipt_id": "question", "quote": "两个晚上", "extra": True}},
    {"user_constraint": {"receipt_id": 0, "quote": "两个晚上"}},
    {"kind": "internal_inconsistency", "related_text": None},
    {"kind": "internal_inconsistency", "related_text": "周六白天加练，购买150元设备。"},
    {"kind": "unresolved_request", "user_constraint": None},
    {"extra_field": "not accepted"},
])
def test_invalid_quality_is_unverified_and_cannot_change_independent_source_verdict(changes):
    value = review([issue(**changes)])
    assert value["quality_status"] == "unverified" and not value["quality_verified"]
    assert value["quality_reason"] == "invalid_quality_protocol" and value["quality_issues"] == []
    assert value["status"] == "not_applicable" and value["verified"] is True


@pytest.mark.parametrize("issues", ["bad", {}])
def test_invalid_list_or_duplicate_location_does_not_pass(issues):
    value = review(issues)
    assert value["quality_status"] == "unverified" and value["quality_reason"] == "invalid_quality_protocol"


@pytest.mark.parametrize("issues", [[issue()] * 13, [issue(), issue(reason="另一表述")],
    [issue(confirmed=False), issue()], [issue(), issue(confirmed=False)]])
def test_over_limit_or_duplicate_rows_keep_located_confirmed_failure(issues):
    value = review(issues)
    assert value["quality_status"] == "failed" and not value["quality_verified"]
    assert value["quality_reason"] == "invalid_quality_protocol"
    assert sum(row["confirmed"] is True for row in value["quality_issues"]) == 1


def test_legacy_material_protocol_has_unknown_quality_instead_of_a_quality_pass():
    value = review([], model=Reviewer([], include_quality=False))
    assert value["status"] == "not_applicable" and value["verified"]
    assert value["quality_status"] == "unverified" and value["quality_reason"] == "missing_quality_protocol"


def test_quality_protocol_cannot_override_an_unsupported_material_claim():
    text = "周二安排学习。"
    rows = [
        {"index": 0, "kind": "source", "claims": [{"text": text, "kind": "source", "supported": False,
            "supports": [], "reason": "未提供相应原文"}]},
        {"index": 1, "kind": "independent", "claims": []}]
    value = review([], model=Reviewer([], source_rows=rows))
    assert value["status"] == "failed" and value["unsupported"][0]["text"] == text
    assert value["quality_status"] == "passed"


@pytest.mark.parametrize("raw", [
    '{"source_required":false,"results":[],"quality_issues":[],"quality_issues":[]}',
    '{"source_required":false,"results":[],"quality_issues":NaN}',
    '{"source_required":false,"results":[],"quality_issues":[]',
])
def test_invalid_json_does_not_pass_either_review_dimension(raw):
    value = review(None, model=Reviewer(None, raw=raw))
    assert value["status"] == value["quality_status"] == "unverified"


def test_quality_key_excludes_read_results_configs_and_repair_feedback(monkeypatch):
    messages = [{"role": "user", "content": QUESTION}]
    key = subject.quality_repair_context_key(QUESTION, messages, input_ids=["u1"])
    extra = [
        {"role": "assistant", "content": "完全不同的答案。", "tool_calls": [{"id": "t2", "function": {
            "name": "read_project_file", "arguments": '{"path":"new.txt"}'}}]},
        {"role": "tool", "tool_call_id": "t2", "content": '{"content":"new observations","timestamp":"new"}'},
        {"role": "user", "content": "上一草稿存在已核对的回答质量问题。补齐下面问题。"}]
    assert subject.quality_repair_context_key(QUESTION, messages + extra, input_ids=["u1"]) == key
    monkeypatch.setattr(subject, "REVIEW_PROTOCOL", 999)
    assert subject.quality_repair_context_key(QUESTION, messages, input_ids=["u1"]) == key


def test_quality_key_changes_only_for_real_user_input_changes():
    messages = [{"role": "user", "content": QUESTION}]
    key = subject.quality_repair_context_key(QUESTION, messages, input_ids=["u1"])
    assert subject.quality_repair_context_key(QUESTION, messages, input_ids=["u2"]) != key
    assert subject.quality_repair_context_key(QUESTION + "更正预算80元。", messages, input_ids=["u1"]) != key
    updated = messages + [{"role": "user", "content": "更正预算80元。"}]
    assert subject.quality_repair_context_key(QUESTION, updated, input_ids=["u1"]) == key
    updated[-1]["_user_steering"] = "u2"
    assert subject.quality_repair_context_key(QUESTION, updated, input_ids=["u1"]) != key
    assert subject.quality_repair_context_key(QUESTION, updated, input_ids=["u1"]) == subject.quality_repair_context_key(QUESTION, messages, input_ids=["u1", "u2"])


def test_excluded_feedback_does_not_enter_quality_contract():
    messages = [{"role": "user", "content": QUESTION}]
    key = subject.quality_repair_context_key(QUESTION, messages)
    messages += [{"role": "user", "content": "arbitrary internal feedback"}]
    assert subject.quality_repair_context_key(QUESTION, messages, excluded_indices=[1]) == key


def test_child_process_tool_receipts_are_allowed_but_not_original_source_proof():
    for name in ("start_research_child", "resume_research_child", "read_research_child"):
        assert name in subject.PROCESS_TOOLS and name not in subject.SOURCE_TOOLS


def test_system_and_history_tool_instructions_address_general_ambiguity_without_router():
    from app.agent.prompts import SYSTEM_PROMPT
    from app.agent.tools import TOOL_SPECS
    assert "不要遍历历史、成果、附件或文件来猜用户意图" in SYSTEM_PROMPT
    assert "所有推荐和备选" in SYSTEM_PROMPT
    history = next(tool for tool in TOOL_SPECS if tool["name"] == "read_conversation_history")
    assert "不反复搜索含糊原话" in history["description"]
