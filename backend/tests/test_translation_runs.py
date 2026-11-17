"""Durable translation lifecycle; synthetic responses, no supplier requests."""
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app import research_runs as runs,run_store,source_store as sources,model_profiles
from app.agent.checkpoint import AgentState
from app.db import SessionLocal
from app.models import Conversation,DeletedConversation
from app.runtime_models import RuntimeRun,RunAttempt
from app.runtime_context import current_runtime_config
from app.llm import LLMResponse
from app.translate import translation_run
from tests.test_translation_sources import client as source_client, fixed as upload_fixed

CID="translation-run-a"


@pytest.fixture
def scope(db):
    db.add_all([Conversation(id=CID,title="Translation"),Conversation(id="translation-run-b",title="B")]);db.commit()
    runs._stopping.clear()
    yield sources.SourceScope("conversation",CID)
    for ident in list(run_store.active_runs()):run_store.finish(ident,"interrupted")


def cloud(db):
    p=model_profiles.save_profile(db,{"name":"Synthetic translate","base_url":"https://translation.synthetic.test/v1",
        "model_id":"Translation-Bound-Case","api_key":"synthetic-translation-key"})
    model_profiles.set_default(db,p.id);db.commit();return p.id


def create(db,source_text="First paragraph.\n\nSecond paragraph.",*,ref=None,mode="parallel",run_id=None,request="translation-request"):
    ident=run_id or "research_"+uuid4().hex;db.rollback()
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _,attempt,fresh=runs.create(db,kind="translation",run_id=ident,conversation_id=CID,request_id=request,
            inputs={"text":source_text,"source":"Chinese","target":"English","display_mode":mode,"source_ref":ref})
        db.commit()
    return ident,attempt.id,fresh


def execute(db,ident,attempt):
    try:runs.execute(ident,attempt)
    except Exception as exc:runs._mark_failure(ident,attempt,exc);raise
    db.expire_all();return runs.public(db,ident,CID)


def resume(db,ident,unknown=False):
    db.expire_all();version=db.get(RuntimeRun,ident).revision;db.rollback()
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _,attempt,_=runs.resume(db,ident,conversation_id=CID,request_id="resume-"+uuid4().hex,
            expected_revision=version,retry_unknown=unknown)
        db.commit()
    return attempt.id


class Model:
    def __init__(self,fail_at=None,error=None,on_call=None):self.calls=[];self.fail_at=fail_at;self.error=error;self.on_call=on_call
    def chat(self,messages,**options):
        self.calls.append({"messages":deepcopy(messages),"binding":current_runtime_config()})
        if self.on_call:self.on_call(len(self.calls))
        if self.fail_at==len(self.calls):
            if self.error:raise self.error
            return LLMResponse(content=" ")
        return LLMResponse(content="Synthetic translation: "+messages[-1]["content"])


def test_offline_does_not_claim_translation_completed_or_allow_useless_resume(db,scope):
    ident,attempt,_=create(db);value=execute(db,ident,attempt)
    assert value["result"]["mode"]=="offline" and value["result"]["translation"] is None
    assert value["outcome"]=="partial" and not value["can_resume"] and value["resume_blocked_reason"]
    with pytest.raises(sources.SourceConflict):resume(db,ident)
    db.rollback()


@pytest.mark.parametrize("unknown",[False,True])
def test_resume_keeps_paid_first_unit_and_retries_only_incomplete_unit(db,scope,monkeypatch,unknown):
    cloud(db);model=Model(2,httpx.ReadTimeout("PRIVATE_ERROR") if unknown else None)
    monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    ident,attempt,_=create(db)
    with pytest.raises(httpx.ReadTimeout if unknown else sources.SourceError):execute(db,ident,attempt)
    first=runs.public(db,ident,CID)
    assert first["result"]["progress"]=={"completed_units":1,"total_units":2}
    assert first["requires_unknown_confirmation"]==unknown and "PRIVATE_ERROR" not in str(first)
    runs.recover_startup();assert len(model.calls)==2
    model.fail_at=None
    final=execute(db,ident,resume(db,ident,unknown))
    assert final["outcome"]=="completed" and len(model.calls)==3
    assert final["result"]["units"][0]==first["result"]["units"][0]
    assert final["result"]["semantic_verification"]=="not_assessed"


def test_budget_pause_is_not_unknown_call_and_keeps_first_unit(db,scope,monkeypatch):
    from app.resource_budget import ResourceBudgetExceeded
    cloud(db);model=Model(2,ResourceBudgetExceeded("Synthetic boundary"));monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    ident,attempt,_=create(db)
    with pytest.raises(ResourceBudgetExceeded):execute(db,ident,attempt)
    first=runs.public(db,ident,CID)
    assert first["status"]=="paused" and first["result"]["progress"]["completed_units"]==1
    assert not first["requires_unknown_confirmation"]
    model.fail_at=None;assert execute(db,ident,resume(db,ident))["outcome"]=="completed"


def test_cancel_after_result_keeps_known_unit_and_stops_next_call(db,scope,monkeypatch):
    from app import cancel_store
    cloud(db);ident,attempt,_=create(db)
    model=Model(on_call=lambda n:cancel_store.request_cancel(ident));monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    with pytest.raises(runs.ResearchStopped):execute(db,ident,attempt)
    assert len(model.calls)==1 and runs.public(db,ident,CID)["result"]["progress"]["completed_units"]==1
    cancel_store.clear(ident);model.on_call=None
    assert execute(db,ident,resume(db,ident))["outcome"]=="completed" and len(model.calls)==2


def fixed_source(db,scope):
    raw=b"Old fixed passage.\nOther line."
    r=sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,"translation-fixed","upload"),
        kind="upload",title="source.md",raw_bytes=raw,text=raw.decode(),media_type="text/markdown")
    loc=sources.add_text_locator(db,r["revision_id"],scope=scope,line_start=1,line_end=1);db.commit()
    return r,loc,{"revision_id":r["revision_id"],"locator_id":loc["locator_id"]}


def test_fixed_source_is_validated_before_admission_and_stays_old_after_new_version(db,scope,monkeypatch):
    record,loc,ref=fixed_source(db,scope)
    with pytest.raises(sources.SourceConflict):create(db,"Edited passage",ref=ref)
    db.rollback();assert db.query(RuntimeRun).count()==0
    cloud(db);model=Model();monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    ident,attempt,_=create(db,loc["quote"],ref=ref)
    sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,"translation-new","upload"),
        kind="upload",title="source.md",raw_bytes=b"New passage.",text="New passage.",media_type="text/markdown",
        source_id=record["source_id"],expected_version=1);db.commit()
    value=execute(db,ident,attempt)
    assert value["result"]["source_evidence"][0]["locator_id"]==loc["locator_id"]
    assert value["result"]["source_evidence"][0]["version"]==1
    assert model.calls[0]["messages"][-1]["content"]==loc["quote"].strip()


def test_bound_model_does_not_follow_changed_global_default(db,scope,monkeypatch):
    cloud(db);ident,attempt,_=create(db)
    def change(n):
        if n==1:
            with SessionLocal.begin() as other:
                p=model_profiles.save_profile(other,{"name":"Other","base_url":"https://other.synthetic.test/v1","model_id":"Other","api_key":"other-synthetic"})
                model_profiles.set_default(other,p.id)
    model=Model(on_call=change);monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    value=execute(db,ident,attempt)
    assert value["outcome"]=="completed" and all(c["binding"]["model_id"]=="Translation-Bound-Case" for c in model.calls)


def test_source_revocation_after_first_unit_blocks_next_and_preserves_paid_receipt(db,scope,monkeypatch):
    cloud(db);ident,attempt,_=create(db)
    def remove(n):
        if n==1:
            with SessionLocal.begin() as other:other.add(DeletedConversation(id=CID))
    model=Model(on_call=remove);monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    with pytest.raises(sources.SourceAccessDenied):execute(db,ident,attempt)
    db.expire_all();state=AgentState.model_validate(db.get(RunAttempt,attempt).checkpoint)
    assert len(model.calls)==1 and any(key.startswith("translation:0:") for key in state.operations)
    with pytest.raises(sources.SourceAccessDenied):runs.public(db,ident,CID)


def test_partition_boundaries_have_no_lost_text_and_preserve_code():
    original="长"*25000+"\n\n```python\nprint('unchanged')\n```\n\n最后段落"
    value={"text":original,"display_mode":"parallel"}
    units=translation_run.units_for(value)
    assert all(len(u["source"])<=12000 or u["preserve"] for u in units)
    assert "".join(u["source"] for u in units)==original.replace("\n\n","")
    assert any(u["preserve"] and "print('unchanged')" in u["source"] for u in units)


def test_small_declared_model_window_produces_bounded_frozen_units():
    config=translation_run.snapshot_config({"context_window_tokens":8192,"max_output_tokens":2048})
    assert config["unit_character_limit"]==1024
    units=translation_run.units_for({"text":"原文"*4000,"display_mode":"whole"},config["unit_character_limit"])
    assert len(units)>1 and max(len(u["source"]) for u in units)<=1024
    assert "".join(u["source"] for u in units)=="原文"*4000


@pytest.mark.parametrize("source",["````python\n```\nDo not translate this code.\n````","$$\nx+y=1\n$$"])
def test_whole_mode_preserved_code_and_formula_use_zero_model_calls(db,scope,monkeypatch,source):
    cloud(db);model=Model();monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    ident,attempt,_=create(db,source,mode="whole")
    value=execute(db,ident,attempt)
    assert value["outcome"]=="completed" and value["result"]["translation"]==source
    assert model.calls==[]


def test_whole_mode_only_sends_prose_and_preserves_code_verbatim(db,scope,monkeypatch):
    cloud(db);model=Model();monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    source="Translate this prose.\n\n```python\nSECRET_CODE_BODY = 1\n```\n\nLast prose."
    ident,attempt,_=create(db,source,mode="whole");value=execute(db,ident,attempt)
    assert len(model.calls)==2
    assert all("SECRET_CODE_BODY" not in str(call["messages"]) for call in model.calls)
    assert "```python\nSECRET_CODE_BODY = 1\n```" in value["result"]["translation"]


def test_corrupted_paid_unit_is_not_disclosed_or_rebought(db,scope,monkeypatch):
    cloud(db);model=Model(2,httpx.ReadTimeout("Synthetic timeout"));monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    ident,attempt,_=create(db)
    with pytest.raises(httpx.ReadTimeout):execute(db,ident,attempt)
    first=runs.public(db,ident,CID)
    saved=deepcopy(db.get(RunAttempt,attempt).checkpoint)
    key=next(key for key in saved["operations"] if key.startswith("translation:0:"))
    saved["operations"][key]["payload"]["content"]="Corrupted paid receipt"
    db.get(RunAttempt,attempt).checkpoint=saved;db.commit()
    next_attempt=resume(db,ident,True);model.fail_at=None
    with pytest.raises(sources.SourceCorruption,match="回执校验失败"):execute(db,ident,next_attempt)
    assert len(model.calls)==2
    assert runs.public(db,ident,CID)["result"]==first["result"]


def test_saved_result_hash_and_archived_source_read_scope(db,scope,monkeypatch):
    cloud(db);_,loc,ref=fixed_source(db,scope)
    model=Model();monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    ident,attempt,_=create(db,loc["quote"],ref=ref)
    first=execute(db,ident,attempt)
    from app.timeutil import utcnow
    db.get(Conversation,CID).archived_at=utcnow();db.commit()
    assert runs.public(db,ident,CID)["result"]==first["result"]
    saved=deepcopy(db.get(RunAttempt,attempt).checkpoint)
    saved["result"]["payload"]["translation"]="Corrupted stored result"
    db.get(RunAttempt,attempt).checkpoint=saved;db.commit()
    with pytest.raises(sources.SourceCorruption,match="保存结果校验失败"):runs.public(db,ident,CID)
    assert len(model.calls)==1


def test_real_http_admission_scope_kind_fencing_cancel_and_resume(db,scope,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    from app import task_runner
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    starts=[];monkeypatch.setattr(runs,"start",starts.append)
    monkeypatch.setattr(translation_run,"get_llm",Model)
    ident="research_"+uuid4().hex
    body={"run_id":ident,"request_id":"translation-http-create","conversation_id":CID,"text":"Actual HTTP fixed input.","display_mode":"whole"}
    with TestClient(app) as client:
        first=client.post("/api/translation/runs",json=body)
        assert first.status_code==200,first.text
        assert starts==[ident] and first.json()["status"]=="queued"
        assert client.post("/api/translation/runs",json=body).json()["replayed"] and starts==[ident]
        assert client.post("/api/translation/runs",json={**body,"text":"Different"}).status_code==409
        assert client.get(f"/api/translation/runs/{ident}?conversation_id=translation-run-b").status_code==404
        assert client.get(f"/api/writing/framework/runs/{ident}?conversation_id={CID}").status_code==404
        assert client.post(f"/api/writing/framework/runs/{ident}/cancel",json={"conversation_id":CID}).status_code==404
        assert client.get(f"/api/translation/runs?conversation_id={CID}").json()["items"][0]["run_id"]==ident
        stopped=client.post(f"/api/translation/runs/{ident}/cancel",json={"conversation_id":CID}).json()
        assert stopped["status"]=="stopped" and stopped["can_resume"]
        resumed=client.post(f"/api/translation/runs/{ident}/resume",json={"conversation_id":CID,"request_id":"translation-http-resume","expected_revision":stopped["revision"]})
        assert resumed.status_code==200,resumed.text
        assert starts==[ident,ident]
        runs.execute(ident)
        final=client.get(f"/api/translation/runs/{ident}?conversation_id={CID}").json()
        assert final["outcome"]=="completed" and final["result"]["model_selection"]=="conversation_bound"
        assert "writer_token" not in str(final) and "credential_ref" not in str(final)


@pytest.mark.parametrize("fmt",["txt","md","pdf","docx","xlsx","csv"])
def test_six_format_fixed_inputs_use_official_durable_endpoint(source_client,db,sample_pdf,monkeypatch,fmt):
    from tests.test_source_document_runtime import word_bytes,excel_bytes
    raw={"pdf":sample_pdf.read_bytes,"docx":word_bytes,"xlsx":excel_bytes,
        "csv":lambda:b"Method,Score\nAttention,28.4"}.get(fmt,lambda:b"Fixed quote.\nOther line.")()
    record,loc,req=upload_fixed(source_client,raw,"source."+fmt)
    monkeypatch.setattr(runs,"start",lambda _:None)
    ident="research_"+uuid4().hex
    reply=source_client.post("/api/translation/runs",json={**req,"run_id":ident,"request_id":"durable-six-format","display_mode":"parallel"})
    assert reply.status_code==200,reply.text
    runs.execute(ident)
    value=source_client.get(f"/api/translation/runs/{ident}?conversation_id=trans-a").json()
    assert value["source_count"]==1
    proof=value["result"]["source_evidence"][0]
    assert proof["revision_id"]==record["revision_id"] and proof["locator_id"]==loc["locator_id"]
    assert proof["verified"] is False and value["result"]["semantic_verification"]=="not_assessed"


def test_full_backup_restores_completed_translation_and_fixed_locator(db,scope,monkeypatch,tmp_path):
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import config,data_backup
    from app.db import engine
    cloud(db);_,loc,ref=fixed_source(db,scope);monkeypatch.setattr(translation_run,"get_llm",Model)
    ident,attempt,_=create(db,loc["quote"],ref=ref);first=execute(db,ident,attempt);db.rollback()
    folder=tmp_path/"backup-source";folder.mkdir()
    data_backup._database_snapshot(Path(engine.url.database),folder/config.settings.db_name)
    saved=data_backup.create_backup(folder,database=config.settings.db_name,origin_data_dir=config.DATA_DIR)
    target=tmp_path/"translation-restored"
    staged=data_backup.prepare_restore(target,saved["path"],expected_database=config.settings.db_name)
    data_backup.schedule_restore(target,staged["id"]);data_backup.apply_pending_restore(target,expected_database=config.settings.db_name)
    restored_engine=create_engine("sqlite:///"+str(target/config.settings.db_name))
    try:
        with Session(restored_engine) as restored:
            value=runs.public(restored,ident,CID)
            assert value["result"]==first["result"] and value["result_integrity"]=="checked"
            assert sources.read_locator(restored,loc["locator_id"],scope=scope)["quote"]==loc["quote"]
    finally:restored_engine.dispose()
