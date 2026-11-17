"""Durable framework runs use real fixed sources; providers are synthetic."""
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app import research_runs as runs, run_store, model_profiles, source_store as sources
from app.agent.checkpoint import AgentState
from app.db import SessionLocal
from app.goal_models import Goal
from app.llm import LLMResponse
from app.models import Conversation
from app.runtime_context import current_runtime_config
from app.runtime_models import RuntimeRun, RunAttempt
from app.writing import framework_run
from tests.test_framework_evidence import seed, response, CID
from tests.test_library_index_versions import setup as library_setup, versions


@pytest.fixture
def fixed(db):
    seed(db,b"attention is measured\nOriginal immutable result.","text/plain")
    db.add(Conversation(id="framework-other",title="Other"));db.commit()
    runs._stopping.clear()
    yield
    for run_id in list(run_store.active_runs()):run_store.finish(run_id,"interrupted")


def create(db, *, idea="attention measured", request="framework-request", run_id=None):
    run_id=run_id or "research_"+uuid4().hex
    db.rollback()
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _, attempt, fresh=runs.create(db,kind="framework",run_id=run_id,conversation_id=CID,
            inputs={"idea":idea,"max_sources":8},request_id=request)
        db.commit()
    return run_id,attempt.id,fresh


def execute(db,ident,attempt):
    try:runs.execute(ident,attempt)
    except Exception as error:
        runs._mark_failure(ident,attempt,error)
        raise
    db.expire_all()
    return runs.public(db,ident,CID)


def resume(db,ident,*,unknown=False,request=None):
    db.expire_all();version=db.get(RuntimeRun,ident).revision;db.rollback()
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _,attempt,_=runs.resume(db,ident,conversation_id=CID,request_id=request or "resume-"+uuid4().hex,
            expected_revision=version,retry_unknown=unknown)
        db.commit()
    return attempt.id


def cloud(db):
    profile=model_profiles.save_profile(db,{"name":"Synthetic framework","base_url":"https://synthetic-framework.example.test/v1",
        "model_id":"Exact-Framework-Model","api_key":"synthetic-framework-key"})
    model_profiles.set_default(db,profile.id);db.commit()
    return profile.id


class Model:
    def __init__(self,review="valid",on_generate=None):self.calls=[];self.review=review;self.on_generate=on_generate
    def chat(self,messages,temperature=0):
        self.calls.append({"messages":deepcopy(messages),"binding":current_runtime_config()})
        is_review="独立的论点支持审查者" in messages[0]["content"]
        if not is_review:
            if self.on_generate:self.on_generate()
            return LLMResponse(json.dumps(response()))
        if self.review=="unknown":raise httpx.ReadTimeout("PRIVATE_SYNTHETIC_ERROR")
        checks=[] if self.review=="invalid" else [{"claim_id":"abstract:0","status":"supported","explanation":"Synthetic excerpt comparison","evidence_ids":["F1E1"]}]
        return LLMResponse(json.dumps({"checks":checks}))


def test_offline_result_is_durable_on_shared_attempt_and_goal(db,fixed):
    ident,attempt,fresh=create(db);assert fresh
    result=execute(db,ident,attempt)
    assert result["status"]=="ended" and result["outcome"]=="completed"
    assert result["result"]["mode"]=="offline" and result["result"]["verified"] is False
    assert result["source_count"]==1 and result["usage"]["call_count"]==0
    with SessionLocal() as other:
        loaded=runs.public(other,ident,CID)
        assert loaded["result"]==result["result"] and not loaded["can_resume"]
        run=other.get(RuntimeRun,ident)
        assert other.get(Goal,run.goal_id).run_id==ident and other.get(RunAttempt,attempt).result["payload"]==loaded["result"]


@pytest.mark.parametrize("review",["unknown","invalid"])
def test_review_resume_reuses_paid_generation_and_keeps_draft(db,fixed,monkeypatch,review):
    cloud(db);model=Model(review);monkeypatch.setattr(framework_run,"get_llm",lambda:model)
    ident,attempt,_=create(db);first=execute(db,ident,attempt)
    assert first["outcome"]=="partial" and first["result"]["semantic_verification"]=="incomplete"
    assert first["result"]["sections"][0]["bullets"][0]["text"]=="attention is measured"
    assert first["requires_unknown_confirmation"]==(review=="unknown")
    if review=="unknown":
        with pytest.raises(sources.SourceConflict):resume(db,ident)
        db.rollback()
    model.review="valid"
    next_attempt=resume(db,ident,unknown=review=="unknown")
    pending=runs.public(db,ident,CID);assert pending["result"]==first["result"]
    last=execute(db,ident,next_attempt)
    assert last["outcome"]=="completed" and last["attempt_number"]==2
    assert len(model.calls)==3,"Successful draft generation must not be paid again"
    assert model.calls[-1]["binding"]["attempt_id"]==next_attempt


def test_create_replay_does_not_restart_and_different_input_is_rejected(db,fixed):
    ident,attempt,_=create(db)
    again=create(db,run_id=ident);assert again==(ident,attempt,False)
    with pytest.raises(sources.SourceConflict):create(db,run_id=ident,idea="different input")
    db.rollback()


def test_scope_rejection_archive_and_restart_keep_known_operations(db,fixed,monkeypatch):
    cloud(db);model=Model("unknown");monkeypatch.setattr(framework_run,"get_llm",lambda:model)
    ident,attempt,_=create(db);execute(db,ident,attempt)
    with pytest.raises(sources.SourceNotFound):runs.public(db,ident,"framework-other")
    db.rollback()
    runs.recover_startup();assert len(model.calls)==2
    from app.timeutil import utcnow
    db.get(Conversation,CID).archived_at=utcnow();db.commit()
    assert runs.public(db,ident,CID)["result"] is not None
    with pytest.raises(sources.SourceConflict):resume(db,ident,unknown=True)
    db.rollback();assert len(model.calls)==2


def test_revocation_after_generation_prevents_support_request_and_retains_receipt(db,fixed,monkeypatch):
    profile=cloud(db)
    def revoke():
        from app.models import ModelCredential,ModelProfile
        from app.timeutil import utcnow
        with SessionLocal.begin() as writer:writer.get(ModelCredential,writer.get(ModelProfile,profile).credential_ref).revoked_at=utcnow()
    model=Model(on_generate=revoke);monkeypatch.setattr(framework_run,"get_llm",lambda:model)
    ident,attempt,_=create(db)
    with pytest.raises(model_profiles.ModelConfigurationError):execute(db,ident,attempt)
    db.expire_all();state=AgentState.model_validate(db.get(RunAttempt,attempt).checkpoint)
    assert len(model.calls)==1 and any(key.startswith("research:llm:0:") for key in state.operations)


def test_stale_writer_cannot_publish_after_new_attempt(db,fixed,monkeypatch):
    cloud(db);model=Model("unknown");monkeypatch.setattr(framework_run,"get_llm",lambda:model)
    ident,attempt,_=create(db);execute(db,ident,attempt)
    old=AgentState.model_validate(db.get(RunAttempt,attempt).checkpoint)
    resume(db,ident,unknown=True)
    old.answer="late wrong output"
    from app import run_lifecycle
    with pytest.raises(run_lifecycle.StaleAttempt):runs.Execution(ident,old).save()


def test_budget_refusal_preserves_structured_draft_and_needs_no_unknown_charge_ack(db,fixed,monkeypatch):
    cloud(db)
    from app.resource_budget import ResourceBudgetExceeded
    class BudgetModel(Model):
        limited=True
        def chat(self,messages,**kw):
            if self.limited and "独立的论点支持审查者" in messages[0]["content"]:
                raise ResourceBudgetExceeded("synthetic budget boundary")
            return super().chat(messages,**kw)
    model=BudgetModel();monkeypatch.setattr(framework_run,"get_llm",lambda:model)
    ident,attempt,_=create(db)
    with pytest.raises(ResourceBudgetExceeded):execute(db,ident,attempt)
    saved=runs.public(db,ident,CID)
    assert saved["result"]["structure_valid"] and saved["status"]=="paused"
    assert saved["resource_pause"]["kind"]=="budget" and not saved["requires_unknown_confirmation"]
    model.limited=False;next_attempt=resume(db,ident)
    result=execute(db,ident,next_attempt)
    assert result["outcome"]=="completed" and len(model.calls)==2


def test_http_admission_list_scope_and_result_polling(db,fixed,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    from app import task_runner
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    starts=[];monkeypatch.setattr(runs,"start",starts.append)
    ident="research_"+uuid4().hex
    body={"run_id":ident,"request_id":"http-framework-create","conversation_id":CID,"idea":"attention measured","max_sources":8}
    with TestClient(app) as client:
        first=client.post("/api/writing/framework/runs",json=body)
        assert first.status_code==200,first.text
        assert first.json()["status"]=="queued" and starts==[ident]
        assert client.post("/api/writing/framework/runs",json=body).json()["replayed"] and starts==[ident]
        assert client.post("/api/writing/framework/runs",json={**body,"idea":"different"}).status_code==409
        assert client.get(f"/api/writing/framework/runs/{ident}?conversation_id=framework-other").status_code==404
        assert client.get(f"/api/writing/framework/runs?conversation_id={CID}").json()["items"][0]["run_id"]==ident
        runs.execute(ident)
        final=client.get(f"/api/writing/framework/runs/{ident}?conversation_id={CID}").json()
        assert final["result"]["sections"] and final["outcome"]=="completed"
        assert "writer_token" not in json.dumps(final) and "credential_ref" not in json.dumps(final)


def test_default_model_change_during_execution_cannot_replace_bound_model(db,fixed,monkeypatch):
    cloud(db)
    def switch():
        with SessionLocal.begin() as other:
            profile=model_profiles.save_profile(other,{"name":"Other model","base_url":"https://another.synthetic.test/v1","model_id":"Other-Model","api_key":"other-synthetic-key"})
            model_profiles.set_default(other,profile.id)
    model=Model(on_generate=switch);monkeypatch.setattr(framework_run,"get_llm",lambda:model)
    ident,attempt,_=create(db);result=execute(db,ident,attempt)
    assert result["outcome"]=="completed" and result["model"]["model_id"]=="Exact-Framework-Model"
    assert all(call["binding"]["model_id"]=="Exact-Framework-Model" for call in model.calls)


def test_task_center_and_chat_deletion_share_existing_runtime_ownership(db,fixed):
    ident,attempt,_=create(db);execute(db,ident,attempt)
    from app import run_center,data_cleanup
    item=next(item for item in run_center.list_runs(db,view="all")["items"] if item["run_id"]==ident)
    assert item["kind"]=="framework" and item["target_kind"]=="framework" and item["status"]=="completed"
    assert "attention measured" in item["title"]
    db.rollback();db.execute(text("BEGIN IMMEDIATE"));data_cleanup.remove(db,CID);db.commit()
    assert db.get(RuntimeRun,ident) is None and db.get(RunAttempt,attempt) is None
    assert db.query(Goal).filter(Goal.run_id==ident).count()==0


def test_full_backup_restores_completed_job_and_fixed_result_locators(db,fixed,tmp_path):
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import config,data_backup
    from app.db import engine
    ident,attempt,_=create(db);original=execute(db,ident,attempt);db.rollback()
    folder=tmp_path/"backup-source";folder.mkdir()
    data_backup._database_snapshot(Path(engine.url.database),folder/config.settings.db_name)
    exported=data_backup.create_backup(folder,database=config.settings.db_name,origin_data_dir=config.DATA_DIR)
    target=tmp_path/"restored-framework"
    staged=data_backup.prepare_restore(target,exported["path"],expected_database=config.settings.db_name)
    data_backup.schedule_restore(target,staged["id"]);data_backup.apply_pending_restore(target,expected_database=config.settings.db_name)
    restored_engine=create_engine("sqlite:///"+str(target/config.settings.db_name))
    try:
        with Session(restored_engine) as restored:
            result=runs.public(restored,ident,CID)
            assert result["result"]==original["result"] and result["status"]=="ended"
            evidence=result["result"]["references"][0]["evidence"][0]
            loc=sources.read_locator(restored,evidence["locator_id"],scope=sources.SourceScope("conversation",CID))
            assert evidence["quote"] in loc["quote"]
    finally:restored_engine.dispose()


def test_resume_retains_original_library_parser_after_primary_selection_changes(db,library_setup,monkeypatch):
    from app import library_index_store
    pid, old, new, _ = versions(db,library_setup)
    db.add(Conversation(id=CID,title="Frozen framework versions"));db.commit()
    cloud(db);model=Model("unknown");monkeypatch.setattr(framework_run,"get_llm",lambda:model)
    ident,attempt,_=create(db,idea="Body original unchanged")
    state=AgentState.model_validate(db.get(RunAttempt,attempt).checkpoint)
    framework_run.freeze(runs.Execution(ident,state))
    state=library_index_store.state(db,pid)
    library_index_store.select_primary(db,pid,new,conversation_id="index-owner",expected_version=state["version"],expected_primary_id=old);db.commit()
    first=execute(db,ident,attempt)
    assert first["outcome"]=="partial"
    scope=sources.SourceScope("conversation",CID)
    assert sources.read_revision(db,first["result"]["references"][0]["evidence"][0]["revision_id"],scope=scope)["parser"]=="python-docx/1"
    runs.recover_startup();model.review="valid"
    last=execute(db,ident,resume(db,ident,unknown=True))
    assert last["outcome"]=="completed" and len(model.calls)==3
    assert last["result"]["references"][0]["evidence"]==first["result"]["references"][0]["evidence"]
    assert library_index_store.state(db,pid)["primary_revision_id"]==new


def test_worker_start_failure_is_resumable_and_does_not_leave_live_registry(db,fixed,monkeypatch):
    ident,attempt,_=create(db)
    def fail(_):raise RuntimeError("PRIVATE_SYNTHETIC_THREAD_ERROR")
    monkeypatch.setattr(runs.threading.Thread,"start",fail)
    with pytest.raises(sources.SourceConflict):runs.start(ident)
    db.expire_all();saved=runs.public(db,ident,CID)
    assert saved["status"]=="interrupted" and saved["can_resume"]
    assert ident not in runs.active() and ident not in run_store.active_runs()
    assert "PRIVATE_SYNTHETIC" not in json.dumps(saved)


def test_cancel_before_start_and_startup_recovery_do_not_send_requests(db,fixed):
    ident,attempt,_=create(db)
    assert runs.cancel(db,ident,CID)=="stopped";db.commit()
    runs.execute(ident,attempt)
    assert runs.public(db,ident,CID)["status"]=="stopped"
    next_attempt=resume(db,ident)
    runs.recover_startup();db.expire_all()
    saved=runs.public(db,ident,CID)
    assert saved["status"]=="interrupted" and saved["can_resume"]
    assert saved["usage"]["call_count"]==0
    assert db.get(RunAttempt,next_attempt).status=="interrupted"
