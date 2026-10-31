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
