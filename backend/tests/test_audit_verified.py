"""语义审计 verified 标记：审计失败必须显式标注，不能静默放行。"""
from app.llm import LLMResponse

ANSWER = "The model uses 8 attention heads[^e1]."
EVIDENCE = [{"id": "e1", "quote": "The model uses 8 attention heads.",
             "paper_title": "Reference"}]


def test_llm_audit_success_is_verified(db):
    from app.agent.audit import audit_answer

    class Auditor:
        def chat(self, *a, **k):
            return LLMResponse('{"results": [{"index": 0, "supported": true}]}',
                               usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16})

    audit = audit_answer(db, ANSWER, EVIDENCE, llm=Auditor())
    assert audit["mode"] == "llm"
    assert audit["verified"] is True
    assert audit["passed"] is True
    assert audit["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}


def test_llm_exception_is_unavailable_and_not_verified(db):
    from app.agent.audit import audit_answer

    class Auditor:
        def chat(self, *a, **k):
            raise RuntimeError("provider down")

    audit = audit_answer(db, ANSWER, EVIDENCE, llm=Auditor())
    assert audit["mode"] == "unavailable"
    assert audit["verified"] is False
    assert audit["status"] == "unverified"
    assert audit["passed"] is False
    assert audit["unverified"]
    assert "error" in audit


def test_unparseable_llm_output_is_not_verified(db):
    from app.agent.audit import audit_answer

    class Auditor:
        def chat(self, *a, **k):
            return LLMResponse("sorry, I cannot produce JSON",
                               usage={"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11})

    audit = audit_answer(db, ANSWER, EVIDENCE, llm=Auditor())
    assert audit["mode"] == "unparseable"
    assert audit["verified"] is False
    assert audit["status"] == "unverified"
    assert audit["passed"] is False
    assert audit["unverified"]
    assert audit["usage"] == {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}


def test_string_false_is_not_coerced_to_supported(db):
    from app.agent.audit import audit_answer

    class Auditor:
        def chat(self, *a, **k):
            return LLMResponse('{"results": [{"index": 0, "supported": "false"}]}')

    audit = audit_answer(db, ANSWER, EVIDENCE, llm=Auditor())
    assert audit["status"] == "unverified"
    assert audit["passed"] is False
    assert audit["unsupported"] == []
    assert audit["unverified"][0]["text"] == "The model uses 8 attention heads."


def test_missing_duplicate_and_out_of_range_results_are_unverified(db):
    from app.agent.audit import audit_answer

    outputs = [
        '{"results": []}',
        '{"results": [{"index": 0, "supported": true}, {"index": 0, "supported": true}]}',
        '{"results": [{"index": 4, "supported": true}]}',
    ]
    for output in outputs:
        class Auditor:
            def chat(self, *a, **k):
                return LLMResponse(output)

        audit = audit_answer(db, ANSWER, EVIDENCE, llm=Auditor())
        assert audit["status"] == "unverified"
        assert audit["passed"] is False
        assert audit["verified"] is False
        assert audit["unverified"]


def test_invalid_index_types_are_unverified(db):
    from app.agent.audit import audit_answer

    class Auditor:
        def chat(self, *a, **k):
            return LLMResponse('{"results": [{"index": "0", "supported": true}]}')

    audit = audit_answer(db, ANSWER, EVIDENCE, llm=Auditor())
    assert audit["status"] == "unverified"
    assert audit["passed"] is False
    assert audit["unverified"]


def test_final_check_surfaces_unverified_status_and_manual_review(db):
    from app.agent.final_check import final_check

    class Auditor:
        def chat(self, *a, **k):
            raise RuntimeError("provider down")

    result = final_check(db, ANSWER, EVIDENCE, llm=Auditor())
    semantic = next(c for c in result["checks"] if c["name"] == "引用语义审计")
    assert result["status"] == "unverified"
    assert result["passed"] is None
    assert semantic["status"] == "unverified"
    assert semantic["passed"] is False
    assert any("需人工核对" in item["check"] for item in result["must_fix"])


def test_final_check_explicit_failure_takes_precedence_over_unverified(db):
    from app.agent.final_check import final_check

    class Auditor:
        def chat(self, *a, **k):
            return LLMResponse('{"results": [{"index": 0, "supported": false}]}')

    result = final_check(db, ANSWER, EVIDENCE, llm=Auditor())
    assert result["status"] == "failed"
    assert result["passed"] is False


def test_lexical_fallback_audit_is_verified(db):
    from app.agent.audit import audit_answer
    from app.llm import MockLLM

    audit = audit_answer(db, ANSWER, EVIDENCE, llm=MockLLM())
    assert audit["mode"] == "lexical"
    assert audit["verified"] is True
    assert audit["status"] in {"passed", "failed"}


def test_answer_result_carries_verified_audit(db, seeded_paper, monkeypatch):
    """端到端：正常交付时 answer 事件携带已验证的审计摘要。"""
    from app.agent import loop as loop_mod

    class FakeLLM:
        def stream_chat(self, messages, tools=None):
            if not any(m["role"] == "tool" for m in messages):
                yield ("final", LLMResponse(content="", tool_calls=[
                    {"id": "c1", "name": "retrieve_evidence",
                     "arguments": {"query": "attention heads", "k": 2}}]))
                return
            text = "模型使用 8 个注意力头 e1[^e1]"
            yield ("final", LLMResponse(content=text))

        def chat(self, *a, **k):
            return LLMResponse('{"results": [{"index": 0, "supported": true, "reason":"Original fixture", "support_kind":"source", "process_scope":"none", "supports":[]}]}')

    monkeypatch.setattr(loop_mod, "get_llm", lambda: FakeLLM())
    events = list(loop_mod.run_agent(db, "模型有多少注意力头？", paper_id=seeded_paper.id))
    answer = next(e for k, e in events if k == "answer")
    assert answer["audit"]["verified"] is True
    assert answer["audit"]["mode"] == "llm"
