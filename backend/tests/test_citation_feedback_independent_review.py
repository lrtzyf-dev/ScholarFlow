"""Independent integration counterexamples; all responses and receipts synthetic.

This suite does not share the feedback author's fixtures, bind scopes manually,
or dispatch network/tool requests. It exercises the production reviewer,
feedback/message path, and durable single-rewrite journal together.
"""
from copy import deepcopy
import json

import pytest

from app.agent.audit import review_citation_scope
from app.agent.checkpoint import AgentState
from app.agent.citation_answer_rewrite import apply_citation_answer_rewrite
from app.agent.citation_repair_feedback import build_citation_repair_feedback
from app.agent.execution import _citation_repair_messages
from app.llm import LLMResponse
from app.rag.citation import citation_gate


QUESTION = "根据固定合成材料说明结果和局限，并仅如实说明实际读取范围。"
EVIDENCE = [{"id": "e1", "paper_title": "Independent synthetic sample",
             "quote": "There were 20 samples and no variance was reported.", "page": 1}]
PROCESS = "实际读取仅返回第1至2行，共3行，下一行是3，尚未读取全部提取文本。"
SOURCE = "这份材料的样本只有20条，因此不能据此断言普遍有效。"
ORIGINAL = "原文样本数为20条。[^e1]\n\n" + PROCESS + "\n\n" + SOURCE
USAGE = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


def history():
    """A real-shaped paired read receipt deliberately proves only a partial read."""
    return [{"role": "user", "content": QUESTION},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "independent-read",
            "type": "function", "function": {"name": "read_attached_source",
                "arguments": json.dumps({"source_revision_id": "b" * 32})}}]},
        {"role": "tool", "tool_call_id": "independent-read", "content": json.dumps({
            "read_range": {"kind": "text_lines", "line_start": 1, "line_end": 2,
                "total_lines": 3, "next_line": 3},
            "text_observed": True, "output_is_partial": False,
            "coverage_scope": "extracted_text_only", "source": {"revision_id": "b" * 32}},
            ensure_ascii=False)}]


class ScopeOracle:
    mode = "api"

    def __init__(self, reason="independent-reason", *, tool_reply=False):
        self.reason = reason
        self.tool_reply = tool_reply
        self.calls = []

    def chat(self, request, **options):
        assert "tools" not in options
        prompt = json.loads(request[-1]["content"])
        self.calls.append(deepcopy(prompt))
        results = []
        for item in prompt["uncited_candidates"]:
            text = item["text_prefix"]
            process = text.startswith("实际读取")
            independent = text.startswith("建议先明确")
            kind = "process" if process else "independent" if independent else "derived"
            supports = [{"receipt_id": prompt["process_receipts"][0]["id"],
                "quote": prompt["process_receipts"][0]["content"]}] if process else []
            if supports and prompt.get("response_protocol") == "citation_scope_with_process_receipts_v2":
                content = prompt["process_receipts"][0]["content"]
                chunks = [content[start:start + 240] for start in range(0, len(content), 240)]
                if len(chunks) > 1 and len(chunks[-1]) < 8:
                    overlap = 8 - len(chunks[-1])
                    chunks[-1] = chunks[-2][-overlap:] + chunks[-1]
                    chunks[-2] = chunks[-2][:-overlap]
                assert "".join(chunks) == content
                assert len(chunks) <= 8 and all(8 <= len(chunk) <= 240 for chunk in chunks)
                supports = [{"receipt_id": prompt["process_receipts"][0]["id"], "quote": chunk} for chunk in chunks]
            results.append({"index": item["index"], "requires_citation": not (process or independent),
                "reason": f"{self.reason}:{item['index']}:{kind}", "kind": kind,
                "process_scope": "bounded" if process else "none",
                "supports": supports})
        # Real models need not return candidates in order. The reviewer, not
        # these fixtures, must establish the correct reason/text correspondence.
        response = LLMResponse(json.dumps({"results": list(reversed(results))}, ensure_ascii=False))
        if self.tool_reply:
            response.tool_calls = [{"id": "unexpected", "name": "must_not_run", "arguments": {}}]
        return response


def scoped(answer=ORIGINAL, *, model=None, journal=None):
    model = model or ScopeOracle()
    return review_citation_scope(QUESTION, answer, deepcopy(EVIDENCE), citation_gate(answer, ["e1"]),
        model, messages=history(), review_journal=journal, persist=lambda: None, before_review=lambda: None)


def repair_request(answer, reason):
    gate = scoped(answer, model=ScopeOracle(reason))
    assert build_citation_repair_feedback(answer, gate, ["e1"])["available"] is True
    return _citation_repair_messages([{"role": "user", "content": QUESTION}], answer, gate,
        {"unsupported": []}, ["e1"])


@pytest.mark.parametrize("mutation,expected", [
    ("receipt_id", "cached_scope_receipt"),
    ("quote", "cached_scope_quote"),
    ("whole_scope", "cached_scope_whole_scope"),
])
def test_cached_process_support_is_revalidated_before_feedback_rebinding(mutation, expected):
    model, journal = ScopeOracle(), {}
    first = scoped(model=model, journal=journal)
    assert first["scoped_review"]["repair_feedback_binding"]
    assert build_citation_repair_feedback(ORIGINAL, first, ["e1"])["available"]
    entry = next(value for key, value in journal.items() if key.startswith("citation_scope/"))
    scope = entry["scoped_review"]
    process = next(d for d in scope["decisions"] if d["kind"] == "process")
    if mutation == "receipt_id":
        process["supports"][0]["receipt_id"] = "context:forged"
    elif mutation == "quote":
        process["supports"][0]["quote"] = "A wholly fabricated process receipt quote."
    else:
        process["process_scope"] = "whole_extracted_text"
    # Keep the aggregate projection consistent: receipt validation must catch
    # this, rather than a mismatched exemption list or stale feedback hash.
    scope["exemptions"] = [{k: deepcopy(v) for k, v in d.items() if k != "requires_citation"}
        for d in scope["decisions"] if not d["requires_citation"]]
    before = deepcopy(journal)
    again = scoped(model=model, journal=journal)
    assert again["scope_review_failure"] == expected
    assert "scoped_review" not in again
    assert not build_citation_repair_feedback(ORIGINAL, again, ["e1"])["available"]
    assert len(model.calls) == 1, "Corrupt cached process support must not buy a fresh scope review"
    assert journal == before, "Rejected cached data must not be silently repaired or rebound in place"


def test_out_of_order_actual_scope_keeps_markdown_text_reason_and_exemptions_aligned():
    long_text = "该合成样本中的这一方法只适用于当前观测范围" * 7 + "，尾部限制仍须保留。"
    answer = ("# 固定材料中结果及局限的完整分析\n\n"
        + PROCESS + "\n\n"
        + "- " + long_text + "\n\n"
        + "| 项目 | 固定合成材料中的结果 |\n|---|---|\n| 样本 | 20条且未给出方差 |\n\n"
        + "建议先明确后续复现所需的评价指标。\n\n"
        + "当前`20条`样本不能支持所有环境中的结论，\n还应对跨域结果作独立核验。")
    model = ScopeOracle("alignment")
    gate = scoped(answer, model=model)
    feedback = build_citation_repair_feedback(answer, gate, ["e1"])
    assert feedback["available"] is True
    decisions = gate["scoped_review"]["decisions"]
    required = [d for d in decisions if d["requires_citation"]]
    assert len(feedback["candidates"]) == len(required) >= 4
    assert {d["kind"] for d in gate["scoped_review"]["exemptions"]} == {"process", "independent"}
    for candidate, decision in zip(feedback["candidates"], required):
        assert candidate["index"] == decision["index"]
        assert candidate["text"][:80] == decision["text"]
        assert candidate["reason"] == f"alignment:{candidate['index']}:derived"
    assert any(c["text"] == long_text and len(c["text"]) > 80 for c in feedback["candidates"])
    assert any("还应对跨域结果" in c["text"] for c in feedback["candidates"])
    assert len(model.calls) == 1


class Interruption(BaseException):
    pass


class Rewriter:
    mode = "api"

    def __init__(self):
        self.calls = 0
        self._runtime_config = {"mode": "api", "base_url": "https://independent.invalid/v1",
                                "model_id": "synthetic-independent-rewriter"}

    def reply(self):
        self.calls += 1
        return LLMResponse("固定材料样本为20条，尚不足以断言普遍有效。[^e1]", usage=USAGE)


@pytest.mark.parametrize("interruption", ["adopted", "saved_before_adoption", "unknown_dispatch"])
def test_regenerated_real_feedback_cannot_purchase_second_durable_rewrite(interruption):
    request_a = repair_request(ORIGINAL, "reason-version-A")
    request_b = repair_request(ORIGINAL, "reason-version-B")
    assert request_a != request_b and "reason-version-B" in request_b[-1]["content"]
    state = AgentState(question=QUESTION, phase="finalize", answer=ORIGINAL, evidence=deepcopy(EVIDENCE),
        messages=[{"role": "user", "content": QUESTION}])
    model, durable = Rewriter(), []

    def invoke(request, call, before=lambda: None):
        return apply_citation_answer_rewrite(state, llm=model, request_messages=request,
            input_identity={"messages": [{"role": "user", "content": QUESTION}],
                "applied_input_ids": ["same-fixed-user-input"], "memory_fingerprint": "fixed"},
            call=call, persist=lambda: durable.append(deepcopy(state.snapshot())), before_review=before)

    def unknown():
        model.calls += 1
        raise Interruption("synthetic dispatch outcome lost")

    def after_response():
        if any(v.get("status") == "ready" for k, v in state.source_attribution_reviews.items()
               if k.startswith("citation_rewrite/")):
            raise Interruption("synthetic stop after durable complete response")

    if interruption == "unknown_dispatch":
        with pytest.raises(Interruption):
            invoke(request_a, unknown)
    elif interruption == "saved_before_adoption":
        with pytest.raises(Interruption):
            invoke(request_a, model.reply, after_response)
    else:
        assert invoke(request_a, model.reply)["status"] == "applied"
    state = AgentState.model_validate(durable[-1])
    result = invoke(request_b, lambda: pytest.fail("New bound feedback bought another rewrite"))
    assert len([k for k in state.source_attribution_reviews if k.startswith("citation_rewrite_context/")]) == 1
    assert len([k for k in state.source_attribution_reviews if k.startswith("citation_rewrite/")]) == 1
    assert model.calls == 1
    assert result["semantic_verification"] == "not_performed" and "passed" not in result
    if interruption == "unknown_dispatch":
        assert result["error_code"] == "dispatch_outcome_unknown"
        assert state.answer == ORIGINAL and state.usage["total_tokens"] == 0
    else:
        assert result["status"] == ("already_applied" if interruption == "adopted" else "applied")
        assert state.usage == USAGE and state.answer != ORIGINAL


def test_valid_json_with_tool_call_never_becomes_feedback_or_execution_authority():
    model = ScopeOracle("this-reason-must-not-be-trusted", tool_reply=True)
    mechanical = citation_gate(ORIGINAL, ["e1"])
    gate = scoped(model=model)
    assert gate["scope_review_failure"] == "response_tool_calls"
    assert gate["unsupported"] == mechanical["unsupported"]
    assert not build_citation_repair_feedback(ORIGINAL, gate, ["e1"])["available"]
    request = _citation_repair_messages([{"role": "user", "content": QUESTION}], ORIGINAL, gate,
        {"unsupported": []}, ["e1"])
    assert "this-reason-must-not-be-trusted" not in request[-1]["content"]
    assert all(not message.get("tool_calls") for message in request)
    assert len(model.calls) == 1
