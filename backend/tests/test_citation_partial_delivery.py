"""Source-review regressions for useful drafts with localized review issues."""
from copy import deepcopy

import pytest

from app.agent import loop, audit
from app.agent.checkpoint import AgentState
from app.models import QARecord
from app.rag.citation import citation_gate
from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint


def test_sixty_one_cited_claims_and_one_uncertain_process_sentence_keep_answer(db, monkeypatch):
    cid, evidence = _fixed_evidence(db)
    limitation = "我没有检索到覆盖本书完整目录的片段，因此不作断言。"
    answer = "\n\n".join([evidence["quote"] + "[^e1]"] * 61 + [limitation])
    reviewed = []
    class NoRepairModel:
        mode = "api"
        def chat(self, *args, **kwargs):
            pytest.fail("An uncertain process sentence must not purchase a blind marker or answer rewrite")
    def scope(question, text, rows, gate, llm, **kwargs):
        assert text == answer and gate["marked_claims"] == 61
        return {**gate, "scoped_review": {"verified": True, "decisions": [{
            "index": 0, "text": limitation, "requires_citation": True,
            "kind": "uncertain", "reason": "缺少可核对的检索过程回执",
            "process_scope": "none", "supports": [],
        }]}}
    def semantic(_db, text, rows, **kwargs):
        reviewed.append(text)
        return {"mode": "synthetic", "status": "unverified", "passed": False,
            "verified": False, "known_failed_count": 0, "unknown_count": 1,
            "unsupported_ratio": 0.0, "unsupported": [], "factual_claims": 61,
            "unverified_count": 1, "unverified": [{"text": evidence["quote"],
                "reason": "一条引用的完整上下文尚待核对"}]}
    monkeypatch.setattr(loop, "get_llm", NoRepairModel)
    monkeypatch.setattr(audit, "review_citation_scope", scope)
    monkeypatch.setattr(loop, "audit_answer", semantic)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    state = AgentState(phase="finalize", question="给我输出原文能支持的论断", conversation_id=cid,
        answer=answer, evidence=[evidence], evidence_counter=1, plan_source="requirements",
        runtime_config={"mode": "api", "model_id": "synthetic"},
        messages=[{"role": "user", "content": "给我输出原文能支持的论断"}])
    saved = []
    events = list(loop.run_agent(db, state.question, conversation_id=cid,
        checkpoint=_current_checkpoint(db, state), save_checkpoint=lambda value: saved.append(deepcopy(value))))
    result = next(value for kind, value in events if kind == "answer")
    assert reviewed == [answer]
    assert result["answer"] == answer and result["refusal"] == 0
    assert result["outcome"] == "partial" and result["completed"] is False
    assert result["stop_reason"] == "citation_review_unresolved"
    assert result["gate"]["grounded_ratio"] == 61 / 62
    assert result["audit"]["known_failed_count"] == 0
    assert result["audit"]["unverified"] and result["gate"]["scoped_review"]["decisions"][0]["kind"] == "uncertain"
    assert not any(kind in {"refuse", "retry"} for kind, value in events)
    assert any(kind == "citation_review" and value["answer_preserved"] for kind, value in events)
    db.flush()  # This fixture's snapshot callback intentionally has no commit.
    assert db.get(QARecord, result["qa_id"]).answer == answer
    assert saved[-1]["phase"] == "completed"


def test_all_confirmed_false_claims_still_cannot_be_preserved_as_useful_output():
    from app.agent.execution import _can_preserve_citation_draft
    gate = citation_gate("模型使用99个注意力头完成编码。[^e1]", ["e1"])
    assert not _can_preserve_citation_draft(gate, {"known_failed_count": 1, "factual_claims": 1})
    assert not _can_preserve_citation_draft({**gate, "invalid_markers": ["e999"]}, {})


def test_confirmed_missing_and_uncertain_scope_findings_remain_distinct():
    from app.agent.execution import _citation_refusal_diagnostic
    uncertain = {"unsupported": ["未核实检索范围。"], "scoped_review": {"verified": True,
        "decisions": [{"text": "未核实检索范围。", "kind": "uncertain", "requires_citation": True}]}}
    result = _citation_refusal_diagnostic(uncertain, {"status": "unverified", "verified": False})
    assert result["reason_code"] == "scope_claims_unverified"
    assert "confirmed_missing_citations" not in result["reason_codes"]


def test_uncertain_scope_without_semantic_unknown_still_blocks_completion(db, monkeypatch):
    cid, evidence = _fixed_evidence(db)
    limitation = "我没有检索到完整目录的原文片段。"
    answer = "\n\n".join([evidence["quote"] + "[^e1]"] * 5 + [limitation])
    class NoModel:
        mode = "api"
        def chat(self, *args, **kwargs):
            pytest.fail("No repair should be bought for uncertainty alone")
    monkeypatch.setattr(loop, "get_llm", NoModel)
    monkeypatch.setattr(loop, "audit_answer", lambda *a, **kw: {
        "mode": "synthetic", "passed": True, "verified": True, "status": "passed",
        "unsupported": [], "known_failed_count": 0, "unknown_count": 0})
    monkeypatch.setattr(audit, "review_citation_scope", lambda q, text, rows, gate, llm, **kw: {
        **gate, "scoped_review": {"verified": True, "decisions": [{
            "text": limitation, "kind": "uncertain", "requires_citation": True, "reason": "范围尚未核实"}]}})
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    state = AgentState(phase="finalize", question="列出原文论断", conversation_id=cid,
        answer=answer, evidence=[evidence], evidence_counter=1, plan_source="requirements",
        runtime_config={"mode": "api"}, messages=[{"role": "user", "content": "列出原文论断"}])
    result = next(value for kind, value in loop.run_agent(db, state.question, conversation_id=cid,
        checkpoint=_current_checkpoint(db, state)) if kind == "answer")
    assert result["refusal"] == 0 and result["answer"] == answer
    assert result["completed"] is False and result["outcome"] == "partial"
