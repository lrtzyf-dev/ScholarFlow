"""Harness mechanics only: none of these are real-model acceptance cases."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from scripts import verify_real_acceptance as runner


def test_manifest_has_distinct_goals_rubrics_and_no_claimed_results():
    value=runner.load_manifest(runner.DEFAULT_MANIFEST)
    assert value["case_count"]==len(value["cases"])==60
    assert value["execution_status"]=="not_run" and value["real_provider_calls"]==0
    assert len({row["user_goal"]for row in value["cases"]})==60
    assert {row["runner"]for row in value["cases"]}=={"chat","translation","framework"}
    assert len({row["category"]for row in value["cases"]})==7
    assert {name.rsplit(".",1)[-1]for row in value["cases"]for name in row["fixtures"]}>={"pdf","txt","md","docx","xlsx","csv"}
    assert all(row["execution_status"]=="not_run"and row["manual_review_required"]for row in value["cases"])


def test_generated_manifest_is_identical_to_reviewable_source():
    spec=importlib.util.spec_from_file_location("acceptance_cases",runner.DEFAULT_MANIFEST.with_name("case_manifest.py"))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.manifest()==runner.load_manifest(runner.DEFAULT_MANIFEST)


def test_default_plan_never_reads_credentials_or_opens_network(tmp_path,monkeypatch):
    import httpx
    def forbidden(*args,**kwargs):pytest.fail("A plan accessed credentials or network")
    monkeypatch.setattr(httpx.HTTPTransport,"handle_request",forbidden)
    from app import credential_protection
    monkeypatch.setattr(credential_protection,"unprotect",forbidden)
    destination=tmp_path/"plan.json"
    assert runner.main(["--report",str(destination),"--credentials-dir",str(tmp_path/"not-present"),
        "--allow-paid-tools","--service-config-db",str(tmp_path/"also-not-present")])is None
    value=json.loads(destination.read_text(encoding="utf-8"))
    assert not value["credentials_read"]and value["real_provider_calls"]==0 and value["case_count"]==60
    assert not value["commercial_acceptance"]


def test_real_mode_rejects_unbounded_suite_before_any_credential_read(tmp_path):
    with pytest.raises(SystemExit):runner.main(["--run-real","--report",str(tmp_path/"no.json")])
    assert not (tmp_path/"no.json").exists()


def test_secret_guard_cannot_write_model_or_service_key(tmp_path):
    destination=tmp_path/"report.json"
    with pytest.raises(ValueError,match="SecretRejected"):
        runner.write_report(destination,{"text":"do not expose synthetic-secret"},("synthetic-secret",))
    assert not destination.exists()


def test_public_diagnostics_exclude_private_thoughts_and_provider_fields():
    events=[{"kind":"thought_delta","text":"PRIVATE-THOUGHT"},
        {"kind":"reasoning_delta","text":"PRIVATE-REASONING"},
        {"kind":"answer_delta","text":"Public candidate answer."},
        {"kind":"critic","gate":{"passed":False,"unsupported":["A public claim"],"reasoning":"PRIVATE-GATE"}},
        {"kind":"audit","audit":{"status":"failed","unsupported":[{"text":"A public claim","reason":"Public evidence mismatch"}],
            "provider_context":{"secret":"PRIVATE-PROVIDER"}}},
        {"kind":"refuse","text":"Public refusal","has_invalid":False,"audit_blocked":True},
        {"kind":"error","text":"Public tool error","source":"tool","provider_context":"PRIVATE-ERROR"}]
    scope={"protocol":"citation_scope_with_process_receipts_v1","process_receipt_count":1,
        "exemptions":[{"kind":"process","process_scope":"bounded",
            "supports":[{"receipt_id":"context:3","quote":"Public process metadata"}],
            "reasoning":"PRIVATE-SCOPE"}]}
    value=runner.public_diagnostics(events,{"gate":{"passed":False,"scoped_review":scope},"audit":{"status":"failed"},"stop_reason":"refused"})
    encoded=json.dumps(value)
    assert "PRIVATE-"not in encoded
    assert value["public_streamed_answer"]=="Public candidate answer."
    assert [e["kind"]for e in value["events"]]==["critic","audit","refuse","error"]
    assert value["events"][1]["audit"]["unsupported"][0]["reason"]=="Public evidence mismatch"
    proof=value["final_gate"]["scoped_review"]
    assert proof["protocol"]==scope["protocol"]and proof["process_receipt_count"]==1
    assert proof["exemptions"][0]["process_scope"]=="bounded"
    assert proof["exemptions"][0]["supports"]==scope["exemptions"][0]["supports"]


def test_citation_draft_probe_keeps_run_identity_and_restores_gate_on_failure(monkeypatch):
    from app.agent import loop
    from app.runtime_context import bind_runtime_config
    def gate(answer,ids):return {"passed":answer=="Public rewritten answer.","unsupported":[],"reasoning":"PRIVATE-GATE"}
    monkeypatch.setattr(loop,"citation_gate",gate)
    with pytest.raises(RuntimeError,match="Synthetic"):
        with runner.public_citation_probe()as drafts:
            with bind_runtime_config({"run_id":"one"}):
                assert not loop.citation_gate("Public initial answer.",[])["passed"]
                assert loop.citation_gate("Public rewritten answer.",[])["passed"]
            with bind_runtime_config({"run_id":"two"}):loop.citation_gate("Another public answer.",[])
            assert len(drafts["one"])==2 and len(drafts["two"])==1
            assert "PRIVATE-"not in json.dumps(drafts)
            raise RuntimeError("Synthetic failure")
    assert loop.citation_gate is gate


@pytest.mark.parametrize("outcome,completed,refusal,accepted",[
    ("completed",True,False,True),("blocked",False,True,False),("partial",False,False,False),
    ("open",True,False,False),("completed",True,True,False),(None,None,False,False)])
def test_nonempty_refusal_or_partial_cannot_pass_normal_case(db,outcome,completed,refusal,accepted):
    case={"control":None,"machine_checks":{}}
    result={"answer":"An ordinary refusal still contains text.","turns":[{"http_status":200,
        "answer":"An ordinary refusal still contains text.","outcome":outcome,"completed":completed,"refusal":refusal}]}
    assert runner.evaluate_receipts(case,result,None,"unused","unused")["answer_or_explicit_cancel"]is accepted


def test_cancellation_requires_expected_case_and_actual_receipt(db):
    case={"control":{"kind":"cancel"},"machine_checks":{}}
    result={"answer":"","turns":[],"controller":{"cancel_http":200,"cancel":{"status":"cancelling"}}}
    assert runner.evaluate_receipts(case,result,None,"unused","unused")["answer_or_explicit_cancel"]
    case["control"]=None
    assert not runner.evaluate_receipts(case,result,None,"unused","unused")["answer_or_explicit_cancel"]
    case["control"]={"kind":"cancel"};result["controller"]["cancel_http"]=409
    assert not runner.evaluate_receipts(case,result,None,"unused","unused")["answer_or_explicit_cancel"]


def test_planned_outcome_requires_explicit_plan_case_and_saved_steps(db):
    result={"answer":"Proposed public plan.","turns":[{"http_status":200,"answer":"Proposed public plan.",
        "outcome":"planned","completed":False,"refusal":False,"plan_steps":[{"title":"Review"}]}]}
    case={"control":{"kind":"plan"},"machine_checks":{}}
    assert runner.evaluate_receipts(case,result,None,"unused","unused")["answer_or_explicit_cancel"]
    case["control"]=None
    assert not runner.evaluate_receipts(case,result,None,"unused","unused")["answer_or_explicit_cancel"]


def test_service_copy_reads_only_dedicated_tables_and_cannot_escape_directory(tmp_path):
    target=tmp_path/"protected.db"
    with sqlite3.connect(target)as db:
        db.executescript("CREATE TABLE service_tool_configurations(provider TEXT,enabled_services TEXT,credential_ref TEXT);"
            "CREATE TABLE service_tool_credentials(id TEXT,provider TEXT,protected_value TEXT,revoked_at TEXT);"
            "CREATE TABLE papers(id TEXT,private_document TEXT);")
        db.execute("INSERT INTO service_tool_configurations VALUES ('deepseek',?, 'key1')",('["search","fetch"]',))
        db.execute("INSERT INTO service_tool_credentials VALUES ('key1','deepseek','dpapi:synthetic-protected',NULL)")
    result=runner.protected_service_configuration(target,tmp_path)
    assert result=={"enabled_services":["search","fetch"],"protected_value":"dpapi:synthetic-protected"}
    with pytest.raises(ValueError,match="InsideProtected"):
        runner.protected_service_configuration(target,tmp_path/"child")


def test_protected_model_key_wins_over_legacy_without_mutating_config(tmp_path):
    path=tmp_path/"config.json"
    path.write_text(json.dumps({"protected_credentials":{"llm_api_key":"protected-fixture"},
        "llm_api_key":"synthetic-old-key","llm_base_url":"https://example.test/v1","llm_model":"fixture-model"}),encoding="utf-8")
    before=path.read_bytes();calls=[]
    def decrypt(value):calls.append(value);return "synthetic-protected-key"
    value=runner.model_configuration(tmp_path,decrypt)
    assert value=={"api_key":"synthetic-protected-key","base_url":"https://example.test/v1",
        "model_id":"fixture-model","storage_kind":"protected_configuration"}
    assert calls==["protected-fixture"]and path.read_bytes()==before
    assert sorted(p.name for p in tmp_path.iterdir())==["config.json"]


def test_legacy_model_key_is_read_only_memory_fallback_without_decrypting(tmp_path):
    path=tmp_path/"config.json"
    path.write_text(json.dumps({"llm_api_key":"synthetic-legacy-key","llm_base_url":"https://example.test/v1",
        "llm_model":"fixture-model","protected_credentials":{"embedding_api_key":"unrelated"}}),encoding="utf-8")
    before=path.read_bytes()
    def forbidden(value):pytest.fail("Legacy-only model configuration attempted decryption")
    value=runner.model_configuration(tmp_path,forbidden)
    assert value["api_key"]=="synthetic-legacy-key"
    assert value["storage_kind"]=="legacy_plaintext_read_only_memory_fallback_pending_migration"
    assert path.read_bytes()==before and sorted(p.name for p in tmp_path.iterdir())==["config.json"]
    destination=tmp_path/"report.json"
    runner.write_report(destination,{"credential_storage_kind":value["storage_kind"]},(value["api_key"],))
    assert "synthetic-legacy-key"not in destination.read_text(encoding="utf-8")


def test_protected_decryption_failure_never_falls_back_to_old_model_key(tmp_path):
    path=tmp_path/"config.json"
    path.write_text(json.dumps({"llm_api_key":"synthetic-legacy-key",
        "protected_credentials":{"llm_api_key":"broken-protected-fixture"}}),encoding="utf-8")
    before=path.read_bytes()
    class FailedDecryption(Exception):pass
    def decrypt(value):raise FailedDecryption("Synthetic failure")
    with pytest.raises(FailedDecryption):runner.model_configuration(tmp_path,decrypt)
    assert path.read_bytes()==before and sorted(p.name for p in tmp_path.iterdir())==["config.json"]


@pytest.mark.parametrize("protected",[{"llm_api_key":""},{"llm_api_key":None},"malformed"])
def test_malformed_present_protected_model_key_cannot_use_legacy(tmp_path,protected):
    (tmp_path/"config.json").write_text(json.dumps({"llm_api_key":"synthetic-legacy-key",
        "protected_credentials":protected}),encoding="utf-8")
    def forbidden(value):pytest.fail("Malformed protected key reached decrypt")
    with pytest.raises(ValueError,match="InvalidProtectedModel"):runner.model_configuration(tmp_path,forbidden)


@pytest.mark.parametrize("saved",[{}, {"llm_api_key":""}, {"llm_api_key":None}])
def test_missing_model_credential_does_not_become_a_real_run(tmp_path,saved):
    (tmp_path/"config.json").write_text(json.dumps(saved),encoding="utf-8")
    with pytest.raises(ValueError,match="ConfiguredModelCredentialRequired"):
        runner.model_configuration(tmp_path,lambda value:pytest.fail("Unexpected decryption"))


@pytest.mark.parametrize("name",["pilot.pdf","pilot.txt","pilot.md","protocol.docx","budget.xlsx","scores.csv","ocr-scan.pdf"])
def test_fabricated_fixtures_are_real_parseable_formats_without_user_material(name):
    from app.source_parsing import MEDIA,extract
    raw=runner.fixture_bytes(name)
    value=extract(raw,MEDIA[name.rsplit(".",1)[-1]])
    if name=="ocr-scan.pdf":assert not value.text.strip()
    else:assert value.text.strip()


def test_all_declared_machine_predicates_have_a_fail_closed_implementation(db):
    class InaccessibleSource:
        def get(self,*args,**kwargs):return type("Response",(),{"status_code":404})()
    for case in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"]:
        result={"turns":[{"http_status":200,"answer":"","completed":False}],"answer":"","tools":[],"evidence":[]}
        checks=runner.evaluate_receipts(case,result,InaccessibleSource(),"evaluation-empty","actual-configured-model")
        assert all(type(v)is bool for v in checks.values())


def test_plain_chat_driver_uses_actual_foreground_and_stream_endpoints(db,monkeypatch):
    from app.main import app
    from app.agent import loop
    from app import task_runner
    from app.llm import LLMResponse
    from fastapi.testclient import TestClient
    import threading
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    class Model:
        def stream_chat(self,*args,**kwargs):
            yield "final",LLMResponse("Harness mechanics only. Not real quality evidence.")
    monkeypatch.setattr(loop,"get_llm",Model)
    case=deepcopy(runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"][0])
    with TestClient(app)as client:
        cid=client.post("/api/conversations",json={"title":"Harness mechanics"}).json()["id"]
        result=runner.run_chat(client,case,cid,{},30)
        assert result["answer"]=="Harness mechanics only. Not real quality evidence.",result
        assert result["turns"][0]["run_id"].startswith("chat_")
        observation=client.get(f"/api/chat/runs/{result['turns'][0]['run_id']}/events",params={"conversation_id":cid})
        assert observation.status_code==200


def test_real_evaluation_environment_retains_isolated_multiturn_capture(tmp_path):
    values=runner.isolated_environment(tmp_path,"https://provider.example.test/v1","Exact-Model")
    assert values["MEMORY_CAPTURE"]==values["MEMORY_RECALL"]=="true"
    assert Path(values["DATA_DIR"]).is_relative_to(tmp_path)
    assert not values["LLM_API_KEY"] and not values["EMBEDDING_API_KEY"]


def test_multiturn_driver_reuses_only_its_conversations_authorized_history(db,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    from app.agent import loop
    from app import task_runner
    from app.config import settings
    from app.llm import LLMResponse
    from app.models import QARecord
    monkeypatch.setattr(settings,"memory_capture",True)
    monkeypatch.setattr(settings,"memory_recall",True)
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    prompts=[]
    class Model:
        def stream_chat(self,messages,*args,**kwargs):
            prompts.append(deepcopy(messages))
            yield "final",LLMResponse("下周一" if len(prompts)==3 else "本会话已收到该日期。")
    monkeypatch.setattr(loop,"get_llm",Model)
    case=deepcopy(next(c for c in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"] if c["id"]=="DAILY08"))
    with TestClient(app)as client:
        cid=client.post("/api/conversations",json={"title":"Isolated multi-turn mechanics"}).json()["id"]
        other=client.post("/api/conversations",json={"title":"Other private scope"}).json()["id"]
        db.add(QARecord(conversation_id=other,question="DO_NOT_CROSS_SCOPE",answer="PRIVATE_OTHER",in_memory=1));db.commit()
        result=runner.run_chat(client,case,cid,{},30)
        assert result["answer"]=="下周一" and len(prompts)==3
        supplied=[m["content"] for m in prompts[2] if m.get("role")=="user" and isinstance(m.get("content"),str)]
        assert any("周四" in text for text in supplied) and any("更正，改为下周一" in text for text in supplied)
        assert "DO_NOT_CROSS_SCOPE" not in json.dumps(prompts,ensure_ascii=False)
        rows=db.query(QARecord).filter(QARecord.conversation_id==cid).all()
        assert len(rows)==3 and all(row.in_memory==1 for row in rows)


def test_research_driver_uses_durable_translation_and_idempotent_readback(db,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    from app import task_runner,research_runs
    from app.translate import translation_run
    from tests.test_translation_runs import Model
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    model=Model();monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    case=next(c for c in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"]if c["id"]=="CONTROL14")
    with TestClient(app)as client:
        research_runs._stopping.clear()
        cid=client.post("/api/conversations",json={"title":"Durable harness mechanics"}).json()["id"]
        result=runner.run_research(client,case,cid,30)
        assert result["run"]["outcome"]=="completed" and len(model.calls)==1
        assert result["controller"]["research_replay_no_rebilling"]
        assert all(runner.evaluate_receipts(case,result,client,cid,"unused-mechanics-model").values())


def test_saved_outputs_keep_actual_pending_diff_for_later_semantic_review(db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app)as client:
        cid=client.post("/api/conversations",json={"title":"Diff review harness"}).json()["id"]
        created=client.post("/api/artifacts",json={"conversation_id":cid,"request_id":"harness_memo_1","title":"Memo",
            "kind":"document","content":"# Goal\nunchanged\n\n# Conclusion\nToo broad.\n","evidence":[],"analysis":{}}).json()
        detail=client.get(f"/api/artifacts/{created['id']}",params={"conversation_id":cid}).json()
        edit=client.post(f"/api/artifacts/{created['id']}/changes",json={"conversation_id":cid,"request_id":"harness_edit_1",
            "base_revision_id":created["revision"]["id"],"content":"# Goal\nunchanged\n\n# Conclusion\nLimited evidence.\n"})
        assert edit.status_code==200
        saved=runner.collect_research_outputs(client,cid)
        assert len(saved["artifacts"])==1 and saved["artifacts"][0]["pending_changes"]["items"]
        assert "Too broad."in saved["artifacts"][0]["current"]["revision"]["content"]


@pytest.fixture
def artifact_history_review_case(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.agent.tools import execute_tool
    from app.runtime_context import bind_runtime_config
    client=TestClient(app)
    cid=client.post("/api/conversations",json={"title":"Fabricated artifact history review"}).json()["id"]
    before=runner.prepare_memo_fixture(client,cid,history=True)
    result={"answer":"Review only: confirm a fixed target and the current base before proposing a restore.",
        "tools":["list_research_artifact_versions","read_research_artifact"],
        "turns":[{"run_id":"review-one","http_status":200,"answer":"Review only.","completed":True,"outcome":"completed"}]}
    with runner.public_artifact_read_probe()as observed:
        with bind_runtime_config({"conversation_id":cid,"run_id":"review-one"}):
            listing=json.loads(execute_tool(db,"list_research_artifact_versions",{"artifact_id":before["artifact_id"]},conversation_id=cid))
            for version in (listing["items"][-1],listing["items"][0]):
                value=json.loads(execute_tool(db,"read_research_artifact",{
                    "artifact_id":before["artifact_id"],"revision_id":version["id"]},conversation_id=cid))
                assert value["version"]==version["version"]
    result["artifact_history_review"]=runner.artifact_history_review_receipt(client,cid,before,result,observed)
    return client,cid,result


def test_artifact_review_fixture_has_three_real_versions_and_actual_unchanged_reads(artifact_history_review_case):
    client,cid,result=artifact_history_review_case
    proof=result["artifact_history_review"]
    assert [r["version"]for r in proof["before"]["revisions"]]==[3,2,1]
    assert len({r["content_sha256"]for r in proof["before"]["revisions"]})==3
    assert proof["before"]==proof["after"]and proof["before"]["pending_change_count"]==0
    case=next(c for c in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"]if c["id"]=="ART03")
    assert case["machine_checks"]["artifact_history_reviewed"]is True
    assert all(runner.evaluate_receipts(case,result,client,cid,"unused").values())
    # ART02 still exercises a single initial version and a separately proposed edit.
    other=client.post("/api/conversations",json={"title":"Single-version edit fixture"}).json()["id"]
    initial=runner.prepare_memo_fixture(client,other)
    assert initial["version"]==1 and len(initial["revisions"])==1


@pytest.mark.parametrize("damage",["missing_proof","one_version","no_list","no_read","only_head",
    "wrong_run","wrong_conversation","wrong_artifact","wrong_content","partial_read","head_changed","pending_changed"])
def test_artifact_review_rejects_claimed_or_incomplete_history_receipts(artifact_history_review_case,damage):
    _,cid,original=artifact_history_review_case
    result=deepcopy(original);proof=result["artifact_history_review"];receipts=proof["tool_receipts"]
    if damage=="missing_proof":result.pop("artifact_history_review")
    elif damage=="one_version":
        proof["before"]["revisions"]=proof["before"]["revisions"][:1];proof["after"]=deepcopy(proof["before"])
    elif damage=="no_list":proof["tool_receipts"]=[r for r in receipts if r["tool"]!="list_research_artifact_versions"]
    elif damage=="no_read":proof["tool_receipts"]=[r for r in receipts if r["tool"]!="read_research_artifact"]
    elif damage=="only_head":proof["tool_receipts"]=[r for r in receipts if r.get("version")!=1]
    elif damage in ("wrong_run","wrong_conversation","wrong_artifact"):
        key={"wrong_run":"run_id","wrong_conversation":"conversation_id","wrong_artifact":"artifact_id"}[damage]
        for receipt in receipts:receipt[key]="other"
    elif damage=="wrong_content":
        for receipt in receipts:
            if receipt["tool"]=="read_research_artifact":receipt["returned_content_sha256"]="0"*64
    elif damage=="partial_read":
        for receipt in receipts:
            if receipt["tool"]=="read_research_artifact":receipt["offset"]=1
    elif damage=="head_changed":proof["after"]["current_revision_id"]="0"*32
    elif damage=="pending_changed":proof["after"]["pending_change_count"]+=1
    assert not runner.artifact_history_reviewed(result,cid)


@pytest.mark.parametrize("mutation",["pending","accepted","rejected","new_artifact"])
def test_artifact_review_detects_real_api_writes_even_without_reported_write_tools(artifact_history_review_case,mutation):
    client,cid,result=artifact_history_review_case
    proof=result["artifact_history_review"];before=proof["before"];ident=before["artifact_id"]
    if mutation=="new_artifact":runner.prepare_memo_fixture(client,cid)
    else:
        change=client.post(f"/api/artifacts/{ident}/changes",json={"conversation_id":cid,
            "request_id":"review_mutation_123","base_revision_id":before["current_revision_id"],"content":"Changed after review."})
        assert change.status_code==200
        if mutation in ("accepted","rejected"):
            accepted=client.post(f"/api/artifacts/{ident}/changes/{change.json()['id']}/decision",json={
                "conversation_id":cid,"accept":mutation=="accepted","expected_revision_id":before["current_revision_id"]})
            assert accepted.status_code==200
    proof["after"]=runner.artifact_review_snapshot(client,cid,ident)
    assert not runner.artifact_history_reviewed(result,cid)


def test_artifact_read_probe_restores_dispatcher_and_keeps_only_public_hashes(monkeypatch):
    from app import artifact_tools
    from app.runtime_context import bind_runtime_config
    value={"artifact_id":"a"*32,"revision_id":"b"*32,"version":1,"content_sha256":"c"*64,
        "content":"Public fabricated content","next_offset":None,"provider_context":"PRIVATE-PROVIDER"}
    def execute(*args,**kwargs):return value
    monkeypatch.setattr(artifact_tools,"execute",execute)
    with pytest.raises(RuntimeError,match="Synthetic"):
        with runner.public_artifact_read_probe()as observed:
            with bind_runtime_config({"run_id":"one"}):
                assert artifact_tools.execute(None,"read_research_artifact",{},conversation_id="cid")is value
            encoded=json.dumps(observed)
            assert "PRIVATE-"not in encoded and "Public fabricated content"not in encoded
            raise RuntimeError("Synthetic failure")
    assert artifact_tools.execute is execute


def test_semantic_review_cannot_promote_unexecuted_or_failed_receipts():
    from scripts.review_real_acceptance import reviewed_report
    base={"kind":"real_acceptance_run","source_unchanged_during_evaluation":True,"cases":[{
        "id":"ONE","acceptance":["exact criterion"],"status":"blocked","machine_receipts_passed":False}]}
    review={"reviewer":"Independent test reviewer","reviewer_kind":"human","independent_review_attestation":True,"case_id":"ONE","decision":"pass",
        "rubric":[{"criterion":"exact criterion","passed":True,"note":"Reasoned finding."}]}
    with pytest.raises(ValueError,match="BlockedOrUnexecuted"):
        reviewed_report(base,review,source_report_sha256="a",review_sha256="b")
    base["cases"][0]["status"]="machine_failed"
    failed=reviewed_report(base,review,source_report_sha256="a",review_sha256="b")
    assert failed["passed"] is False and failed["cases"][0]["passed"] is False
    assert failed["cases"][0]["status"]=="machine_failed"
    assert failed["cases"][0]["semantic_review"]["status"]=="passed"
    assert failed["commercial_acceptance"] is False
    base["cases"][0].update(status="needs_semantic_review",machine_receipts_passed=True)
    value=reviewed_report(base,review,source_report_sha256="a",review_sha256="b")
    assert value["passed"]and not value["commercial_acceptance"]
    assert base["cases"][0]["status"]=="needs_semantic_review"


def test_agent_review_does_not_impersonate_human_semantic_acceptance():
    from scripts.review_real_acceptance import reviewed_report
    base={"kind":"real_acceptance_run","source_unchanged_during_evaluation":True,"cases":[{
        "id":"ONE","acceptance":["criterion"],"status":"needs_semantic_review","machine_receipts_passed":True,
        "semantic_review":{"status":"pending","reviewer":None}}]}
    review={"reviewer":"Independent agent","reviewer_kind":"agent","independent_review_attestation":True,"case_id":"ONE","decision":"pass",
        "rubric":[{"criterion":"criterion","passed":True,"note":"Checked actual generated output."}]}
    result=reviewed_report(base,review,source_report_sha256="a",review_sha256="b")
    assert result["cases"][0]["agent_review"]["status"]=="passed"
    assert result["cases"][0]["status"]=="needs_human_review"
    assert result["semantic_acceptance"]=="pending_independent_human_review"and not result["passed"]
    review.pop("reviewer_kind")
    with pytest.raises(ValueError,match="ExplicitReviewerKind"):
        reviewed_report(base,review,source_report_sha256="a",review_sha256="b")


def test_known_translation_resume_driver_preserves_units_without_rebuying(db,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    from app import task_runner,research_runs
    from app.translate import translation_run
    from tests.test_translation_runs import Model
    release=threading.Event()
    class Controlled(Model):
        def chat(self,*args,**kwargs):
            value=super().chat(*args,**kwargs)
            if len(self.calls)==2:assert release.wait(8),"Harness did not stop a partially translated run"
            return value
    model=Controlled();monkeypatch.setattr(translation_run,"get_llm",lambda:model)
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    case=next(c for c in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"]if c["id"]=="CONTROL13")
    with TestClient(app)as client:
        research_runs._stopping.clear()
        class Observer:
            def get(self,*args,**kwargs):return client.get(*args,**kwargs)
            def post(self,path,**kwargs):
                response=client.post(path,**kwargs)
                if path.endswith("/cancel"):release.set()
                return response
        cid=client.post("/api/conversations",json={"title":"Known translation resume mechanics"}).json()["id"]
        try:result=runner.run_research(Observer(),case,cid,30)
        finally:release.set()
        assert result["run"]["outcome"]=="completed"
        assert result["controller"]["known_resume_exercised"]
        assert len(model.calls)==3


def test_matrix_manifest_uses_durable_child_entry_without_loosening_user_rubric():
    cases={c["id"]:c for c in runner.load_manifest(runner.DEFAULT_MANIFEST)["cases"]}
    for ident in ("RESEARCH05","RESEARCH06"):
        case=cases[ident]
        assert case["machine_checks"]["required_tools"]==["create_evidence_matrix","start_research_child"]
        assert case["machine_checks"]["matrix_saved"]and case["machine_checks"]["matrix_extracted"]
        assert case["manual_review_required"]and case["execution_status"]=="not_run"
    assert cases["RESEARCH05"]["machine_checks"]["matrix_extracted"]["field_labels"]==["样本量","研究设计","结果方向"]
    assert cases["RESEARCH06"]["acceptance"][1]=="未找到与不存在不同，导出仍保留此状态"
    assert {"start_research_child","resume_research_child"}<=runner.WRITE_ALLOW
    assert not {"extract_evidence_matrix","run_project_command","write_project_file","read_research_child"}&runner.WRITE_ALLOW


@pytest.fixture
def matrix_harness_case(db,monkeypatch):
    import threading
    from app import research_runs
    from app.config import settings
    from tests.test_matrix_runs import seed,FIELDS,CID
    from tests.test_research_children import parent
    monkeypatch.setattr(settings,"llm_api_key","")
    monkeypatch.setattr(settings,"embedding_api_key","")
    monkeypatch.setattr(research_runs,"_stopping",threading.Event())
    item,_=seed(db)
    binding=parent(db)
    expected={"field_labels":[f["label"]for f in FIELDS],"source_fixtures":["alpha-a.md","alpha-b.md"]}
    result={"answer":"Local mechanics, not real-model quality evidence.","turns":[{"run_id":binding["run_id"],
        "http_status":200,"answer":"Local mechanics.","completed":True,"outcome":"completed"}],
        "source_revision_ids":dict(zip(expected["source_fixtures"],[p["revision_id"]for p in item.document["papers"]])),
        "tools":["create_evidence_matrix"],"evidence":[]}
    case={"control":None,"machine_checks":{"matrix_saved":True,"matrix_extracted":expected}}
    try:yield item,binding,result,case,CID
    finally:
        from app import run_store
        run_store.finish(binding["run_id"],"interrupted",attempt_id=binding["attempt_id"])


def test_empty_saved_matrix_or_legacy_migration_cannot_pass_extraction(matrix_harness_case):
    from app import research_tools
    item,binding,result,case,cid=matrix_harness_case
    migrated=research_tools.execute("extract_evidence_matrix",{"matrix_id":item.id},conversation_id=cid)
    assert migrated["not_executed"]and migrated["code"]=="migration_required"
    result["tools"].append("extract_evidence_matrix")
    checks=runner.evaluate_receipts(case,result,None,cid,"unused")
    assert checks["matrix_saved"]and not checks["matrix_extracted"]


def test_completed_matrix_child_proves_saved_coverage_and_exports_actual_result(db,monkeypatch,matrix_harness_case):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import research_tools
    from app.runtime_context import bind_runtime_config
    from tests.test_matrix_runs import api_embeddings
    from tests.test_research_children import arguments
    item,binding,result,case,cid=matrix_harness_case
    calls=api_embeddings(db,item,monkeypatch)
    with bind_runtime_config(binding):
        value=research_tools.execute("start_research_child",arguments(item),conversation_id=cid)
        readback=research_tools.execute("read_research_child",{"child_run_id":value["run_id"]},conversation_id=cid)
    assert value["delivery_verified"]and readback["result_sha256"]==value["result_sha256"]and len(calls)==2,value
    result["tools"]+=["start_research_child","read_research_child"]
    checks=runner.evaluate_receipts(case,result,None,cid,"unused")
    assert checks["matrix_extracted"]
    receipt=runner.matrix_extraction_receipts(result,cid,case["machine_checks"]["matrix_extracted"])[0]
    assert receipt["cell_count"]==4 and receipt["semantic_support"]=="unverified"
    assert receipt["research_delivery"]["child_run_id"]==value["run_id"]
    saved=runner.collect_research_outputs(TestClient(app),cid,result)
    assert saved["research_children"][0]["run_id"]==value["run_id"]
    export=saved["matrix_exports"][0]
    assert export["exported_version"]==2 and export["history"][-1]["document"]["matrix_run"]["run_id"]==value["run_id"]
    assert len(calls)==2  # Receipt verification and output collection do not rebuy extraction.


@pytest.mark.parametrize("changed",["partial_fields","partial_outcome","missing_link","wrong_parent","wrong_fields","wrong_sources","saved_sha","current_changed"])
def test_matrix_machine_contract_rejects_incomplete_or_unrelated_receipts(db,monkeypatch,matrix_harness_case,changed):
    from sqlalchemy import delete,update,select
    from app import research_children as children
    from app.research_models import ResearchChildLink,MatrixRevision
    from app.runtime_models import RuntimeRun,RunAttempt
    from tests.test_research_children import arguments
    item,binding,result,case,cid=matrix_harness_case
    args=arguments(item)
    if changed=="partial_fields":args["inputs"]["field_keys"]=["method"]
    value=children.execute(children.START,args,binding=binding)
    assert value["outcome"]=="completed"
    if changed=="partial_outcome":
        attempt=db.get(RunAttempt,value["attempt_id"],populate_existing=True)
        checkpoint=deepcopy(attempt.checkpoint);checkpoint["completed"]=False
        attempt.checkpoint=checkpoint
        db.get(RuntimeRun,value["run_id"],populate_existing=True).outcome="partial"
    elif changed=="missing_link":db.execute(delete(ResearchChildLink).where(ResearchChildLink.child_run_id==value["run_id"]))
    elif changed=="wrong_parent":result["turns"][0]["run_id"]="not-this-parent"
    elif changed=="wrong_fields":case["machine_checks"]["matrix_extracted"]["field_labels"]=["Different field","Not located"]
    elif changed=="wrong_sources":result["source_revision_ids"]["alpha-b.md"]=result["source_revision_ids"]["alpha-a.md"]
    elif changed=="saved_sha":
        revision=db.scalar(select(MatrixRevision).where(MatrixRevision.matrix_id==item.id,MatrixRevision.version==2))
        forged=deepcopy(revision.document);forged["matrix_run"]["document_sha256"]="0"*64
        db.execute(update(MatrixRevision).where(MatrixRevision.id==revision.id).values(document=forged))
    elif changed=="current_changed":
        from app.research_models import ResearchMatrix
        current=db.get(ResearchMatrix,item.id,populate_existing=True)
        changed_document=deepcopy(current.document);changed_document["dimensions"][0]["label"]="Lost user field"
        current.document=changed_document;current.version+=1
    db.commit()
    assert not runner.evaluate_receipts(case,result,None,cid,"unused")["matrix_extracted"]


@pytest.mark.parametrize("name",["list_evidence_matrices","read_evidence_matrix","create_evidence_matrix",
    "update_evidence_matrix","start_research_child","resume_research_child","read_research_child"])
def test_daily_no_sources_check_includes_durable_research_tools(db,name):
    result={"answer":"Local mechanics.","turns":[{"http_status":200,"answer":"Local mechanics.",
        "outcome":"completed","completed":True}],"tools":[name]}
    checks=runner.evaluate_receipts({"control":None,"machine_checks":{"no_source_tools":True}},result,None,"unused","unused")
    assert not checks["no_source_tools"]
