"""Independent actual-executor unknown-scope regression; no paid requests."""
from copy import deepcopy

import pytest

from app.agent import loop
from app.agent.audit import review_citation_scope
from app.agent.checkpoint import AgentState
from app.goal_models import Goal
from app.rag.citation import citation_gate
from app.runtime_models import RunAttempt
from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint
from tests.test_research_children import parent


def test_scope_pending_at_ninety_percent_does_not_buy_other_repairs_or_complete_goal(db,monkeypatch):
    from app import run_store
    cid,evidence=_fixed_evidence(db)
    question="按附件说明编码方法并保留来源。"
    binding=parent(db,cid=cid,question=question)
    attempt=db.get(RunAttempt,binding["attempt_id"],populate_existing=True)
    state=AgentState.model_validate(attempt.checkpoint)
    state.runtime_config={**state.runtime_config,"mode":"api"}
    state.phase="finalize"
    state.answer="\n\n".join([evidence["quote"]+"[^e1]"]*9+["**附件编码方法的实际记录**"])
    state.evidence=[evidence];state.evidence_counter=1
    state.messages=[{"role":"user","content":question}]
    state.plan_source="requirements"
    class NoNewModelCalls:
        mode="api"
        def __init__(self):self._runtime_config=deepcopy(state.runtime_config);self.calls=[]
        def chat(self,*args,**kwargs):
            self.calls.append((args,kwargs))
            pytest.fail("Unknown scope dispatch was bypassed with another scope/marker/rewrite purchase")
    model=NoNewModelCalls()
    gate=citation_gate(state.answer,["e1"])
    assert gate["total_claims"]==10 and gate["marked_claims"]==9 and gate["grounded_ratio"]==.9
    durable_journals=[]
    def stop_after_pending():
        # Simulate process loss after committing intent. durable_save correctly
        # restores this interpreter's old in-memory journal when save raises;
        # recovery must load the separately committed checkpoint, not mistake
        # the rolled-back interpreter state for the durable pending record.
        durable_journals.append(deepcopy(state.source_attribution_reviews))
        raise OSError("Synthetic process loss after durable pending checkpoint")
    with pytest.raises(OSError):
        review_citation_scope(question,state.answer,state.evidence,gate,model,messages=state.messages,
            review_journal=state.source_attribution_reviews,persist=stop_after_pending,before_review=lambda:None)
    assert durable_journals
    state.source_attribution_reviews=deepcopy(durable_journals[-1])
    assert not model.calls and any(v.get("review_dispatch")=="pending"for v in state.source_attribution_reviews.values())
    monkeypatch.setattr(loop,"get_llm",lambda:model)
    # This isolates scope recovery from the separately tested source auditor.
    # The known source review is a synthetic local receipt, not quality evidence.
    monkeypatch.setattr(loop,"audit_answer",lambda *a,**k:{"mode":"synthetic_known_receipt","status":"passed",
        "passed":True,"verified":True,"unsupported":[],"known_failed_count":0,"unknown_count":0,"unsupported_ratio":0.0})
    monkeypatch.setattr(loop.hooks_store,"run_hooks",lambda *a,**kw:{"ran":0,"blocked":False,"feedback":""})
    saved=[]
    try:
        events=list(loop.run_agent(db,question,conversation_id=cid,run_id=binding["run_id"],
            checkpoint=_current_checkpoint(db,state),save_checkpoint=lambda value:saved.append(deepcopy(value))))
        result=next(value for kind,value in events if kind=="answer")
        assert not model.calls and not result["completed"] and result["outcome"]!="completed",result
        assert result["gate"]["error_code"]=="dispatch_outcome_unknown" and result["gate"]["grounded_ratio"]==.9
        assert not any(kind=="retry"for kind,_ in events)
        db.expire_all()
        assert db.get(Goal,binding["goal_id"]).outcome!="completed"
        assert saved and any(any(v.get("error_code")=="dispatch_outcome_unknown"for v in s["source_attribution_reviews"].values())for s in saved)
    finally:run_store.finish(binding["run_id"],"interrupted",attempt_id=binding["attempt_id"])
