from app.agent.execution import _citation_repair_messages
from app.agent.audit import _build_audit_text, audit_answer
from app.rag.citation import parse_claims
from app.llm import LLMResponse


def test_repair_sees_actual_rejected_draft_and_precise_feedback():
    history = [{"role": "user", "content": "比较样本"}, {"role": "tool", "content": "A=81, B=87", "tool_call_id": "t"}]
    draft = "A准确率81%，B准确率87%。差值为6个百分点。[^e1]"
    result = _citation_repair_messages(history, draft,
        {"unsupported": ["A准确率81%，B准确率87%。"], "invalid_markers": []}, {}, ["e1"])
    assert result[-2] == {"role": "assistant", "content": draft}
    assert "A准确率81%，B准确率87%。" in result[-1]["content"]
    assert "[^e1]" in result[-1]["content"]
    assert len(history) == 2


def test_repair_carries_explicit_semantic_reason_and_invalid_ids():
    result = _citation_repair_messages([], "已证明显著[^e9]", {"invalid_markers": ["e9"]},
        {"unsupported": [{"text": "已证明显著", "reason": "没有统计检验"}]}, ["e1"])
    assert "没有统计检验" in result[-1]["content"] and "e9" in result[-1]["content"]


def test_all_facts_in_cited_bullet_reach_audit_including_earlier_numbers(db):
    text = "- A样本量999，B样本量36。按原文准确率计算相差6个百分点。[^e1]"
    claims = parse_claims(text, ["e1"])
    assert len(claims) == 2 and "999" in claims[0]["claim"]
    assert all(c["evidence_ids"] == ["e1"] for c in claims)
    class Auditor:
        def chat(self, messages, **kwargs):
            assert "999" in messages[-1]["content"]
            return LLMResponse('{"results":[{"index":0,"supported":false,"reason":"原文A样本量24，不是999"},{"index":1,"supported":true}]}')
    audit = audit_answer(db, text, [{"id": "e1", "quote": "A样本量24，准确率81%；B样本量36，准确率87%。"}], llm=Auditor())
    assert not audit["passed"] and audit["unsupported_ratio"] == .5


def test_inherited_table_rows_are_audited_but_headers_and_prose_are_not_inherited():
    claims = parse_claims("|组别|样本量|\n|---|---|\n|A|24|\n|B|999[^e1]|\n\n另一句没有来源。", ["e1"])
    assert len(claims) == 2 and "999" in claims[1]["claim"]
    assert all(c["evidence_ids"] == ["e1"] for c in claims)


def test_list_intro_inheritance_does_not_leak_between_bullets():
    claims = parse_claims("- A是24。[^e1]\n- B是999。\n普通句没有标记。", ["e1"])
    assert len(claims) == 1


def test_audit_requires_correct_derived_numbers_and_does_not_infer_significance():
    prompt = _build_audit_text([{ "text": "按原文计算相差6个百分点", "evidence_ids": ["e1"]}],
        {"e1": {"quote": "81%和87%"}})
    assert "全部输入数值与单位正确计算" in prompt
    assert "不能由数值差推出统计显著" in prompt
