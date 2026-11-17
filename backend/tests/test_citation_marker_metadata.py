"""Server-verified locator context for exact marker annotation, synthetic only."""
from copy import deepcopy
import hashlib
import json

import pytest

from app import source_word_table_shapes
from app.agent import citation_marker_repair as repair
from app.agent.checkpoint import AgentState
from app.agent.citation_scope import verified_source_metadata
from app.agent.execution import _citation_marker_annotation
from app.agent.model_review import model_review_identity
from app.llm import LLMResponse
from app.source_documents import add_document_locator
from tests.test_citation_marker_execution import scope_gate
from tests.test_word_shape_citation_metadata import fixed_word


CLAIM = "该表共2行2列。"


def word_evidence(db):
    cid, tables, revision, scope = fixed_word(db)
    locator = add_document_locator(db, revision, scope=scope, kind="word_paragraphs",
        paragraph_start=1, paragraph_end=1)
    paragraph = {"id": "e1", "source_id": tables[0]["source_id"], "revision_id": revision,
        "locator_id": locator["locator_id"], "quote": locator["quote"],
        "source_location": deepcopy(locator["location"])}
    first, other = deepcopy(tables)
    first["id"], other["id"] = "e2", "e3"
    db.commit()
    return cid, [paragraph, first, other]


class MetadataModel:
    mode = "api"

    def __init__(self, chosen="e2"):
        self.calls = []
        self.chosen = chosen

    def chat(self, messages, **kwargs):
        prompt = json.loads(messages[-1]["content"])
        self.calls.append(deepcopy(prompt))
        metadata = prompt.get("source_metadata", {})
        assert "table_shape" not in metadata["e1"]
        assert metadata["e2"]["table_shape"]["complete"] is True
        assert metadata["e2"]["table_shape"]["row_count"] == 2
        assert metadata["e3"]["table_shape"]["row_count"] == 3
        return LLMResponse(json.dumps({"edits": [{"candidate_index": 0, "text": CLAIM,
            "evidence_ids": [self.chosen]}]}))


def test_real_executor_marker_request_has_verified_same_table_context(db):
    cid, evidence = word_evidence(db)
    state = AgentState(question="说明首表行列数", conversation_id=cid, answer=CLAIM, evidence=evidence)
    model = MetadataModel()
    result = _citation_marker_annotation(state, scope_gate(CLAIM, evidence), model, db=db,
        persist=lambda: None, before_review=lambda: None, on_usage=lambda _: None)
    assert result["changed"] and state.answer == CLAIM + "[^e2]"
    assert result["semantic_verification"] == "not_performed"


def annotate(db, state, model, *, persist=lambda: None):
    return _citation_marker_annotation(state, scope_gate(state.answer, state.evidence), model, db=db,
        persist=persist, before_review=lambda: None, on_usage=lambda _: None)


class CaptureModel:
    mode = "api"

    def __init__(self, chosen=None):
        self.calls = []
        self.chosen = chosen

    def chat(self, messages, **kwargs):
        self.calls.append({"prompt": json.loads(messages[-1]["content"]), "instructions": messages[0]["content"]})
        return LLMResponse(json.dumps({"edits": [] if self.chosen is None else [{
            "candidate_index": 0, "text": CLAIM, "evidence_ids": [self.chosen]}]}))


@pytest.mark.parametrize("forged",["shape", "location", "metadata"])
def test_evidence_fields_cannot_supply_forged_metadata(db, forged):
    cid, evidence = word_evidence(db)
    if forged == "shape":evidence[0]["table_shape"]={"row_count":999,"column_count":999,"complete":True}
    elif forged == "location":evidence[1]["source_location"]={"part_name":"fake.xml","table_path":"fake-table"}
    else:evidence[0]["source_metadata"]={"table_shape":{"row_count":999,"complete":True}}
    state=AgentState(question="首表尺寸", conversation_id=cid, answer=CLAIM,evidence=evidence)
    model=MetadataModel()
    assert annotate(db,state,model)["changed"]
    payload=model.calls[0]
    assert "999" not in json.dumps(payload["source_metadata"])
    assert payload["source_metadata"]["e2"]["location"]["part_name"]=="word/document.xml"
    assert "table_shape"not in payload["source_metadata"]["e1"]


@pytest.mark.parametrize("field,value",[("revision_id","other-version"),("raw_sha256","0"*64),
    ("part_name","word/header1.xml"),("table_path","/document/body/tbl[9]")])
def test_invalid_cross_identity_shape_is_not_supplied_to_marker(db,monkeypatch,field,value):
    cid,evidence=word_evidence(db)
    original=source_word_table_shapes.verified_word_table_shape
    def wrong(*args,**kwargs):
        shape=original(*args,**kwargs);shape[field]=value;return shape
    monkeypatch.setattr(source_word_table_shapes,"verified_word_table_shape",wrong)
    state=AgentState(question="首表尺寸",conversation_id=cid,answer=CLAIM,evidence=evidence)
    model=CaptureModel()
    assert not annotate(db,state,model)["changed"]
    metadata=model.calls[0]["prompt"]["source_metadata"]
    assert all("table_shape"not in metadata[eid]for eid in ("e2","e3"))


def incomplete_policy(monkeypatch):
    original=source_word_table_shapes.verified_word_table_shape
    def incomplete(*args,**kwargs):
        shape=original(*args,**kwargs)
        shape.update(complete=False,shape_kind="unknown",reason="synthetic_policy_change")
        shape.pop("row_count",None);shape.pop("column_count",None)
        return shape
    monkeypatch.setattr(source_word_table_shapes,"verified_word_table_shape",incomplete)


def test_incomplete_shape_is_explicitly_unknown_and_not_treated_as_total_dimensions(db,monkeypatch):
    cid,evidence=word_evidence(db);incomplete_policy(monkeypatch)
    state=AgentState(question="首表尺寸",conversation_id=cid,answer=CLAIM,evidence=evidence)
    model=CaptureModel()
    result=annotate(db,state,model)
    assert not result["changed"]
    request=model.calls[0]
    shape=request["prompt"]["source_metadata"]["e2"]["table_shape"]
    assert shape["complete"]is False and "row_count"not in shape
    assert "单元格坐标只给尺寸下界"in request["instructions"]
    assert "complete=true"in request["instructions"]


def test_replay_rejects_metadata_changed_or_old_protocol_receipt(db,monkeypatch):
    cid,evidence=word_evidence(db)
    metadata=verified_source_metadata(db,evidence,cid)
    result=repair.repair_citation_markers(CLAIM,evidence,scope_gate(CLAIM,evidence),MetadataModel(),source_metadata=metadata)
    assert repair.reuse_citation_marker_annotation(CLAIM,evidence,scope_gate(CLAIM,evidence),result,source_metadata=metadata)["changed"]
    legacy=deepcopy(result);legacy.pop("annotation_protocol");legacy.pop("source_metadata_sha256")
    assert not repair.reuse_citation_marker_annotation(CLAIM,evidence,scope_gate(CLAIM,evidence),legacy,source_metadata=metadata)["changed"]
    incomplete_policy(monkeypatch)
    changed=verified_source_metadata(db,evidence,cid)
    assert not repair.reuse_citation_marker_annotation(CLAIM,evidence,scope_gate(CLAIM,evidence),result,source_metadata=changed)["changed"]


def marker_legacy_key(state,model):
    return "citation_marker/"+hashlib.sha256(json.dumps({"protocol":"exact_citation_marker_annotation_v1",
        "answer":state.answer,"evidence":state.evidence,"review_model_identity":model_review_identity(model)},
        ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


@pytest.mark.parametrize("state_kind",["pending","unknown"])
@pytest.mark.parametrize("generation",["legacy","metadata"])
def test_unknown_dispatch_fences_metadata_and_protocol_upgrades(db,monkeypatch,state_kind,generation):
    cid,evidence=word_evidence(db)
    state=AgentState(question="首表尺寸",conversation_id=cid,answer=CLAIM,evidence=evidence)
    model=CaptureModel("e2")
    if generation=="legacy":key=marker_legacy_key(state,model)
    else:
        key=marker_legacy_key(state,model)+"/"+repair._evidence_digest({"protocol":repair.ANNOTATION_PROTOCOL,
            "source_metadata":verified_source_metadata(db,evidence,cid)})
    state.source_attribution_reviews[key]={"review_dispatch":"pending"}if state_kind=="pending"else{
        "error_code":"dispatch_outcome_unknown","changed":False}
    incomplete_policy(monkeypatch)
    result=annotate(db,state,model)
    assert result["error_code"]=="dispatch_outcome_unknown"and not model.calls and state.answer==CLAIM


def test_known_result_not_reused_after_metadata_change(db,monkeypatch):
    cid,evidence=word_evidence(db)
    state=AgentState(question="首表尺寸",conversation_id=cid,answer=CLAIM,evidence=evidence)
    model=CaptureModel("e2");durable=[]
    assert annotate(db,state,model,persist=lambda:durable.append(state.snapshot()))["changed"]
    before_adoption=next(s for s in durable if s["answer"]==CLAIM
        and any(r.get("changed")for r in s["source_attribution_reviews"].values()))
    restored=AgentState.model_validate(before_adoption);incomplete_policy(monkeypatch)
    model.chosen=None
    result=annotate(db,restored,model)
    assert not result["changed"]and restored.answer==CLAIM and len(model.calls)==2
    assert len(restored.source_attribution_reviews)==2


def test_model_can_still_choose_wrong_paragraph_and_annotation_does_not_certify_semantics(db):
    cid,evidence=word_evidence(db)
    state=AgentState(question="首表尺寸",conversation_id=cid,answer=CLAIM,evidence=evidence)
    result=annotate(db,state,CaptureModel("e1"))
    assert result["changed"]and state.answer==CLAIM+"[^e1]"
    assert result["semantic_verification"]=="not_performed"and "passed"not in result
    metadata=verified_source_metadata(db,evidence,cid)
    assert "table_shape"not in metadata["e1"]


def test_stale_raw_hash_is_rejected_before_marker_dispatch(db):
    from app.source_models import SourceSnapshot,SourceRevision
    from sqlalchemy import update
    cid,evidence=word_evidence(db)
    snapshot_id=db.get(SourceRevision,evidence[0]["revision_id"]).snapshot_id
    db.execute(update(SourceSnapshot).where(SourceSnapshot.id==snapshot_id).values(raw_sha256="0"*64))
    db.commit();db.expire_all()
    state=AgentState(question="首表尺寸",conversation_id=cid,answer=CLAIM,evidence=evidence)
    model=CaptureModel("e2")
    with pytest.raises(Exception):annotate(db,state,model)
    assert not model.calls


@pytest.mark.parametrize("chosen,completed",[("e2",True),("e1",False)])
def test_full_execution_reaudits_real_word_metadata_annotations(db,monkeypatch,chosen,completed):
    from app.agent import loop,audit
    from tests.test_citation_repair_review import _current_checkpoint
    cid,evidence=word_evidence(db)
    state=AgentState(phase="finalize",question="说明首表行列数",conversation_id=cid,
        answer=CLAIM,evidence=evidence,evidence_counter=3,
        runtime_config={"mode":"api","model_id":"synthetic"},plan_source="requirements",
        messages=[{"role":"user","content":"说明首表行列数"}])
    model=CaptureModel(chosen);reviewed=[]
    def review(_db,answer,_evidence,**kwargs):
        reviewed.append(answer)
        unknown="[^e1]"in answer
        return {"mode":"synthetic","passed":not unknown,"verified":not unknown,
            "status":"unverified"if unknown else"passed","unsupported_ratio":0.0,
            "known_failed_count":0,"unknown_count":int(unknown),"unverified_count":int(unknown),
            "unsupported":[],"unverified":[{"text":CLAIM,"reason":"Paragraph e1 has no table proof"}]if unknown else[]}
    monkeypatch.setattr(loop,"get_llm",lambda:model)
    monkeypatch.setattr(loop,"audit_answer",review)
    monkeypatch.setattr(audit,"review_citation_scope",
        lambda question,answer,evidence,gate,llm,**kw:scope_gate(answer,evidence))
    monkeypatch.setattr(loop.hooks_store,"run_hooks",lambda *a,**kw:{"ran":0,"blocked":False,"feedback":""})
    events=list(loop.run_agent(db,state.question,conversation_id=cid,
        checkpoint=_current_checkpoint(db,state),save_checkpoint=lambda _:None))
    result=next(value for kind,value in events if kind=="answer")
    assert reviewed==[CLAIM,CLAIM+f"[^{chosen}]"]
    assert result["completed"]is completed
    assert result["answer"]==CLAIM+f"[^{chosen}]"
    assert len(model.calls)==1 and model.calls[0]["prompt"]["source_metadata"]["e2"]["table_shape"]["row_count"]==2
    if not completed:
        assert result["outcome"]=="partial"and result["audit"]["unknown_count"]==1
