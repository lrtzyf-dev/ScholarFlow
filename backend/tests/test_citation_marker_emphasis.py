"""Exact formatting-aware annotations, without relaxing source verification."""
from copy import deepcopy

import pytest

from app.agent import citation_marker_repair as repair
from app.rag.citation import MARKER_RE, citation_gate, parse_claims
from tests.test_citation_marker_repair import EVIDENCE, Model, edit, scoped


@pytest.mark.parametrize("wrapper", ["**", "__"])
@pytest.mark.parametrize("prefix", ["", "- ", "1. "])
def test_complete_emphasized_sentence_inserts_before_closing_delimiter(wrapper, prefix):
    body = "结论：这份附件不能支持任何普遍有效的推断。"
    text = prefix + wrapper + body + wrapper
    neighbor = "另一个已引用句子。[^e2]"
    answer = text + "\n\n" + neighbor
    gate = scoped(answer)
    assert gate["unsupported"] == [wrapper + body + (wrapper if prefix else "")]
    model = Model([edit(text)])
    result = repair.repair_citation_markers(answer, EVIDENCE, gate, model)
    expected = prefix + wrapper + body + "[^e1]" + wrapper + "\n\n" + neighbor
    assert result["changed"] and result["answer"] == expected, result
    assert MARKER_RE.sub("", result["answer"]) == MARKER_RE.sub("", answer)
    assert citation_gate(expected, ["e1", "e2"])["passed"]
    assert [c["evidence_ids"] for c in parse_claims(expected, ["e1", "e2"])] == [["e1"], ["e2"]]
    assert result["semantic_verification"] == "not_performed"
    record = result["edits"][0]
    assert record["end"] == len(text) and record["insert_at"] == len(text) - len(wrapper)
    replay = repair.reuse_citation_marker_annotation(answer, EVIDENCE, gate, result)
    assert replay["changed"] and replay["answer"] == expected
    tampered = deepcopy(result)
    tampered["edits"][0]["insert_at"] = len(text)
    assert not repair.reuse_citation_marker_annotation(answer, EVIDENCE, gate, tampered)["changed"]


@pytest.mark.parametrize("text", [
    "**第一项材料断言。第二项材料断言。**",
    "**材料断言。\n跨行内容。**",
    "**第一项**与**第二项材料断言。**",
    "**材料断言。_**",
])
def test_emphasis_never_expands_one_candidate_across_other_claims_or_markup(text):
    result = repair.repair_citation_markers(text, EVIDENCE, scoped(text), Model([edit(text)]))
    assert not result["changed"] and result["answer"] == text


@pytest.mark.parametrize("kind", ["process", "independent", "mixed", "uncertain"])
def test_emphasis_does_not_override_current_scope(kind):
    text = "**结论：这份附件不能支持任何普遍有效的推断。**"
    model = Model([edit(text)])
    result = repair.repair_citation_markers(text, EVIDENCE, scoped(text, kinds=[kind]), model)
    assert not result["changed"] and not model.calls


def test_long_whole_sentence_keeps_exact_text_and_rejects_truncated_prefix():
    sentence = "**材料明确限定" + "本次合成验收样本范围" * 12 + "。**"
    gate = scoped(sentence)
    assert len(gate["unsupported"][0]) == 80
    bad = repair.repair_citation_markers(sentence, EVIDENCE, gate, Model([edit(sentence[:80])]))
    assert not bad["changed"] and bad["answer"] == sentence
    good = repair.repair_citation_markers(sentence, EVIDENCE, gate, Model([edit(sentence)]))
    assert good["changed"] and good["answer"] == sentence[:-2] + "[^e1]**"


def test_emphasized_first_sentence_keeps_adjacent_sentence_and_its_evidence():
    first = "**结论：这份附件不能支持普遍有效性的推断。**"
    second = " 原文记录20个合成查询。[^e2]"
    answer = first + second
    result = repair.repair_citation_markers(answer, EVIDENCE, scoped(answer), Model([edit(first)]))
    assert result["changed"] and result["answer"] == first[:-2] + "[^e1]**" + second
    assert [c["evidence_ids"] for c in parse_claims(result["answer"], ["e1", "e2"])] == [["e1"], ["e2"]]


def test_underscore_only_markup_is_not_a_fact_but_real_underscore_identifiers_remain():
    assert citation_gate("__", ["e1"])["total_claims"] == 0
    assert parse_claims("- __材料事实。[^e1]__", ["e1"]) == [
        {"claim": "__材料事实。", "evidence_ids": ["e1"], "supported": True}]
    assert citation_gate("MODEL_ID为deepseek_flash。", ["e1"])["total_claims"] == 1


def test_control10_pattern_does_not_launder_uncited_mixed_followup_invitation():
    first = "**结论：这份附件不能支持任何“普遍有效”的推断。**"
    tail = "如果需要进一步加工，我可以把12/20的区间估计作为演示性计算单列。"
    answer = first + "\n\n" + tail
    gate = scoped(answer, kinds=["source", "mixed"])
    result = repair.repair_citation_markers(answer, EVIDENCE, gate, Model([edit(first)]))
    assert result["changed"] and result["missing_after"] == 1
    assert result["answer"].endswith(tail)
    assert citation_gate(result["answer"], ["e1", "e2"])["unsupported"] == [tail]
    assert result["semantic_verification"] == "not_performed"
