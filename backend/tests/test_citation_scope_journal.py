"""Persistent scope dispatch mechanics only; no true provider evaluations."""
from copy import deepcopy
import json

import pytest

from app.agent.audit import review_citation_scope
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from app.rag.citation import citation_gate
from tests.test_citation_scope_receipts import messages
from tests.test_citation_scope_review import QUESTION, EVIDENCE, SOURCE, GENERAL
from tests.test_citation_review_diagnostics import provider  # noqa: F401 -- synthetic HTTP


ANSWER = SOURCE + "\n\n" + GENERAL
USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}


class Model:
    mode = "api"
    def __init__(self, *, raw=None, error=None):
        self.calls, self.raw, self.error = [], raw, error
    def chat(self, history, **kwargs):
        payload = json.loads(history[-1]["content"])
        self.calls.append(deepcopy(payload))
        if self.error:
            raise self.error
        results = [{"index": row["index"], "requires_citation": False,
                    "reason": "This general explanation is independent of the specified material."}
                   for row in payload["uncited_candidates"]]
        if "process_receipts" in payload:
            for row in results:
                row.update(kind="independent", process_scope="none", supports=[])
        return LLMResponse(self.raw if self.raw is not None else json.dumps({"results": results}), usage=USAGE)


def invoke(model, journal, *, question=QUESTION, answer=ANSWER, evidence=None, history=(),
           persist=lambda: None, before=lambda: None, usage=None, excluded=()):
    evidence = evidence if evidence is not None else EVIDENCE
    gate = citation_gate(answer, [e["id"] for e in evidence])
    return review_citation_scope(question, answer, evidence, gate, model, messages=list(history) if history is not None else None,
        review_journal=journal, persist=persist, before_review=before, on_usage=usage,
        excluded_indices=excluded)


def test_scope_intent_is_durable_before_dispatch_and_exact_result_reuses_without_usage():
    journal, durable, usage = {}, [], []
    class Checked(Model):
        def chat(self,*args,**kwargs):
            assert durable and any(v.get("review_dispatch")=="pending" for v in durable[-1].values())
            return super().chat(*args,**kwargs)
    model=Checked()
    result=invoke(model,journal,persist=lambda:durable.append(deepcopy(journal)),usage=usage.append)
    assert result["passed"] and len(model.calls)==1 and usage==[USAGE]
    assert len(journal)==1 and next(iter(journal)).startswith("citation_scope/")
    restored=AgentState.model_validate(AgentState(question=QUESTION,source_attribution_reviews=journal).snapshot())
    replay=invoke(model,restored.source_attribution_reviews,usage=usage.append)
    assert replay==result and len(model.calls)==1 and usage==[USAGE]


def test_pending_scope_dispatch_restores_unknown_without_retry_or_scope_pass():
    for history in (None,()):
        journal, durable, model = {}, [], Model()
        def fail_store():
            durable.append(deepcopy(journal))
            raise OSError("Synthetic persistence boundary before HTTP")
        with pytest.raises(OSError):invoke(model,journal,history=history,persist=fail_store)
        restored=json.loads(json.dumps(durable[-1]))
        value=invoke(model,restored,history=history)
        original=citation_gate(ANSWER,["e1"])
        assert not model.calls and value["unsupported"]==original["unsupported"] and not value["passed"]
        assert value["error_code"]=="dispatch_outcome_unknown" and "scoped_review"not in value


def test_known_result_saved_before_post_call_cancellation_can_resume_without_rebilling():
    journal,durable,model,usage={},[],Model(),[]
    def check():
        if any(value.get("scoped_review",{}).get("verified") for value in journal.values()):
            raise RuntimeError("Synthetic cancellation after saved receipt")
    with pytest.raises(RuntimeError,match="saved receipt"):
        invoke(model,journal,persist=lambda:durable.append(deepcopy(journal)),before=check,usage=usage.append)
    assert len(model.calls)==1 and usage==[USAGE]
    restored=deepcopy(durable[-1])
    assert invoke(model,restored,usage=usage.append)["passed"]
    assert len(model.calls)==1 and usage==[USAGE]
    def cancelled():raise RuntimeError("Synthetic cached cancellation")
    with pytest.raises(RuntimeError,match="cached cancellation"):invoke(model,restored,before=cancelled)
    assert len(model.calls)==1


def test_each_actual_prompt_evidence_instruction_or_model_change_creates_distinct_dispatch(monkeypatch):
    from app.agent import citation_scope
    mutations=[
        {"question":QUESTION+" Add an explanation."},
        {"answer":ANSWER+"\n\n另一个不依赖具体材料的一般说明足够完整。"},
        {"evidence":[{**EVIDENCE[0],"quote":"A different fixed version."}]},
        {"evidence":[{**EVIDENCE[0],"paper_title":"Another source title"}]},
        {"evidence":[{**EVIDENCE[0],"page":9}]},
        {"history":None},
        {"history":messages()},
    ]
    for changed in mutations:
        model,journal=Model(),{}
        invoke(model,journal)
        invoke(model,journal,**changed)
        assert len(model.calls)==2 and len(journal)==2,changed
    model,journal=Model(),{}
    invoke(model,journal)
    monkeypatch.setattr(citation_scope,"PROCESS_INSTRUCTIONS",citation_scope.PROCESS_INSTRUCTIONS+" A changed semantic instruction.")
    invoke(model,journal)
    assert len(model.calls)==2
    model,journal=Model(),{}
    model._runtime_config={"version":1,"mode":"api","model_id":"Exact-Scope-Model","profile_id":"scope-fixture",
        "profile_version":1,"credential_ref":"synthetic-scope-reference","base_url":"https://scope.example.test/v1"}
    invoke(model,journal)
    model._runtime_config["model_id"]="Changed-Scope-Model"
    invoke(model,journal)
    assert len(model.calls)==2


def test_process_receipt_range_and_exclusion_are_part_of_identity_but_unrelated_prose_is_not():
    model,journal=Model(),{}
    history=messages()
    invoke(model,journal,history=history)
    unrelated=deepcopy(history)+[{"role":"user","content":"Unrelated quoted text cannot become a process receipt."}]
    invoke(model,journal,history=unrelated)
    assert len(model.calls)==1
    changed=deepcopy(history);payload=json.loads(changed[2]["content"])
    payload["read_range"].update(line_end=2,next_line=3)
    changed[2]["content"]=json.dumps(payload)
    invoke(model,journal,history=changed)
    invoke(model,journal,history=history,excluded=(2,))
    assert len(model.calls)==3


def test_mutable_runtime_controls_do_not_repurchase_an_identical_scope_review():
    model,journal=Model(),{}
    model._runtime_config={"version":1,"mode":"api","model_id":"Exact-Scope-Model","profile_id":"scope-fixture",
        "profile_version":1,"credential_ref":"synthetic-scope-reference","base_url":"https://scope.example.test/v1"}
    invoke(model,journal)
    model._runtime_config.update(run_id="other-attempt",attempt_id="new-attempt",resource_budget_id="larger-budget",
        approval_mode="full_access",policy_revision=4)
    invoke(model,journal)
    assert len(model.calls)==1 and len(journal)==1


def test_invalid_or_unavailable_scope_response_is_retained_without_details_or_second_purchase():
    for model in (Model(raw="PRIVATE_PROVIDER_BODY"),Model(error=RuntimeError("PRIVATE_CREDENTIAL_DETAILS"))):
        journal,usage={},[]
        first=invoke(model,journal,usage=usage.append)
        second=invoke(model,journal,usage=usage.append)
        assert not first["passed"]and not second["passed"]and len(model.calls)==1
        assert "PRIVATE"not in json.dumps(journal)and "PRIVATE"not in json.dumps(second)
        assert len(usage)==(0 if model.error else 1)


def test_missing_persistence_callbacks_or_wrong_journal_never_dispatch():
    model=Model();gate=citation_gate(ANSWER,["e1"])
    for supplied in ({"review_journal":{}},{"review_journal":[],"persist":lambda:None,"before_review":lambda:None}):
        result=review_citation_scope(QUESTION,ANSWER,EVIDENCE,gate,model,**supplied)
        assert not result["passed"]and result["error_code"]=="scope_journal_unavailable"
    assert not model.calls


def test_corrupted_cached_decision_or_aggregate_never_waives_the_original_candidates():
    for mutation in ("passed","unknown_id","text","requires","scope","supports","exemption","missing"):
        model,journal=Model(),{}
        invoke(model,journal)
        saved=next(iter(journal.values()))
        if mutation=="passed":saved["passed"]=False
        elif mutation=="unknown_id":saved["scoped_review"]["decisions"][0]["index"]=9
        elif mutation=="text":saved["scoped_review"]["decisions"][0]["text"]="Wrong original candidate."
        elif mutation=="requires":saved["scoped_review"]["decisions"][0]["requires_citation"]="false"
        elif mutation=="scope":saved["scoped_review"]["decisions"][0]["kind"]="source"
        elif mutation=="supports":saved["scoped_review"]["decisions"][0]["supports"]=[{"receipt_id":"not-real","quote":"Invented process proof"}]
        elif mutation=="exemption":saved["scoped_review"]["exemptions"]=[]
        else:saved["scoped_review"]["decisions"]=[]
        result=invoke(model,journal)
        assert not result["passed"]and result["unsupported"]==[GENERAL]and len(model.calls)==1,mutation


def test_default_no_journal_legacy_behavior_and_usage_remain_unchanged():
    gate=citation_gate(ANSWER,["e1"]);model=Model(raw="not-json")
    assert review_citation_scope(QUESTION,ANSWER,EVIDENCE,gate,model)is gate
    usage=[];model=Model()
    first=review_citation_scope(QUESTION,ANSWER,EVIDENCE,gate,model,on_usage=usage.append)
    second=review_citation_scope(QUESTION,ANSWER,EVIDENCE,gate,model,on_usage=usage.append)
    assert first["passed"]and second["passed"]and len(model.calls)==2 and usage==[USAGE,USAGE]


def test_production_api_client_only_sends_one_synthetic_http_for_durable_scope_replay(db,provider):
    import httpx
    from app.llm import APIClient
    from app.call_ledger import summarize_calls
    responses,requests=provider
    responses.append(httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{
        "content":json.dumps({"results":[{"index":0,"requires_citation":False,"reason":"Independent general explanation.",
            "kind":"independent","process_scope":"none","supports":[]}]})}}],"usage":USAGE}))
    model,journal,usage=APIClient(synthetic=True),{},[]
    assert invoke(model,journal,usage=usage.append)["passed"]
    assert invoke(model,journal,usage=usage.append)["passed"]
    assert len(requests)==1 and usage==[USAGE]
    assert summarize_calls(db=db,include_synthetic=True)["synthetic_call_count"]==1


def test_scope_actual_admission_rejection_can_resume_after_budget_increase(db,provider):
    import httpx
    from app import resource_budget
    from app.call_ledger import list_calls
    from app.llm import APIClient
    from app.runtime_context import bind_runtime_config
    responses,requests=provider
    model,journal,durable=APIClient(synthetic=True),{},[]
    identity=resource_budget.create_budget(owner_run_id="scope-review-owner",amount_limit=".1",
        request_estimate=".2",price_version="synthetic-scope-estimate")
    with bind_runtime_config({"resource_budget_id":identity,"mode":"api",
            "base_url":"https://diagnostic.example.test/v1","model_id":"Exact-Diagnostic-Model"}):
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            invoke(model,journal,persist=lambda:durable.append(deepcopy(journal)))
        assert journal==durable[-1]=={} and not requests and not list_calls(include_synthetic=True)["items"]
        snapshot=resource_budget.budget_snapshot(identity)
        resource_budget.increase_budget(identity,amount_limit="1",expected_revision=snapshot["revision"])
        responses.append(httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{
            "content":json.dumps({"results":[{"index":0,"requires_citation":False,"reason":"Independent general explanation.",
                "kind":"independent","process_scope":"none","supports":[]}]})}}],"usage":USAGE}))
        assert invoke(model,journal,persist=lambda:durable.append(deepcopy(journal)))["passed"]
        assert invoke(model,journal)["passed"]
    assert len(requests)==1 and not responses
