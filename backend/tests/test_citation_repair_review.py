"""Independent review of citation repair; no provider calls or real data."""
from copy import deepcopy
import json

import pytest

from app.agent import loop
from app.agent.checkpoint import AgentState
from app.agent.execution import _citation_repair_messages
from app.llm import LLMResponse
from app.models import Conversation
from app.rag.citation import citation_gate, parse_claims
from app import source_store as sources


FACT = "模型准确率达到99.8%，评测样本量达到5000个。"


@pytest.mark.parametrize("answer", [
    "实验报告给出的结论如下。[^e1]\n\n- " + FACT,
    "| " + FACT + "[^e1] | 方法比较 |\n| --- | --- |\n| 测试结果说明正文范围内的完整方法 | 原文方法 |",
    "模型准确率达到99.8%，评测样本量达到5000个，\n并在相同公开基准完成评测。[^e1]",
    "- " + FACT + "该模型使用了16个注意力头。[^e1]",
    "| 实验结果 | 说明 |\n| --- | --- |\n| " + FACT + "模型使用16个注意力头。[^e1] | 基准测试 |",
])
def test_every_numeric_claim_in_an_accepted_citation_scope_reaches_audit(answer):
    assert citation_gate(answer, ["e1"])["passed"] is True
    claims = parse_claims(answer, ["e1"])
    included = "\n".join(c["claim"] for c in claims if c["evidence_ids"] == ["e1"])
    assert "99.8%" in included and "5000" in included
    if "16" in answer:
        assert "16" in included


@pytest.mark.parametrize("header", [
    "模型准确率达到百分之九十九点八且评测样本量五千个",
    "该算法在全部公开基准均显著超过所有对比方法",
])
def test_table_header_fact_is_not_exempt_merely_because_it_has_no_arabic_digits(header):
    answer = f"| {header} | 方法比较 |\n| --- | --- |\n| 原文支持的方法事实内容。[^e1] | 原文方法 |"
    assert citation_gate(answer, ["e1"])["passed"] is True
    claims = parse_claims(answer, ["e1"])
    assert any(header in c["claim"] and c["evidence_ids"] == ["e1"] for c in claims)


def test_cited_heading_fact_reaches_audit_alongside_its_list():
    answer = "## " + FACT + "[^e1]\n\n- 该模型使用16个注意力头完成编码处理。"
    claims = parse_claims(answer, ["e1"])
    assert any("99.8%" in c["claim"] and "5000" in c["claim"] for c in claims)
    assert any("16" in c["claim"] and c["evidence_ids"] == ["e1"] for c in claims)


@pytest.mark.parametrize("answer", [
    "模型的准确率达到 `99.8%`，样本量为 `5000` 个。[^e1]",
    "- 模型的准确率达到 `99.8%`，样本量为 `5000` 个。[^e1]",
    "| 条目 | 数值 |\n| --- | --- |\n| 准确率与样本量 | `99.8%`，`5000` 个。[^e1] |",
])
def test_inline_numeric_values_remain_visible_to_the_semantic_auditor(answer):
    claims = parse_claims(answer, ["e1"])
    assert any("99.8%" in c["claim"] and "5000" in c["claim"] for c in claims)


@pytest.mark.parametrize("answer", [
    FACT + " `[^e1]`",
    FACT + " ``[^e1]``",
    FACT + r" \[^e1]",
])
def test_literal_or_escaped_marker_does_not_authorize_the_surrounding_claim(answer):
    gate = citation_gate(answer, ["e1"])
    assert gate["passed"] is False and gate["marked_claims"] == 0
    assert parse_claims(answer, ["e1"]) == []


def test_list_or_table_citation_cannot_silently_cover_later_prose():
    for prefix in ("- 源模型使用8个注意力头完成编码处理。[^e1]",
                   "| 方法 | 说明 |\n| --- | --- |\n| 编码器采用原文支持的注意力方法。[^e1] | 实验 |"):
        answer = prefix + "\n\n" + FACT
        gate = citation_gate(answer, ["e1"])
        assert not gate["passed"]
        assert any("99.8%" in s for s in gate["unsupported"])
        assert not any("99.8%" in c["claim"] for c in parse_claims(answer, ["e1"]))


def test_invalid_marker_stays_invalid_inside_a_cited_block():
    for answer in ("- " + FACT + "[^e999] 模型仍有其他限制。[^e1]",
                   "| 方法 | 数值 |\n|---|---|\n| " + FACT + "[^e999] | 模型仍有其他限制。[^e1] |"):
        gate = citation_gate(answer, ["e1"])
        assert not gate["passed"] and "e999" in gate["invalid_markers"]
        claims = parse_claims(answer, ["e1"])
        assert any(not c["supported"] for c in claims)


def test_repair_context_preserves_original_request_tools_draft_and_exact_feedback():
    messages = [{"role": "system", "content": "合成测试系统规则"}, {"role": "user", "content": "请依据原文说明方法并附来源"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {
            "name": "read_attached_source", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": '{"evidence":[{"id":"e1","quote":"真实合成原文"}]}'}]
    original = deepcopy(messages)
    rejected = FACT + "[^e999]"
    gate = {"unsupported": ["原文没有来源的具体结论"], "invalid_markers": ["e999"]}
    audit = {"unsupported": [{"text": FACT, "reason": "原文没有99.8%与5000个样本"}]}
    result = _citation_repair_messages(messages, rejected, gate, audit, ["e1"])
    assert messages == original and result[:len(original)] == original
    assert result[-2] == {"role": "assistant", "content": rejected}
    assert result[-1]["role"] == "user"
    feedback = result[-1]["content"]
    assert "原文没有来源的具体结论" in feedback and "原文没有99.8%与5000个样本" in feedback
    assert "- [^e1]" in feedback and "- [^e999]" not in feedback
    assert "伪造来源" in feedback


def test_repair_does_not_duplicate_an_already_present_draft():
    messages = [{"role": "user", "content": "合成原始问题"}, {"role": "assistant", "content": FACT}]
    result = _citation_repair_messages(messages, FACT, {}, {}, ["e1"])
    assert sum(m.get("role") == "assistant" and m.get("content") == FACT for m in result) == 1


def _fixed_evidence(db):
    cid, text = "citation-review", "模型采用8个注意力头完成编码处理。"
    db.add(Conversation(id=cid))
    scope = sources.SourceScope("conversation", cid)
    source = sources.capture_source(db, scope=scope, authorization=sources.SourceAuthorization(scope, "synthetic-source-approval", "upload"),
        kind="upload", title="Synthetic reference.txt", raw_bytes=text.encode(), text=text, media_type="text/plain")
    loc = sources.add_text_locator(db, source["revision_id"], scope=scope, line_start=1, line_end=1)
    db.commit()
    return cid, {"id": "e1", "source_id": source["source_id"], "revision_id": source["revision_id"],
        "locator_id": loc["locator_id"], "quote": text, "paper_title": "Synthetic reference", "paper_id": "", "page": 0}


def _current_checkpoint(db, state):
    # Build the same memory scope marker as normal admission; a hand-built
    # finalization fixture must not accidentally simulate revoked memory.
    from app.memory_governance import fingerprint
    current = fingerprint(db, state.conversation_id)
    state.memory_fingerprint = current
    state.messages[0].update(_task_root=True, _memory_fingerprint=current)
    return state.snapshot()


def test_repair_cannot_turn_a_fabricated_id_into_a_delivered_reference(db, monkeypatch):
    cid, evidence = _fixed_evidence(db)
    calls = []

    class Model:
        def chat(self, messages, **kwargs):
            calls.append((deepcopy(messages), kwargs))
            return LLMResponse(FACT + "[^e999]")

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    state = AgentState(phase="finalize", question="依据所选来源解释方法", conversation_id=cid,
        answer=FACT + "[^e999]", evidence=[evidence], evidence_counter=1,
        runtime_config={"mode": "api"}, messages=[{"role": "user", "content": "依据所选来源解释方法"}])
    events = list(loop.run_agent(db, state.question, conversation_id=cid, checkpoint=_current_checkpoint(db, state)))
    answer = next(e for k, e in events if k == "answer")
    assert answer["refusal"] == 1 and "e999" not in answer["answer"]
    assert answer["evidence"] == [evidence]
    assert len(calls) == 1 and calls[0][1].get("tools") is None
    assert calls[0][0][-2]["content"] == state.answer


def test_valid_repair_is_reaudited_and_cannot_mutate_fixed_source_quote(db, monkeypatch):
    cid, evidence = _fixed_evidence(db)
    calls, reviews = [], []
    corrected = evidence["quote"] + "[^e1]"

    class Model:
        def chat(self, messages, **kwargs):
            if messages[0]["content"] == "你是严谨的引用审计员，只输出 JSON。":
                reviews.append(deepcopy(messages))
                return LLMResponse(json.dumps({"results": [{"index": 0, "supported": len(reviews) > 1,
                    "reason":"Checked fixed source", "support_kind":"source", "process_scope":"none", "supports":[]}]}))
            calls.append((deepcopy(messages), kwargs))
            return LLMResponse(corrected)

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    state = AgentState(phase="finalize", question="依据所选来源解释方法", conversation_id=cid,
        answer="模型采用99个注意力头完成编码处理。[^e1]", evidence=[evidence], evidence_counter=1,
        runtime_config={"mode": "api"}, messages=[{"role": "user", "content": "依据所选来源解释方法"}])
    events = list(loop.run_agent(db, state.question, conversation_id=cid, checkpoint=_current_checkpoint(db, state)))
    answer = next(e for k, e in events if k == "answer")
    assert answer["refusal"] == 0 and answer["answer"] == corrected
    assert answer["audit"]["status"] == "passed" and len(reviews) == 2
    assert len(calls) == 1 and calls[0][1].get("tools") is None
    assert answer["evidence"] == [evidence]
    assert "99个" in calls[0][0][-2]["content"] and "8个" in reviews[-1][-1]["content"]
    scope = sources.SourceScope("conversation", cid)
    assert sources.read_locator(db, evidence["locator_id"], scope=scope)["quote"] == evidence["quote"]
