"""Real fabricated XLSX/locators; semantic reviewer outputs are synthetic."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.audit import audit_answer, review_citation_scope
from app.agent.citation_scope import verified_source_metadata
from app.llm import LLMResponse
from app.main import app
from app.models import Conversation
from app.source_runtime import freeze_sources
from app.source_tools import execute_source_tool
from app.runtime_context import bind_runtime_config
from scripts.verify_real_acceptance import fixture_bytes
from tests.test_citation_scope_receipts import Reviewer, FACT, EVIDENCE, messages


def table(db):
    cid = "audit-fixed-xlsx"
    db.add_all([Conversation(id=cid), Conversation(id="audit-other")]); db.commit()
    with TestClient(app) as client:
        response = client.post("/api/sources/upload", files={"file":("budget.xlsx",fixture_bytes("budget.xlsx"))},
            data={"conversation_id":cid,"request_id":"evidence-contract"})
        assert response.status_code == 200, response.text
        rid = response.json()["revision_id"]
    binding = freeze_sources(db, cid, [rid], {"conversation_id":cid})
    history, evidence = [], []
    with bind_runtime_config(binding):
        for number, (tool, cell_range) in enumerate([("read_attached_source","A1:C4"), ("verify_attached_table","B2:B4")],1):
            args={"source_revision_id":rid,"sheet":"Budget","cell_range":cell_range}
            result=json.loads(execute_source_tool(db,tool,args,conversation_id=cid))
            assert not result.get("error"), result
            history += [{"role":"assistant","tool_calls":[{"id":f"call{number}","type":"function",
                "function":{"name":tool,"arguments":json.dumps(args)}}]},
                {"role":"tool","tool_call_id":f"call{number}","content":json.dumps(result,ensure_ascii=False)}]
            evidence += [{**e,"id":f"e{number}"} for e in result["evidence"]]
    db.commit()
    return cid, history, evidence


def audit_result(index, supported=True, kind="source", supports=None):
    return {"index":index,"supported":supported,"reason":"Synthetic explicit source/process contract",
        "support_kind":kind,"process_scope":"bounded" if kind in ("process","mixed") else "none",
        "supports":supports or []}


def test_actual_table_metadata_preserves_units_coordinates_and_missing_cache_without_trusting_answer(db):
    cid, history, evidence = table(db)
    evidence[0]["table_context"]={"cells":[{"cell":"B4","cached_value":999}]}
    evidence[0]["source_location"]={"sheet":"Forged", "table_semantics":{"formula_recalculation_performed":True}}
    metadata=verified_source_metadata(db,evidence,cid)
    cells={cell["cell"]:cell for cell in metadata["e1"]["cells"]}
    assert cells["A2"]["value"]=="equipment" and cells["C2"]["value"]=="CNY"
    assert cells["B4"]["formula"]=="=SUM(B2:B3)" and cells["B4"]["cache_state"]=="missing"
    assert cells["B4"]["cached_value"]is None and cells["B4"]["cache_verified"]is False
    assert metadata["e1"]["location"]["sheet"]=="Budget"
    assert all(cell.get("cached_value")!=999 for cell in metadata["e1"]["cells"])
    assert {c["cell"] for c in metadata["e2"]["cells"]}=={"B2","B3","B4"}
    with pytest.raises(Exception):verified_source_metadata(db,evidence,"audit-other")


def test_actual_xlsx_and_e_public_formula_process_claims_reach_distinct_audit_inputs(db):
    cid, history, evidence=table(db)
    answer=("设备equipment为1200 CNY，耗材supplies为350 CNY，单位相同，差额850 CNY由二者计算所得。[^e1]\n\n"
        "B4的缓存值为空，cache_state为missing，不能把它说成零。[^e1]\n\n"
        "该复算工具的执行记录自述其能力限制：非完整Excel引擎，不支持外部工作簿、宏、命名区域、动态数组与易失函数，且不做单位换算。[^e2]")
    class Model:
        def chat(self, prompt, **kwargs):
            text=prompt[1]["content"]
            assert '"cell": "B4"'in text and '"cache_state": "missing"'in text and '"cached_value": null'in text
            assert "source_metadata="in text and "process_receipts="in text
            from app.agent.citation_scope import process_receipts
            receipts,_=process_receipts(history)
            actual=next(r for r in receipts if r["tool_name"]=="verify_attached_table")
            limitation_proof = [
                {"receipt_id": actual["id"], "quote": json.dumps(value, ensure_ascii=False)}
                for value in json.loads(actual["content"])["result"]["limitations"]
            ]
            limitation_proof.append({"receipt_id": actual["id"], "quote": '"unit_conversions_applied": false'})
            return LLMResponse(json.dumps({"results":[audit_result(0,kind="derived"),audit_result(1),
                audit_result(2,kind="process",supports=limitation_proof)]}))
    value=audit_answer(db,answer,evidence,llm=Model(),messages=history,conversation_id=cid)
    assert value["passed"]and value["status"]=="passed",value


@pytest.mark.parametrize("mutation",["unpaired","quote","missing","mixed"])
def test_process_or_mixed_audit_support_never_comes_from_fake_or_missing_receipts(db,mutation):
    cid, history, evidence=table(db)
    from app.agent.citation_scope import process_receipts
    actual=next(r for r in process_receipts(history)[0] if r["tool_name"]=="verify_attached_table")
    supports=[{"receipt_id":actual["id"],"quote":actual["content"]}]
    if mutation=="unpaired":history[-1]["tool_call_id"]="not-real"
    if mutation=="quote":supports[0]["quote"]="fabricated capability proof"
    if mutation=="missing":history=[]
    if mutation=="mixed":supports=[]
    class Model:
        def chat(self,*args,**kwargs):
            return LLMResponse(json.dumps({"results":[audit_result(0,kind="mixed"if mutation=="mixed"else"process",supports=supports)]}))
    value=audit_answer(db,"本次实际执行了公式复算并确认所选范围全部通过。[^e2]",evidence,llm=Model(),messages=history,conversation_id=cid)
    assert not value["passed"]and value["status"]=="unverified"and value["unverified"]


def test_explicit_unsupported_process_remains_failed_without_needing_a_success_receipt(db):
    cid, history, evidence=table(db)
    class Model:
        def chat(self,*args,**kwargs):return LLMResponse(json.dumps({"results":[audit_result(0,False,kind="process")]}))
    value=audit_answer(db,"已经完整核验整个工作簿并证明所有公式均正确。[^e1]",evidence,llm=Model(),messages=[],conversation_id=cid)
    assert value["status"]=="failed"and value["unsupported"]and not value["unverified"]


def test_safe_scope_failure_reason_has_no_response_or_private_provider_detail():
    answer=FACT+"\n\n本次工具已成功完成全部计算并返回核查结果。"
    from app.rag.citation import citation_gate
    gate=citation_gate(answer,["e1"])
    value=review_citation_scope("Q",answer,EVIDENCE,gate,Reviewer(support=[{"receipt_id":"made-up","quote":"PRIVATE-PROVIDER-SECRET"}]),messages=messages())
    assert value["scope_review_failure"]=="receipt"and "PRIVATE"not in json.dumps(value)
    assert "scope_review_failure"not in gate


def test_explicit_uncertainty_is_unknown_instead_of_a_known_false_claim(db):
    class Model:
        def chat(self,*args,**kwargs):
            return LLMResponse(json.dumps({"results":[audit_result(0,False,kind="uncertain")]}))
    value=audit_answer(db,FACT,EVIDENCE,llm=Model(),messages=[])
    assert value["status"]=="unverified"and value["known_failed_count"]==0 and value["unknown_count"]==1
    assert not value["unsupported"]and value["unverified"]


@pytest.mark.parametrize("scope",[None,"not-a-real-scope"])
def test_false_result_with_invalid_scope_is_unknown_not_a_known_rejection(db,scope):
    result=audit_result(0,False)
    result["process_scope"]=scope
    class Model:
        def chat(self,*args,**kwargs):return LLMResponse(json.dumps({"results":[result]}))
    value=audit_answer(db,FACT,EVIDENCE,llm=Model(),messages=[])
    assert value["status"]=="unverified"and value["known_failed_count"]==0 and value["unknown_count"]==1


def test_full_e_doc05_draft_is_reviewed_with_actual_table_metadata_and_receipts(db):
    cid,history,evidence=table(db)
    fixture=json.loads((Path(__file__).parent/"fixtures/citation-e-public-drafts.json").read_text(encoding="utf-8"))
    draft=next(c for c in fixture["cases"]if c["id"]=="DOC05")["drafts"][1]
    class Model:
        def chat(self,prompt,**kwargs):
            text=prompt[1]["content"]
            # The whole actual public draft is visible to semantic review;
            # source cells/cache and actual execution are separate contracts.
            assert json.dumps(draft,ensure_ascii=False)in text
            assert '"cache_state": "missing"'in text and '"cell": "C2"'in text
            assert '"tool_name": "verify_attached_table"'in text
            return LLMResponse('{"results":[]}')
    result=audit_answer(db,draft,evidence,llm=Model(),messages=history,conversation_id=cid)
    assert result["status"]=="unverified"and result["known_failed_count"]==0
    assert result["unknown_count"]==result["factual_claims"]and result["unknown_count"]>5


def test_full_e_doc12_draft_keeps_effect_inferences_citable_despite_own_analysis_label():
    from app.rag.citation import citation_gate
    fixture=json.loads((Path(__file__).parent/"fixtures/citation-e-public-drafts.json").read_text(encoding="utf-8"))
    draft=next(c for c in fixture["cases"]if c["id"]=="DOC12")["drafts"][1]
    gate=citation_gate(draft,["e1","e2"])
    class Model(Reviewer):
        def chat(self,prompt,**kwargs):
            data=json.loads(prompt[1]["content"])
            assert data["full_answer"]==draft
            results=[]
            for item in data["uncited_candidates"]:
                derived="不能合并为同一效应"in item["text_prefix"]or"可以合并的是一种"in item["text_prefix"]
                results.append({"index":item["index"],"requires_citation":derived,"reason":"Synthetic explicit boundary for preserved real draft",
                    "kind":"derived"if derived else"independent","process_scope":"none","supports":[]})
            return LLMResponse(json.dumps({"results":results}))
    result=review_citation_scope("比较两个附件摘要",draft,[{"id":"e1","quote":"40 participants improved"},
        {"id":"e2","quote":"18 participants did not find improvement"}],gate,Model(),messages=[])
    assert len(result["unsupported"])==2 and not result["passed"]
    assert all("合并"in text for text in result["unsupported"])


def test_rewrite_uses_final_audit_even_when_missing_source_sentence_remains(db,monkeypatch):
    from app.agent import loop
    from app.agent.checkpoint import AgentState
    from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint
    cid, evidence=_fixed_evidence(db)
    source_sentence=evidence["quote"]+"[^e1]"
    rewritten=source_sentence+"\n\n不能合并为同一效应层面的结论。"
    observed=[]
    class Model:
        mode="api"
        def chat(self,prompt,**kwargs):
            if "引用适用范围审查员"in prompt[0]["content"]:
                data=json.loads(prompt[1]["content"])
                return LLMResponse(json.dumps({"results":[{"index":item["index"],"requires_citation":True,"reason":"材料推导仍需引用",
                    "kind":"derived","process_scope":"none","supports":[]}for item in data["uncited_candidates"]]}))
            if prompt[0]["content"]=="你是严谨的引用审计员，只输出 JSON。":
                observed.append(prompt[1]["content"])
                return LLMResponse(json.dumps({"results":[audit_result(0,len(observed)>1)]}))
            return LLMResponse(rewritten)
    monkeypatch.setattr(loop,"get_llm",lambda:Model())
    monkeypatch.setattr(loop.settings,"memory_capture",False)
    state=AgentState(phase="finalize",question="解释来源方法",conversation_id=cid,
        answer="模型采用99个注意力头完成编码处理。[^e1]",evidence=[evidence],evidence_counter=1,
        runtime_config={"mode":"api"},messages=[{"role":"user","content":"解释来源方法"}])
    events=list(loop.run_agent(db,state.question,conversation_id=cid,checkpoint=_current_checkpoint(db,state)))
    result=next((value for kind,value in events if kind=="answer"),None)
    assert result is not None,[(kind,value.get("text"))for kind,value in events]
    assert len(observed)==2 and "99个"in observed[0]and "不能合并为同一效应"in observed[1]
    assert result["audit"]["status"]=="passed"and result["refusal"]==1
    assert result["gate"]["unsupported"]==["不能合并为同一效应层面的结论。"]


@pytest.mark.parametrize("known_failure",[True,False])
def test_unknown_audit_item_does_not_erase_known_rejection_or_invent_one(db,monkeypatch,known_failure):
    from app.agent import loop
    from app.agent.checkpoint import AgentState
    from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint
    cid,evidence=_fixed_evidence(db)
    text="模型采用99个注意力头完成编码处理。[^e1]\n\n该模型在所有数据集上都有可靠改进。[^e1]"
    class Model:
        def chat(self,prompt,**kwargs):
            if prompt[0]["content"]=="你是严谨的引用审计员，只输出 JSON。":
                return LLMResponse(json.dumps({"results":[audit_result(0,not known_failure),
                    {"index":1,"supported":"unknown"}]}))
            return LLMResponse(text)
    monkeypatch.setattr(loop,"get_llm",lambda:Model())
    monkeypatch.setattr(loop.settings,"memory_capture",False)
    state=AgentState(phase="finalize",question="解释来源方法",conversation_id=cid,answer=text,
        evidence=[evidence],evidence_counter=1,runtime_config={"mode":"api"},
        messages=[{"role":"user","content":"解释来源方法"}])
    events=list(loop.run_agent(db,state.question,conversation_id=cid,checkpoint=_current_checkpoint(db,state)))
    result=next(value for kind,value in events if kind=="answer")
    assert result["audit"]["verified"]is False and result["audit"]["unknown_count"]==1
    assert result["audit"]["known_failed_count"]==int(known_failure)
    assert result["audit"]["unsupported_ratio"]==(.5 if known_failure else 0)
    # A known rejection is retained as a sentence-level issue; it must not
    # erase the otherwise useful answer or turn the whole turn into a refusal.
    assert result["refusal"] == 0
    assert result["completed"] is False and result["outcome"] == "partial"
    if known_failure:
        assert result["audit"]["unsupported"]
