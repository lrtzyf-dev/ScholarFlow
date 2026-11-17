"""Legacy presentation hints cannot remove cited sentences from review."""
from app.llm import LLMResponse
from app.rag.citation import is_meta_statement

EVIDENCE = [{"id": "e1", "quote": "The model uses 8 attention heads.",
             "paper_title": "Reference"}]


def test_meta_classifier_recognizes_process_and_limits():
    meta = [
        "我另外检索了其他关键词，返回的仍是同一批片段。",
        "第 8 页几乎无法提取有效文本，只能得到零散的表格残片。",
        "任务书中没有给出具体到几点几分的时间点，本文档中未找到。",
        "如需精确到时刻，建议以大赛报名官网通知为准。",
    ]
    assert all(is_meta_statement(s) for s in meta)


def test_meta_classifier_keeps_factual_claims():
    factual = [
        "模型使用 8 个注意力头。",
        "截止时间为 2026 年 10 月 20 日。",
        "该模型在验证集上的准确率达到 89.4%。",
    ]
    assert not any(is_meta_statement(s) for s in factual)


def test_audit_includes_process_claim_and_reports_unverified_without_its_proof(db):
    from app.agent.audit import audit_answer

    answer = (
        "模型使用 8 个注意力头。[^e1] "
        "我另外检索了其他关键词，返回的是同一批片段。[^e1]"
    )

    class Auditor:
        def chat(self, *a, **k):
            return LLMResponse('{"results": [{"index": 0, "supported": true}, {"index": 1, "supported": "unknown"}]}')

    audit = audit_answer(db, answer, EVIDENCE, llm=Auditor())
    assert audit["mode"] == "llm"
    assert audit["factual_claims"] == 2
    assert audit["unsupported_ratio"] == 0.0
    assert audit["passed"] is False and audit["unknown_count"] == 1


def test_audit_known_failure_survives_an_unknown_process_claim(db):
    from app.agent.audit import audit_answer

    answer = (
        "模型完全没有任何注意力头。[^e1] "
        "我另外检索了其他关键词，返回的是同一批片段。[^e1]"
    )

    class Auditor:
        def chat(self, *a, **k):
            return LLMResponse('{"results": [{"index": 0, "supported": false}, {"index": 1, "supported": "unknown"}]}')

    audit = audit_answer(db, answer, EVIDENCE, llm=Auditor())
    assert audit["factual_claims"] == 2
    assert audit["unsupported_ratio"] == 0.5
    assert audit["known_failed_count"] == audit["unknown_count"] == 1
    assert audit["passed"] is False


def test_audit_cannot_pass_process_claim_without_a_reviewer(db):
    from app.agent.audit import audit_answer

    answer = "我另外检索了其他关键词，返回的是同一批片段。[^e1]"
    audit = audit_answer(db, answer, EVIDENCE, llm=object())
    assert audit["mode"] == "unavailable"
    assert audit["factual_claims"] == 1
    assert audit["passed"] is False and audit["verified"] is False
