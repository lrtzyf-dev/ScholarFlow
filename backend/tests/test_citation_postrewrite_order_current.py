"""Review the final annotation once, retaining full semantic/quality gates."""
from copy import deepcopy
import json

import pytest

from app.agent import audit, loop
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from tests.test_citation_marker_execution import HEADER, FACT, scope_gate
from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint


BAD = "模型采用99个注意力头完成编码处理。[^e1]"
REVISED = HEADER + "\n\n" + FACT
ANNOTATED = HEADER + "[^e1]\n\n" + FACT


@pytest.mark.parametrize("final_verdict", ["passed", "failed", "unverified"])
def test_postrewrite_annotation_precedes_single_final_full_audit(db, monkeypatch, final_verdict):
    cid, evidence = _fixed_evidence(db)
    question = "按附件解释编码方法并给出处"
    state = AgentState(question=question, conversation_id=cid, phase="finalize", answer=BAD,
        evidence=[evidence], evidence_counter=1, plan_source="requirements",
        runtime_config={"mode": "api", "model_id": "synthetic-postrewrite-order"},
        messages=[{"role": "user", "content": question}])
    reviews, operations = [], []

    class Model:
        mode = "api"

        def chat(self, messages, **kwargs):
            if "语义修订后最终答案的独立质量核验员" in messages[0]["content"]:
                operations.append("quality")
                return LLMResponse('{"quality_issues":[]}')
            if "candidates" in messages[-1]["content"]:
                operations.append("annotation")
                return LLMResponse(json.dumps({"edits": [{"candidate_index": 0,
                    "text": HEADER, "evidence_ids": ["e1"]}]}))
            operations.append("rewrite")
            assert operations.count("rewrite") == 1
            assert "原文实际是8头" in messages[-1]["content"]
            return LLMResponse(REVISED)

    def review(_db, answer, evidence, **kwargs):
        reviews.append(answer)
        operations.append("audit:" + ("initial" if answer == BAD else "final"))
        kind = "failed" if answer == BAD else final_verdict
        failed = kind == "failed"
        return {"mode": "synthetic", "passed": kind == "passed", "status": kind,
            "verified": kind != "unverified", "known_failed_count": int(failed),
            "unknown_count": int(kind == "unverified"), "unsupported_ratio": float(failed),
            "unsupported": [{"text": BAD if answer == BAD else "明确否决但不构成可删除的完整原句",
                "reason": "原文实际是8头" if answer == BAD else "synthetic final source rejection"}] if failed else []}

    monkeypatch.setattr(loop, "get_llm", Model)
    monkeypatch.setattr(loop, "audit_answer", review)
    monkeypatch.setattr(audit, "review_citation_scope",
        lambda question, answer, evidence, gate, llm, **kwargs: scope_gate(answer, evidence))
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False})
    events = list(loop.run_agent(db, question, conversation_id=cid,
        checkpoint=_current_checkpoint(db, state)))
    result = next(value for kind, value in events if kind == "answer")
    assert reviews == [BAD, ANNOTATED], "The intermediate rewrite must not buy a full audit before exact annotations"
    assert operations.index("annotation") < operations.index("audit:final")
    assert result["completed"] is (final_verdict == "passed")
    if final_verdict == "failed":
        assert result["refusal"] == 0
        assert result["outcome"] == "partial" and result["answer"] == ANNOTATED
        assert result["audit"]["unsupported"]
    else:
        assert result["answer"] == ANNOTATED
    assert operations.count("rewrite") == 1
    if final_verdict == "passed":
        assert operations.count("quality") == 1


@pytest.mark.parametrize("kind", ["semantic_pending", "marker_pending", "marker_unknown", "malformed"])
def test_early_annotation_admission_preserves_existing_paid_unknowns(kind):
    from app.agent.execution import _postrewrite_annotation_admitted
    state = AgentState(question="当前用户要求", messages=[{"role": "user", "content": "当前用户要求"}])
    if kind == "semantic_pending":
        state.citation_audit_batches["a" * 64] = {"protocol": "citation_semantic_batches_v1",
            "batches": {"0": {"state": "pending"}}}
    elif kind == "malformed":
        state.citation_audit_batches["broken"] = {}
    else:
        state.source_attribution_reviews["citation_marker/" + "b" * 64] = (
            {"review_dispatch": "pending"} if kind == "marker_pending" else {"error_code": "dispatch_outcome_unknown"})
    before = deepcopy(state.snapshot())
    assert _postrewrite_annotation_admitted(state) is False
    assert state.snapshot() == before


def test_early_annotation_admission_accepts_fresh_context_without_mutation():
    from app.agent.execution import _postrewrite_annotation_admitted
    state = AgentState(question="当前用户要求", messages=[{"role": "user", "content": "当前用户要求"}])
    before = deepcopy(state.snapshot())
    assert _postrewrite_annotation_admitted(state) is True
    assert state.snapshot() == before
