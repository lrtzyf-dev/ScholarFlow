"""Missing markers must not hide already cited factual errors from correction."""
from copy import deepcopy

from app.agent import loop, audit
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint


def test_rewrite_receives_semantic_errors_even_while_another_sentence_lacks_marker(db, monkeypatch):
    cid, evidence = _fixed_evidence(db)
    bad = "实验模型采用99个注意力头完成编码处理。[^e1]\n\n实验记录还显示了其他尚未标注来源的数据。"
    good = "实验模型采用8个注意力头完成编码处理。[^e1]"
    state = AgentState(question="按附件说明编码方法并标明来源", conversation_id=cid, phase="finalize",
        answer=bad, evidence=[evidence], evidence_counter=1, plan_source="requirements",
        runtime_config={"mode": "api", "model_id": "synthetic"},
        messages=[{"role": "user", "content": "按附件说明编码方法并标明来源"}])
    seen, reviewed, quality_reviews = [], [], []
    class Model:
        mode = "api"
        def chat(self, messages, **kwargs):
            if "语义修订后最终答案的独立质量核验员" in messages[0]["content"]:
                # The real executor additionally checks task quality after the
                # one semantic rewrite. This separate synthetic response is
                # not a second rewrite and does not weaken source assertions.
                quality_reviews.append(deepcopy(messages))
                return LLMResponse('{"quality_issues":[]}')
            seen.append(deepcopy(messages))
            assert "99个注意力头" in messages[-1]["content"]
            assert "原文实际是8头" in messages[-1]["content"]
            assert "尚未标注来源" in messages[-1]["content"]
            return LLMResponse(good)
    def review(_db, answer, _evidence, **kwargs):
        reviewed.append(answer)
        failed = answer == bad
        return {"mode": "synthetic", "passed": not failed, "verified": True,
            "status": "failed" if failed else "passed", "unsupported_ratio": 1.0 if failed else 0.0,
            "known_failed_count": 1 if failed else 0, "unknown_count": 0,
            "unsupported": [{"text": "实验模型采用99个注意力头完成编码处理。", "reason": "原文实际是8头"}] if failed else []}
    monkeypatch.setattr(loop, "get_llm", Model)
    monkeypatch.setattr(loop, "audit_answer", review)
    monkeypatch.setattr(audit, "review_citation_scope", lambda question, answer, evidence, gate, llm, **kw: gate)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    result = next(value for kind, value in loop.run_agent(db, state.question, conversation_id=cid,
        checkpoint=_current_checkpoint(db, state)) if kind == "answer")
    assert reviewed == [bad, good] and len(seen) == 1
    assert len(quality_reviews) == 1
    assert result["completed"] and result["answer"] == good and not result["refusal"]
