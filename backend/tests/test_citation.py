"""引用闸门测试。"""
from app.rag.citation import citation_gate, parse_claims


def test_gate_accepts_marked_claims():
    answer = "模型使用 8 个注意力头。[^e1] 翻译任务达到 28.4 BLEU。[^e2]"
    gate = citation_gate(answer, ["e1", "e2"])
    assert gate["passed"] is True
    assert gate["total_claims"] == 2
    claims = parse_claims(answer, ["e1", "e2"])
    assert len(claims) == 2
    assert all(c["supported"] for c in claims)


def test_gate_rejects_unmarked_claim():
    answer = "模型使用 8 个注意力头。[^e1] 它在所有任务上都显著优于所有竞品。"
    gate = citation_gate(answer, ["e1"])
    assert gate["passed"] is False
    assert len(gate["unsupported"]) == 1


def test_gate_rejects_invalid_marker():
    answer = "模型使用 8 个注意力头。[^e9]"
    gate = citation_gate(answer, ["e1"])
    assert gate["passed"] is False
    assert gate["invalid_markers"] == ["e9"]


def test_gate_retains_heading_and_table_content_for_typed_scope_review():
    """Content-bearing structure is a candidate; syntax-only lines are not."""
    answer = (
        "# 文档标题\n"
        "## 小节标题\n"
        "模型使用 8 个注意力头。[^e1]\n"
        "| 列一 | 列二 |\n|---|---|\n| 数据 | 内容 |\n"
        "[^e1]: 第 1 页 — 证据说明"
    )
    gate = citation_gate(answer, ["e1"])
    assert gate["invalid_markers"] == []
    assert gate["unsupported"] == ["# 文档标题", "## 小节标题", "| 列一 | 列二 |", "| 数据 | 内容 |"]
    assert gate["passed"] is False


def test_gate_list_inherits_intro_marker():
    """引导句带标记时，紧随其后的列表项整体继承引用。"""
    answer = (
        "提交物料包括以下内容。[^e1]\n"
        "- 最终开放的技能包目录结构\n"
        "- 完整的对话日志文件记录\n"
        "- 成果分析报告正文内容"
    )
    gate = citation_gate(answer, ["e1"])
    assert gate["unsupported"] == []
    assert gate["passed"] is True
    assert gate["marked_claims"] == 4


def test_gate_preserves_inline_hash_and_does_not_grant_prefix_exemptions():
    """Scope of process/independent sentences requires actual review."""
    answer = (
        "我先确认可用论文。# 研究选题头脑风暴与实验证据详细分析。\n"
        "**说明**：文本层存在排版噪声，部分句子被截断。"
    )
    gate = citation_gate(answer, ["e1"])
    assert gate["unsupported"] == ["我先确认可用论文。", "# 研究选题头脑风暴与实验证据详细分析。",
        "**说明**：文本层存在排版噪声，部分句子被截断。"]
    assert gate["passed"] is False


def test_gate_grounded_ratio_reports_partial():
    """严格口径仍判失败，但输出溯源比例供 finalize 判定。"""
    answer = "模型使用 8 个注意力头。[^e1] 它在所有任务上都显著优于所有竞品。"
    gate = citation_gate(answer, ["e1"])
    assert gate["passed"] is False
    assert gate["grounded_ratio"] == 0.5


def test_gate_unmarked_paragraph_fails():
    """完全无标记的事实段落仍判失败（抗幻觉底线）。"""
    answer = "该模型在所有基准上都取得了最优成绩并且没有任何局限。"
    gate = citation_gate(answer, ["e1"])
    assert gate["passed"] is False
    assert gate["grounded_ratio"] == 0.0
    assert len(gate["unsupported"]) == 1
