"""Literal source titles must not erase claims or their real citations."""
import pytest

from app.rag.citation import citation_gate, parse_claims


@pytest.mark.parametrize("quoted", [
    '"# Fabricated alpha A" 与 "# Fabricated alpha B"',
    '“# Fabricated alpha A” 与 “# Fabricated alpha B”',
    "'# Fabricated alpha A' 与 '# Fabricated alpha B'",
    '`# Fabricated alpha A` 与 `# Fabricated alpha B`',
])
def test_quoted_hash_titles_keep_markers_and_reach_semantic_audit(quoted):
    answer = f"标题分别为 {quoted} [^e1][^e2]，两份材料均记录了研究对象。"
    gate = citation_gate(answer, ["e1", "e2"])
    assert gate["passed"] and gate["marked_claims"] == 1
    claims = parse_claims(answer, ["e1", "e2"])
    assert len(claims) == 1
    assert "# Fabricated alpha A" in claims[0]["claim"]
    assert "# Fabricated alpha B" in claims[0]["claim"]
    assert claims[0]["evidence_ids"] == ["e1", "e2"]


def test_exact_f_public_rewrite_title_is_not_truncated():
    answer = ('两份附件各自只有 3 行，我已完整读取这两份附件。标题分别为 '
        '"# Fabricated alpha A" 与 "# Fabricated alpha B" [^e1][^e2]，'
        '因此这两份材料的内容带有占位或示意性质，下文只复述其原文表述。')
    gate = citation_gate(answer, ["e1", "e2"])
    assert not any('标题分别为' in text for text in gate["unsupported"])
    title = next(c for c in parse_claims(answer, ["e1", "e2"]) if "标题分别为" in c["claim"])
    assert '# Fabricated alpha B' in title["claim"] and title["supported"]


@pytest.mark.parametrize("answer", [
    '标题写为 "# 该模型在所有任务上显著优于所有竞品"。',
    '实验报告结果。# 准确率达到99.8%且样本量为5000人。',
    '模型达到99.8%。[^e1] # 后续测试同样在全部任务实现最优性能。',
])
def test_hash_cannot_hide_an_uncited_fact(answer):
    gate = citation_gate(answer, ["e1"])
    assert not gate["passed"] and gate["unsupported"]


def test_real_heading_lines_are_still_structure_and_cited_heading_reaches_audit():
    answer = '# 研究标题\n\n## 正文小节\n\n模型使用8个注意力头完成编码处理。[^e1]'
    assert citation_gate(answer, ["e1"])["passed"]
    cited = '## 模型使用8个注意力头完成编码处理。[^e1]'
    assert any('8个' in c["claim"] for c in parse_claims(cited, ["e1"]))


def test_hash_does_not_remove_an_invalid_marker():
    answer = '标题分别为 "# Fabricated alpha A" 与 "# Fabricated alpha B" [^e9]。'
    gate = citation_gate(answer, ["e1"])
    assert not gate["passed"] and gate["invalid_markers"] == ["e9"]
