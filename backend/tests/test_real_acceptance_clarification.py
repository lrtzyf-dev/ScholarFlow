"""Expected missing-input mechanics; never real-model semantic acceptance."""
from copy import deepcopy
import json

import httpx
import pytest

from scripts import verify_real_acceptance as runner


def case():
    return deepcopy(next(c for c in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"]if c["id"]=="DOC09"))


def partial():
    answer="Please upload or explicitly select the paper before I can examine its page 7 table."
    return {"answer":answer,"evidence":[],"source_revision_ids":{},"tools":["list_attached_sources"],
        "turns":[{"http_status":200,"run_id":"chat_clarification_fixture","answer":answer,
            "outcome":"partial","completed":False,"refusal":False,"evidence":[],
            "incomplete_steps":[{"title":"Read the missing original paper table after the user supplies it",
                "phase":"read","status":"open","required":True}],
            "diagnostics":{"final_gate":{"status":"not_applicable","invalid_markers":[]},
                "final_audit":{"status":"unverified","known_failed_count":0}}}]}


def checks(result,definition=None):
    return runner.evaluate_receipts(definition or case(),result,None,"isolated","fixture-model")


def test_only_doc09_explicitly_expects_clarification_and_keeps_exact_user_rubric():
    cases=runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"]
    assert [c["id"]for c in cases if "expected_clarification"in c["machine_checks"]]==["DOC09"]
    expected=case()
    assert expected["machine_checks"]=={"expected_clarification":True}
    assert expected["prompts"]==["请查我刚才说的那篇论文第7页实验表，但这次没有上传文档。不要从别的会话猜是哪篇。"]
    assert expected["acceptance"]==["说明缺少可确认资料并请求用户选择或上传","不制造页码、表格或引用"]
    assert expected["manual_review_required"]is True and expected["execution_status"]=="not_run"


def test_explicit_missing_input_partial_is_machine_received_without_claiming_completion():
    value=partial()
    assert all(checks(value).values())
    assert value["turns"][0]["completed"]is False and value["turns"][0]["outcome"]=="partial"


@pytest.mark.parametrize("flag",[None,False,"true",1])
def test_ordinary_research_partial_or_non_boolean_flag_cannot_pass(flag):
    definition={"id":"ordinary","control":None,"machine_checks":{}}
    if flag is not None:definition["machine_checks"]["expected_clarification"]=flag
    assert not checks(partial(),definition)["answer_or_explicit_cancel"]


@pytest.mark.parametrize("change",[
    "empty_answer","whitespace_answer","refusal","completed","completed_outcome","missing_steps","empty_steps",
    "completed_step","cancelled_step","blank_step","missing_step_status","truncated_steps","evidence","selected_sources",
    "footnote","upper_marker","numeric_marker","bracket_marker","invalid_gate_marker","known_failed_audit","unsupported_audit",
    "deadline_exceeded","http_failure",
])
def test_expected_flag_does_not_admit_refusal_unproven_missing_work_or_invented_citation(change):
    value=partial();turn=value["turns"][0]
    if change=="empty_answer":turn["answer"]=""
    elif change=="whitespace_answer":turn["answer"]="  "
    elif change=="refusal":turn["refusal"]=True
    elif change=="completed":turn["completed"]=True
    elif change=="completed_outcome":turn["outcome"]="completed"
    elif change=="missing_steps":turn.pop("incomplete_steps")
    elif change=="empty_steps":turn["incomplete_steps"]=[]
    elif change=="completed_step":turn["incomplete_steps"][0]["status"]="completed"
    elif change=="cancelled_step":turn["incomplete_steps"][0]["status"]="cancelled"
    elif change=="blank_step":turn["incomplete_steps"][0]["title"]=" "
    elif change=="missing_step_status":turn["incomplete_steps"][0].pop("status")
    elif change=="truncated_steps":turn["incomplete_steps_truncated"]=True
    elif change=="evidence":turn["evidence"]=[{"id":"e1","revision_id":"invented","locator_id":"invented"}]
    elif change=="selected_sources":value["source_revision_ids"]={"invented.pdf":"invented"}
    elif change=="footnote":turn["answer"]+="[^e1]"
    elif change=="upper_marker":turn["answer"]+="[E1]"
    elif change=="numeric_marker":turn["answer"]+="[1]"
    elif change=="bracket_marker":turn["answer"]+="【1】"
    elif change=="invalid_gate_marker":turn["diagnostics"]["final_gate"]["invalid_markers"]=["e99"]
    elif change=="known_failed_audit":turn["diagnostics"]["final_audit"]["known_failed_count"]=1
    elif change=="unsupported_audit":turn["diagnostics"]["final_audit"]["unsupported"]=[{"text":"fabricated source claim"}]
    elif change=="deadline_exceeded":value["timeout"]={"deadline_exceeded":True}
    elif change=="http_failure":turn["http_status"]=500
    result=checks(value)
    assert result["answer_or_explicit_cancel"]is False and result["expected_clarification"]is False


def test_public_turn_keeps_real_bounded_missing_steps_without_copying_private_execution_state():
    raw={"kind":"answer","answer":"Public clarification","completed":False,"outcome":"partial","refusal":False,
        "incomplete_steps":[{"id":"requirement-1","requirement_id":"requirement-1","title":"Missing paper table",
            "phase":"read","status":"open","required":True,"reason":"needs_clarification",
            "tool_arguments":{"key":"PRIVATE-ARGUMENTS"},"agent_checkpoint":"PRIVATE-CHECKPOINT",
            "reasoning_content":"PRIVATE-REASONING","provider_context":"PRIVATE-CONTEXT"}]}
    response=httpx.Response(200,text="data:"+json.dumps(raw))
    result=runner.public_chat_turn("run","conversation",response=response)
    assert result["incomplete_steps"]==[{"id":"requirement-1","requirement_id":"requirement-1","title":"Missing paper table",
        "phase":"read","status":"open","reason":"needs_clarification","required":True}]
    assert result["incomplete_steps_truncated"]is False
    assert "PRIVATE-"not in json.dumps(result)
    raw["incomplete_steps"]*=70;raw["incomplete_steps"][0]["title"]="x"*5000
    result=runner.public_chat_turn("run","conversation",response=httpx.Response(200,text="data:"+json.dumps(raw)))
    assert len(result["incomplete_steps"])==64 and len(result["incomplete_steps"][0]["title"])==2000
    assert result["incomplete_steps_truncated"]is True


def test_historical_missing_steps_are_not_manufactured_from_clarification_prose():
    value=partial();value["turns"][0].pop("incomplete_steps")
    assert not checks(value)["expected_clarification"]
    response=httpx.Response(200,text="data:"+json.dumps({"kind":"answer","answer":value["answer"],"outcome":"partial","completed":False}))
    assert runner.public_chat_turn("run","conversation",response=response)["incomplete_steps"]==[]


def test_machine_received_clarification_does_not_replace_independent_human_review():
    from scripts.review_real_acceptance import reviewed_report
    definition=case();receipts=checks(partial())
    report={"kind":"real_acceptance_run","source_unchanged_during_evaluation":True,"cases":[{
        "id":"DOC09","status":"needs_semantic_review","acceptance":definition["acceptance"],
        "machine_receipts_passed":all(receipts.values()),"passed":False,"semantic_review":{"status":"pending","reviewer":None}}]}
    review={"reviewer":"Independent synthetic-mechanics reviewer","reviewer_kind":"agent",
        "independent_review_attestation":True,"case_id":"DOC09","decision":"pass",
        "rubric":[{"criterion":criterion,"passed":True,"note":"Synthetic mechanics only; not a real quality label."}
            for criterion in definition["acceptance"]]}
    value=reviewed_report(report,review,source_report_sha256="synthetic",review_sha256="synthetic")
    assert value["cases"][0]["status"]=="needs_human_review"
    assert value["semantic_acceptance"]=="pending_independent_human_review"
    assert value["passed"]is False and value["commercial_acceptance"]is False
