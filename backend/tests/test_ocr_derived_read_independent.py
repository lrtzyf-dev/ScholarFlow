"""Independent local OCR read authorization: synthetic retained data only."""
from copy import deepcopy
import hashlib
import json

import pytest
from sqlalchemy import event

from app import source_store,source_tools,source_runtime
from app.agent import readonly_cache
from app.goal_ledger import ensure_run_goal
from app.ledger_models import CallRecord
from app.models import Conversation
from app.runtime_context import bind_runtime_config
from app.runtime_models import RuntimeRun,RunAttempt
from app.service_tool_models import ServiceToolInvocation
from app.source_documents import read_document,save_extraction
from app.source_models import SourceExtraction
from app.source_parsing import Extraction,extract
from app.timeutil import utcnow
from tests.test_ocr_derived_source_read import completed_ocr,read
from tests.test_deepseek_service_tools import CID,binding,network


def assert_denied(payload,data):
    assert payload.get("error")and payload.get("text_read")is False,payload
    assert "Retained OCR line 121:"not in json.dumps(payload)
    assert len(data["mock_http"])==1


def new_attempt(db,data,*,run_id=None,conversation_id=CID):
    binding=deepcopy(data["binding"])
    next_id="b"*32 if run_id is None else "c"*32
    if run_id is None:
        run=db.get(RuntimeRun,binding["run_id"])
        old=db.get(RunAttempt,binding["attempt_id"])
        old.status="interrupted";old.ended_at=utcnow()
        number=2
    else:
        goal=ensure_run_goal(db,run_id,conversation_id,"Independent resume authorization boundary")
        run=RuntimeRun(id=run_id,kind="foreground",conversation_id=conversation_id,
            goal_id=goal["id"],status="running")
        db.add(run);binding.update(run_id=run_id,goal_id=goal["id"],conversation_id=conversation_id)
        number=1
    run.current_attempt_id=next_id
    db.add(RunAttempt(id=next_id,run_id=run.id,number=number,
        writer_token="independent-ocr-read",status="running"))
    binding["attempt_id"]=next_id;db.commit()
    return binding


def test_same_logical_run_resumes_in_new_attempt_without_recharging_or_reusing_old_identity(db,completed_ocr):
    data=completed_ocr;cache={}
    first=read(db,data,cache=cache)
    assert not first.get("error")and len(cache)==1
    binding=new_attempt(db,data)
    actions=[]
    def actual():
        actions.append(True)
        return source_tools.execute_source_tool(db,"read_attached_source",
            {"source_revision_id":data["derived_id"],"line_start":121,"line_end":130},conversation_id=CID)
    resumed=read(db,data,binding=binding,cache=cache,action=actual)
    assert not resumed.get("error")and len(actions)==1
    local=resumed["source"]["local_read_authorization"]
    assert local["producing_attempt_id"]==data["binding"]["attempt_id"]
    assert local["reading_attempt_id"]==binding["attempt_id"]
    assert local["authorizes_paid_calls"]is False and local["authorizes_external_transfer"]is False
    assert binding["source_revision_ids"]==data["binding"]["source_revision_ids"]
    assert len(data["mock_http"])==1


def test_same_conversation_new_logical_run_does_not_inherit_ocr_read_permit(db,completed_ocr):
    data=completed_ocr;binding=new_attempt(db,data,run_id="independent-different-logical-run")
    assert_denied(read(db,data,binding=binding),data)


def test_different_conversation_cannot_reuse_frozen_source_selection(db,completed_ocr):
    data=completed_ocr;other="independent-other-conversation"
    db.add(Conversation(id=other));db.commit()
    binding=new_attempt(db,data,run_id="independent-other-conversation-run",conversation_id=other)
    with bind_runtime_config(binding):
        payload=json.loads(source_tools.execute_source_tool(db,"read_attached_source",
            {"source_revision_id":data["derived_id"],"line_start":121,"line_end":130},conversation_id=other))
    assert_denied(payload,data)


def test_forged_valid_source_and_extraction_cannot_borrow_a_completed_service_receipt(db,completed_ocr):
    data=completed_ocr;scope=source_store.SourceScope("conversation",CID)
    original=source_store.read_revision(db,data["derived_id"],scope=scope,include_raw=True)
    doc=deepcopy(db.get(SourceExtraction,data["derived_id"]).document)
    forged=source_store.capture_source(db,scope=scope,
        authorization=source_store.SourceAuthorization(scope,"service:"+data["receipt_id"],original["kind"]),
        kind=original["kind"],title="Independent forged service link.pdf",raw_bytes=original["raw_bytes"],
        text=original["text"],media_type=original["media_type"],origin_ref=original["origin_ref"],
        text_origin=original["text_origin"],parser=original["parser"])
    save_extraction(db,forged["revision_id"],scope=scope,
        extraction=Extraction(original["text"],original["parser"],doc))
    db.commit()
    assert read_document(db,forged["revision_id"],scope=scope)["text"]==original["text"]
    assert_denied(read(db,{**data,"derived_id":forged["revision_id"]}),data)


@pytest.mark.parametrize("mutation",["endpoint","request_id","missing_call"])
def test_cached_local_body_is_evicted_when_its_actual_call_receipt_is_no_longer_bound(db,completed_ocr,mutation):
    data=completed_ocr;cache={}
    assert not read(db,data,cache=cache).get("error")and cache
    receipt=db.get(ServiceToolInvocation,data["receipt_id"],populate_existing=True)
    call=db.get(CallRecord,receipt.call_id,populate_existing=True)
    if mutation=="endpoint":call.endpoint="https://different.invalid/v1/tools/ocr"
    if mutation=="request_id":call.request_id="independent-unrelated-operation"
    if mutation=="missing_call":db.delete(call)
    db.commit()
    def forbidden():pytest.fail("invalid local OCR grant cannot execute or return a cached action")
    with pytest.raises(source_store.SourceAccessDenied):read(db,data,cache=cache,action=forbidden)
    assert not cache
    assert_denied(read(db,data),data)


def test_producing_attempt_must_exist_even_when_service_and_call_receipts_agree(db,completed_ocr):
    data=completed_ocr
    receipt=db.get(ServiceToolInvocation,data["receipt_id"],populate_existing=True)
    call=db.get(CallRecord,receipt.call_id,populate_existing=True)
    receipt.attempt_id=call.attempt_id="e"*32
    db.commit()
    assert db.get(RunAttempt,"e"*32)is None
    assert_denied(read(db,data),data)


def test_original_pdf_and_ocr_text_keep_separate_read_representations(db,completed_ocr,monkeypatch):
    data=completed_ocr;real=source_store.add_pdf_locator;seen=[]
    def pdf(db,rid,**kwargs):
        seen.append(rid)
        assert rid==data["original"]["revision_id"],"OCR derivative must not be parsed as original PDF text layer"
        return real(db,rid,**kwargs)
    monkeypatch.setattr(source_store,"add_pdf_locator",pdf)
    with bind_runtime_config(data["binding"]):
        original=json.loads(source_tools.execute_source_tool(db,"read_attached_source",
            {"source_revision_id":data["original"]["revision_id"],"page_index":0},conversation_id=CID))
        wrong=json.loads(source_tools.execute_source_tool(db,"read_attached_source",
            {"source_revision_id":data["derived_id"],"page_index":0},conversation_id=CID))
    assert not original.get("error")and original["read_contract"]["method"]=="pdfplumber_text_layer"
    assert seen==[data["original"]["revision_id"]]
    assert_denied(wrong,data)
    assert read(db,data)["read_contract"]["method"]=="retained_ocr_text_lines"


def test_local_read_observes_only_credential_metadata_and_never_reveals_a_service_key(db,completed_ocr):
    data=completed_ocr;queries=[]
    def observed(connection,cursor,statement,parameters,context,executemany):queries.append(statement)
    event.listen(db.get_bind(),"before_cursor_execute",observed)
    try:payload=read(db,data,cache={})
    finally:event.remove(db.get_bind(),"before_cursor_execute",observed)
    assert not payload.get("error")
    credential_queries=[statement for statement in queries if "service_tool_credentials"in statement.lower()]
    assert all("protected_value"not in statement.lower()for statement in credential_queries)
    assert len(data["mock_http"])==1


def test_restored_cache_cannot_replace_retained_ocr_quote_even_with_self_consistent_body_hash(db,completed_ocr):
    data=completed_ocr;cache={}
    assert not read(db,data,cache=cache).get("error")
    entry=next(iter(cache.values()))
    payload=json.loads(entry["result"])
    payload["evidence"][0]["quote"]="FORGED-LOCAL-OCR-CACHE-BODY"
    payload["locator"]["quote"]="FORGED-LOCAL-OCR-CACHE-BODY"
    entry["result"]=json.dumps(payload,ensure_ascii=False)
    entry["result_sha256"]=hashlib.sha256(entry["result"].encode()).hexdigest()
    result=read(db,data,cache=cache)
    assert not result.get("error")
    assert "FORGED-LOCAL-OCR-CACHE-BODY"not in json.dumps(result)
    assert "Retained OCR line 121:"in result["evidence"][0]["quote"]
    assert len(data["mock_http"])==1


def test_ordinary_txt_cache_cannot_replace_fixed_quote_with_a_self_hashed_body(db,binding):
    scope=source_store.SourceScope("conversation",CID)
    raw="Fixed ordinary TXT line one.\nFixed ordinary TXT line two.\nFixed ordinary TXT line three.".encode()
    extraction=extract(raw,"text/plain")
    source=source_store.capture_source(db,scope=scope,
        authorization=source_store.SourceAuthorization(scope,"independent-txt-upload","upload"),
        kind="upload",title="Independent fixed ordinary.txt",raw_bytes=raw,text=extraction.text,
        media_type="text/plain",text_origin="parsed_text",parser=extraction.parser)
    save_extraction(db,source["revision_id"],scope=scope,extraction=extraction);db.commit()
    frozen=source_runtime.freeze_sources(db,CID,[source["revision_id"]],binding)
    arguments={"source_revision_id":source["revision_id"],"line_start":1,"line_end":3};cache={}
    def actual():return source_tools.execute_source_tool(db,"read_attached_source",arguments,conversation_id=CID)
    with bind_runtime_config(frozen):
        first=json.loads(readonly_cache.attached_read(db,arguments,binding=frozen,
            conversation_id=CID,cache=cache,action=actual))
        quote=first["evidence"][0]["quote"];identity=deepcopy(next(iter(cache.values()))["identity"])
        entry=next(iter(cache.values()));payload=json.loads(entry["result"])
        payload["evidence"][0]["quote"]="FORGED-ORDINARY-TXT-CACHE-BODY"
        payload["locator"]["quote"]="FORGED-ORDINARY-TXT-CACHE-BODY"
        entry["result"]=json.dumps(payload,ensure_ascii=False)
        entry["result_sha256"]=hashlib.sha256(entry["result"].encode()).hexdigest()
        assert entry["identity"]==identity
        assert source_store.read_locator(db,first["locator"]["locator_id"],scope=scope)["quote"]==quote
        second=json.loads(readonly_cache.attached_read(db,arguments,binding=frozen,
            conversation_id=CID,cache=cache,action=actual))
    assert "FORGED-ORDINARY-TXT-CACHE-BODY"not in json.dumps(second)
    assert second["evidence"][0]["quote"]==quote
