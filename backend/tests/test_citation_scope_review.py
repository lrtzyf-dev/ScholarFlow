"""Mixed answers keep source claims checked without sourcing independent advice.

The synthetic reviewer verifies protocol and fail-closed behavior. It does not
prove a real model correctly judges all citation requirements or factual truth.
"""
from copy import deepcopy
import json

import pytest

from app.agent.audit import review_citation_scope
from app.llm import APIClient, LLMResponse, MockLLM
from app.rag.citation import citation_gate


EVIDENCE = [{"id": "e1", "paper_title": "Example paper",
             "quote": "作者在对照实验中使用八个注意力头。", "page": 2}]
QUESTION = "先概括论文方法，再解释梯度下降的基础概念。"
SOURCE = "作者在对照实验中使用八个注意力头。[^e1]"
GENERAL = "梯度下降是一种通过迭代更新参数来降低目标函数的方法。"
UNSUPPORTED_SOURCE = "实验组的识别准确率达到了百分之九十九。"


class Reviewer:
    mode = "api"

    def __init__(self, results=None, *, raw=None, error=None):
        self.results = results
        self.raw = raw
        self.error = error
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return LLMResponse(self.raw if self.raw is not None else json.dumps({"results": self.results}))


def result(index, requires_citation, reason="独立于该论文的基础概念说明"):
    return {"index": index, "requires_citation": requires_citation, "reason": reason}


def mixed_answer():
    answer = SOURCE + "\n\n" + GENERAL
    gate = citation_gate(answer, ["e1"])
    assert gate["total_claims"] == 2 and gate["marked_claims"] == 1
    assert gate["unsupported"] == [GENERAL]
    return answer, gate


def test_mixed_answer_exempts_only_independent_explanation_without_mutating_gate():
    answer, gate = mixed_answer()
    original = deepcopy(gate)
    reviewer = Reviewer([result(0, False)])
    reviewed = review_citation_scope(QUESTION, answer, EVIDENCE, gate, reviewer)
    assert gate == original
    assert reviewed["passed"] is True
    assert reviewed["unsupported"] == []
    assert reviewed["marked_claims"] == 1
    assert reviewed["total_claims"] == 1
    assert reviewed["grounded_ratio"] == 1.0
    scope = reviewed["scoped_review"]
    assert scope["verified"] is True and scope["mode"] == "llm"
    assert scope["scope"] == "citation_requirement_only"
    assert scope["original_total_claims"] == 2
    assert scope["original_grounded_ratio"] == 0.5
    assert scope["exemptions"][0]["text"] == GENERAL
    messages, kwargs = reviewer.calls[0]
    payload = json.loads(messages[1]["content"])
    assert payload["question"] == QUESTION and payload["full_answer"] == answer
    assert payload["evidence"][0]["quote"] == EVIDENCE[0]["quote"]
    assert payload["uncited_candidates"] == [{"index": 0, "text_prefix": GENERAL}]
    assert kwargs == {"temperature": 0}


def test_uncited_paper_fact_retains_requirement_and_failure():
    answer = SOURCE + "\n\n" + UNSUPPORTED_SOURCE
    gate = citation_gate(answer, ["e1"])
    reviewed = review_citation_scope(QUESTION, answer, EVIDENCE, gate,
                                     Reviewer([result(0, True, "该句声称论文实验结果，仍须原文证据")]))
    for key, value in gate.items():
        assert reviewed[key] == value
    assert reviewed["passed"] is False
    assert reviewed["scoped_review"]["exemptions"] == []


def test_partial_mixed_review_keeps_unreferenced_paper_fact_and_recalculates_denominator():
    answer = SOURCE + "\n\n" + GENERAL + "\n\n" + UNSUPPORTED_SOURCE
    gate = citation_gate(answer, ["e1"])
    reviewer = Reviewer([result(1, True, "论文实验结论"), result(0, False)])
    reviewed = review_citation_scope(QUESTION, answer, EVIDENCE, gate, reviewer)
    assert reviewed["unsupported"] == [UNSUPPORTED_SOURCE]
    assert reviewed["total_claims"] == 2
    assert reviewed["marked_claims"] == 1
    assert reviewed["grounded_ratio"] == 0.5
    assert reviewed["passed"] is False


@pytest.mark.parametrize("raw", [
    "not json",
    '```json\n{"results":[{"index":0,"requires_citation":false,"reason":"general"}]}\n```',
    '{"results":[]}',
    '{"results":[{"index":0,"requires_citation":false,"reason":"general"},'
    '{"index":0,"requires_citation":false,"reason":"general"}]}',
    '{"results":[{"index":true,"requires_citation":false,"reason":"general"}]}',
    '{"results":[{"index":1,"requires_citation":false,"reason":"general"}]}',
    '{"results":[{"index":"0","requires_citation":false,"reason":"general"}]}',
    '{"results":[{"index":0,"requires_citation":"false","reason":"general"}]}',
    '{"results":[{"index":0,"requires_citation":false}]}',
    '{"results":[{"index":0,"requires_citation":false,"reason":" "}]}',
    '{"results":[{"index":0,"requires_citation":false,"reason":5}]}',
    '{"results":[{"index":0,"requires_citation":false,"reason":"general","extra":1}]}',
    '{"results":[{"index":0,"requires_citation":true,"requires_citation":false,"reason":"general"}]}',
    '{"results":[{"index":0,"requires_citation":false,"reason":"general"}],"extra":NaN}',
    '{"results":[null]}',
    '[{"index":0,"requires_citation":false,"reason":"general"}]',
])
def test_invalid_review_never_exempts_candidates(raw):
    answer, gate = mixed_answer()
    reviewed = review_citation_scope(QUESTION, answer, EVIDENCE, gate, Reviewer(raw=raw))
    assert reviewed is gate


def test_exception_does_not_leak_error_or_change_gate():
    answer, gate = mixed_answer()
    reviewed = review_citation_scope(QUESTION, answer, EVIDENCE, gate,
                                     Reviewer(error=RuntimeError("private transport details")))
    assert reviewed is gate
    assert "private" not in json.dumps(reviewed)


def test_duplicate_or_missing_results_in_multi_candidate_review_reject_entire_review():
    answer = SOURCE + "\n\n" + GENERAL + "\n\n" + UNSUPPORTED_SOURCE
    gate = citation_gate(answer, ["e1"])
    for results in ([result(0, False), result(0, True)], [result(0, False)]):
        assert review_citation_scope(QUESTION, answer, EVIDENCE, gate, Reviewer(results)) is gate


def test_invalid_citation_markers_are_never_reviewed_or_bypassed():
    answer, _ = mixed_answer()
    answer += "\n\n论文还得出了另一个没有证据的结论。[^e999]"
    gate = citation_gate(answer, ["e1"])
    reviewer = Reviewer([result(0, False)])
    assert review_citation_scope(QUESTION, answer, EVIDENCE, gate, reviewer) is gate
    assert reviewer.calls == []
    assert gate["invalid_markers"] == ["e999"]


def test_source_free_or_fully_cited_answers_do_not_add_an_inference():
    answer, gate = mixed_answer()
    reviewer = Reviewer([result(0, False)])
    assert review_citation_scope(QUESTION, answer, [], gate, reviewer) is gate
    cited_gate = citation_gate(SOURCE, ["e1"])
    assert review_citation_scope(QUESTION, SOURCE, EVIDENCE, cited_gate, reviewer) is cited_gate
    assert reviewer.calls == []


def test_mock_unknown_modes_and_oversized_review_stay_conservative():
    answer, gate = mixed_answer()

    class PretendOnlineMock(MockLLM):
        mode = "api"

        def chat(self, *args, **kwargs):
            pytest.fail("Mock must not review citation scope")

    assert review_citation_scope(QUESTION, answer, EVIDENCE, gate, PretendOnlineMock()) is gate
    for mode in (None, "mock", "unknown"):
        reviewer = Reviewer([result(0, False)])
        reviewer.mode = mode
        assert review_citation_scope(QUESTION, answer, EVIDENCE, gate, reviewer) is gate
        assert reviewer.calls == []
    reviewer = Reviewer([])
    oversized = {**gate, "unsupported": [GENERAL] * 101, "total_claims": 102}
    assert review_citation_scope(QUESTION, answer, EVIDENCE, oversized, reviewer) is oversized
    assert reviewer.calls == []


def test_real_api_client_type_is_recognized_without_reading_global_configuration():
    class SyntheticAPIClient(APIClient):
        def chat(self, *args, **kwargs):
            return LLMResponse(json.dumps({"results": [result(0, False)]}))

    answer, gate = mixed_answer()
    assert review_citation_scope(QUESTION, answer, EVIDENCE, gate, SyntheticAPIClient())["passed"] is True
    invalid_binding = SyntheticAPIClient(runtime_config={"mode": "unknown"})
    assert review_citation_scope(QUESTION, answer, EVIDENCE, gate, invalid_binding) is gate


@pytest.mark.parametrize("valid_json", [True, False])
def test_review_usage_is_reported_even_when_review_cannot_be_applied(valid_json):
    usage = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}

    class MeteredReviewer(Reviewer):
        def chat(self, *args, **kwargs):
            content = json.dumps({"results": [result(0, False)]}) if valid_json else "not JSON"
            return LLMResponse(content, usage=usage)

    answer, gate = mixed_answer()
    recorded = []
    reviewed = review_citation_scope(QUESTION, answer, EVIDENCE, gate, MeteredReviewer(),
                                     on_usage=recorded.append)
    assert recorded == [usage]
    assert reviewed["passed"] is valid_json
