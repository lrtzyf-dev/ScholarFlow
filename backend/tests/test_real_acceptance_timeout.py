"""Controlled executor deadlines only; zero external requests or quality claims."""
from copy import deepcopy
import json
import threading

import httpx
import pytest

from scripts import verify_real_acceptance as runner


UNKNOWN_USAGE={"call_count":1,"actual_usage":{},"estimated_usage":{},"unknown_usage_calls":1,
    "unknown_amount_calls":1,"amount_complete":False,"amounts":{},"pricing":{"unknown_calls":1}}


class ControlledClient:
    """Actual pool workers block on a barrier; snapshots use production channels."""
    def __init__(self,*,release_on_cancel=False,first_fast=False,cancel_status=200):
        self.release=threading.Event();self.started=threading.Event();self.channels={};self.bindings={}
        self.cancels=[];self.posts=[];self.release_on_cancel=release_on_cancel;self.first_fast=first_fast
        self.cancel_status=cancel_status;self.guard=threading.Lock()

    @staticmethod
    def response(value=None,events=None,status=200):
        return httpx.Response(status,json=value)if events is None else httpx.Response(status,
            text="\n".join("data:"+json.dumps(e)for e in events))

    def post(self,path,json=None,**kwargs):
        from app import chat_events
        if path=="/api/conversations":return self.response({"id":"other-conversation"})
        if path=="/api/foreground-runs":
            rid=json["id"];cid=json["conversation_id"]
            channel=chat_events.begin(rid,cid)
            channel.record("answer_delta",{"text":"Public partial "+cid})
            channel.record("thinking_delta",{"text":"PRIVATE-THINKING"})
            channel.record("tool_call",{"name":"read_attached_source","arguments":{"api_key":"PRIVATE-ARGS"}})
            channel.record("audit",{"audit":{"status":"failed","reason":"Public mismatch","provider_context":"PRIVATE-AUDIT"}})
            with self.guard:self.channels[rid]=channel;self.bindings[rid]=cid;self.posts.append(deepcopy(json))
            self.started.set()
            return self.response({"id":rid})
        if path=="/api/chat/stream":
            rid=json["run_id"];cid=json["conversation_id"]
            if not(self.first_fast and json["question"]=="first"):
                assert self.release.wait(10),"Test worker barrier was not released"
            final={"kind":"answer","answer":"Public final "+cid,"completed":True,"outcome":"completed",
                "evidence":[{"id":"e1","revision_id":"revision-"+cid,"locator_id":"locator-"+cid,
                    "quote":"Public source quote","source_location":{"page_number":2,"paragraph_index":4,
                        "provider_context":"PRIVATE-LOCATION"},"reasoning_content":"PRIVATE-EVIDENCE"}],
                "audit":{"status":"passed","reasoning_content":"PRIVATE-FINAL-AUDIT"},
                "provider_context":"PRIVATE-FINAL","agent_checkpoint":"PRIVATE-CHECKPOINT"}
            channel=self.channels[rid];channel.record("answer",final);channel.end("completed")
            return self.response(events=[final])
        if path.endswith("/cancel"):
            rid=path.split("/")[-2]
            with self.guard:self.cancels.append(rid)
            if self.release_on_cancel:self.release.set()
            return self.response({"run_id":rid,"status":"cancelling","provider_context":"PRIVATE-CANCEL"},status=self.cancel_status)
        raise AssertionError(path)

    def get(self,path,params=None):
        if path=="/api/foreground-runs":
            with self.guard:rows=[{"id":rid,"status":"running","workspace_revision":0}
                for rid,cid in self.bindings.items()if cid==params["conversation_id"]]
            return self.response({"runs":rows})
        if path.endswith("/events"):
            assert self.release.wait(10),"Test observer barrier was not released"
            rid=path.split("/")[-2]
            return self.response(events=[self.channels[rid].snapshot()])
        raise AssertionError(path)


@pytest.fixture
def controlled(monkeypatch):
    from app import call_ledger
    monkeypatch.setattr(call_ledger,"summarize_calls",lambda **kwargs:deepcopy(UNKNOWN_USAGE))
    clients=[]
    def create(**kwargs):
        value=ControlledClient(**kwargs);clients.append(value);return value
    yield create
    for client in clients:client.release.set()


def timeout(client,*,control=None,prompts=None,grace=0,drafts=None):
    stopped=threading.Event()
    with pytest.raises(runner.ChatRunTimeout)as caught:
        runner.run_chat(client,{"prompts":prompts or["slow"],"control":control},"main-conversation",{},.25,
            on_timeout=stopped.set,cleanup_grace_seconds=grace,citation_drafts=drafts)
    assert stopped.is_set()
    return caught.value


def test_deadline_retains_public_partial_and_unknown_usage_without_claiming_stop(controlled):
    client=controlled();error=timeout(client)
    try:
        result=error.partial_result;turn=result["turns"][0];rid=turn["run_id"]
        assert turn["diagnostics"]["public_streamed_answer"]=="Public partial main-conversation"
        assert turn["diagnostics"]["events"][0]["audit"]["reason"]=="Public mismatch"
        assert turn["tools"]==["read_attached_source"]
        assert result["usage_by_run"][rid]==UNKNOWN_USAGE
        assert result["timeout"]["new_paid_dispatch_fenced"]is True
        assert result["timeout"]["workers_settled"]is False
        receipt=result["timeout"]["cancellations"][0]
        assert receipt["status"]=="cancelling"and receipt["supplier_cancellation_confirmed"]is False
        assert client.cancels==[rid]
        assert "PRIVATE-"not in json.dumps(result)
    finally:client.release.set();error.finish_cleanup()


def test_main_failure_retention_persists_before_wait_then_keeps_late_final_and_drafts(controlled,tmp_path):
    client=controlled();drafts={};error=timeout(client,drafts=drafts)
    rid=error.partial_result["turns"][0]["run_id"]
    drafts[rid]=[{"answer":"Public reviewed draft","gate":{"passed":False,"reasoning_content":"PRIVATE-DRAFT"}}]
    entry={"id":"synthetic-timeout","passed":False};saved=[];target=tmp_path/"timeout.json"
    def persist():
        runner.write_report(target,entry)
        saved.append(json.loads(target.read_text(encoding="utf-8")))
        if len(saved)==1:
            assert not entry["partial_result"]["timeout"]["workers_settled"]
            client.release.set()
    try:runner.retain_chat_timeout(entry,error,persist)
    finally:client.release.set();error.finish_cleanup()
    assert len(saved)==2 and all(s["status"]=="execution_failed"for s in saved)
    assert all(s["machine_receipts_passed"]is False and s["passed"]is False for s in saved)
    final=saved[-1]["partial_result"];turn=final["turns"][0]
    assert final["timeout"]["workers_settled"]is True
    assert turn["answer"]=="Public final main-conversation"and turn["completed"]is True
    assert turn["evidence"][0]["revision_id"]=="revision-main-conversation"
    assert turn["evidence"][0]["source_location"]=={"page_number":2,"paragraph_index":4}
    assert turn["public_citation_drafts"][0]["answer"]=="Public reviewed draft"
    assert saved[-1]["usage_by_run"][rid]==UNKNOWN_USAGE
    assert client.cancels==[rid]and "PRIVATE-"not in target.read_text(encoding="utf-8")


def test_final_arriving_during_cancel_grace_still_exceeds_deadline(controlled):
    client=controlled(release_on_cancel=True);error=timeout(client,grace=1)
    try:
        assert error.partial_result["turns"][0]["answer"]=="Public final main-conversation"
        assert error.partial_result["timeout"]["workers_settled"]is True
        assert error.partial_result["timeout"]["deadline_exceeded"]is True
        assert error.partial_result["timeout"]["machine_receipts_passed"]is False
    finally:error.finish_cleanup()


def test_explicit_control_cancellation_is_not_repeated_after_timeout(controlled):
    client=controlled();error=timeout(client,control={"kind":"cancel"})
    try:
        assert len(client.cancels)==1
        assert error.partial_result["controller"]["cancel_http"]==200
        assert len(error.partial_result["timeout"]["cancellations"])==1
    finally:client.release.set();error.finish_cleanup()


def test_concurrent_timeout_cancels_and_keeps_each_exact_run_separately(controlled):
    client=controlled();error=timeout(client,control={"kind":"concurrent","other":"also slow"})
    try:
        result=error.partial_result;primary=result["turns"][0];other=result["related_runs"][0]
        assert primary["run_id"]!=other["run_id"]
        assert primary["conversation_id"]=="main-conversation"and other["conversation_id"]=="other-conversation"
        assert primary["diagnostics"]["public_streamed_answer"]=="Public partial main-conversation"
        assert other["diagnostics"]["public_streamed_answer"]=="Public partial other-conversation"
        assert set(client.cancels)=={primary["run_id"],other["run_id"]}and len(client.cancels)==2
        assert set(result["usage_by_run"])==set(client.cancels)
    finally:client.release.set();error.finish_cleanup()


def test_reattach_observer_does_not_create_an_extra_cancel_or_provider_call(controlled):
    client=controlled();error=timeout(client,control={"kind":"reattach"})
    try:
        assert error.partial_result["controller"]["live_observation_started"]is True
        assert len(client.cancels)==len(client.posts)==1
    finally:client.release.set();error.finish_cleanup()


def test_preceding_completed_turn_survives_later_turn_timeout(controlled):
    client=controlled(first_fast=True);error=timeout(client,prompts=["first","second"])
    try:
        turns=error.partial_result["turns"]
        assert len(turns)==2 and turns[0]["completed"]is True and turns[1]["completed"]is None
        assert len(error.partial_result["usage_by_run"])==2
        assert client.cancels==[turns[1]["run_id"]]
    finally:client.release.set();error.finish_cleanup()


@pytest.mark.parametrize("cancel_status",[409,500])
def test_failed_cancel_receipt_remains_failed_and_unknown(controlled,cancel_status):
    client=controlled(cancel_status=cancel_status);error=timeout(client)
    try:
        receipt=error.partial_result["timeout"]["cancellations"][0]
        assert receipt["http_status"]==cancel_status
        assert receipt["supplier_cancellation_confirmed"]is False
        assert len(client.cancels)==1
        assert list(error.partial_result["usage_by_run"].values())==[UNKNOWN_USAGE]
    finally:client.release.set();error.finish_cleanup()


@pytest.mark.parametrize("rid,cid",[("foreign","main-conversation"),("run","foreign")])
def test_foreign_snapshot_is_not_retained_as_current_run_output(rid,cid):
    result=runner.public_chat_turn("run","main-conversation",snapshot={"run_id":rid,"conversation_id":cid,
        "partial_answer":"OTHER-OUTPUT","result":{"answer":"OTHER-OUTPUT","evidence":[{"quote":"OTHER-OUTPUT"}]}})
    assert "OTHER-OUTPUT"not in json.dumps(result)


def test_normal_public_projection_retains_output_and_excludes_private_nested_payload():
    response=ControlledClient.response(events=[{"kind":"answer","answer":"Public completed answer", "completed":True,
        "outcome":"completed","plan_steps":[{"title":"Review","status":"done","provider_context":"PRIVATE-PLAN"}],
        "evidence":[{"revision_id":"fixed","locator_id":"located","quote":"Public quote","api_key":"PRIVATE-KEY"}],
        "provider_context":"PRIVATE-PROVIDER"}])
    result=runner.public_chat_turn("run","conversation",response=response)
    assert result["answer"]=="Public completed answer"and result["completed"]is True
    assert result["evidence"][0]["locator_id"]=="located"
    assert result["plan_steps"]==[{"title":"Review","status":"done"}]
    assert "PRIVATE-"not in json.dumps(result)


def test_worker_timeout_error_does_not_fabricate_an_expired_harness_deadline(controlled):
    client=controlled();original=client.post
    def post(path,**kwargs):
        if path=="/api/chat/stream":raise TimeoutError("PRIVATE-WORKER-ERROR")
        return original(path,**kwargs)
    client.post=post;stopped=threading.Event()
    with pytest.raises(TimeoutError)as caught:
        runner.run_chat(client,{"prompts":["immediate worker error"]},"main-conversation",{},5,on_timeout=stopped.set)
    assert not isinstance(caught.value,runner.ChatRunTimeout)
    assert not stopped.is_set()and not client.cancels


def test_cleanup_is_idempotent_without_new_cancellations(controlled):
    client=controlled();error=timeout(client)
    client.release.set();first=error.finish_cleanup();second=error.finish_cleanup()
    assert first is second and first["timeout"]["workers_settled"]
    assert len(client.cancels)==1


def test_real_main_timeout_path_saves_evidence_and_fences_http_through_cleanup(monkeypatch,tmp_path):
    """Run main's actual failure branch, with no credentials or external I/O."""
    from app import task_runner
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    monkeypatch.setattr(runner,"model_configuration",lambda *args:{"api_key":"synthetic-secret-not-a-real-key",
        "base_url":"https://api.deepseek.com/v1","model_id":"synthetic-model","storage_kind":"synthetic_fixture"})
    monkeypatch.setattr(runner,"isolated_environment",lambda *args:{})
    transports=[];cleanup=[];destination=tmp_path/"main-timeout.json"
    def no_network(*args,**kwargs):
        transports.append(True)
        raise AssertionError("A timed-out acceptance attempted an external transport")
    monkeypatch.setattr(httpx.HTTPTransport,"handle_request",no_network)
    def exercise(client,case,cid,sources,seconds,*,on_timeout,**kwargs):
        partial={"turns":[{"run_id":"chat_synthetic_timeout","conversation_id":cid,"answer":"Public partial answer"}],
            "usage_by_run":{"chat_synthetic_timeout":deepcopy(UNKNOWN_USAGE)},
            "timeout":{"workers_settled":False,"deadline_exceeded":True}}
        on_timeout()
        def check_fence():
            with pytest.raises(RuntimeError,match="EvaluationStoppedAfterTimeout"):
                httpx.HTTPTransport().handle_request(httpx.Request("POST","https://api.deepseek.com/chat/completions"))
        check_fence()
        def finish():
            stored=json.loads(destination.read_text(encoding="utf-8"))
            assert stored["cases"][0]["status"]=="execution_failed"
            assert stored["cases"][0]["partial_result"]["turns"][0]["answer"]=="Public partial answer"
            assert stored["cases"][0]["partial_result"]["timeout"]["workers_settled"]is False
            check_fence();cleanup.append(True)
            final=deepcopy(partial);final["timeout"]["workers_settled"]=True
            return final
        raise runner.ChatRunTimeout(partial,finish)
    monkeypatch.setattr(runner,"run_chat",exercise)
    assert runner.main(["--run-real","--case","DAILY01","--report",str(destination),
        "--credentials-dir",str(tmp_path),"--timeout-seconds","15"])==1
    stored=json.loads(destination.read_text(encoding="utf-8"));entry=stored["cases"][0]
    assert cleanup==[True]and transports==[]and stored["real_provider_calls"]==0
    assert entry["status"]=="execution_failed"and entry["passed"]is False
    assert entry["machine_receipts_passed"]is False and stored["passed"]is False
    assert entry["partial_result"]["timeout"]["workers_settled"]is True
    assert entry["usage_by_run"]["chat_synthetic_timeout"]==UNKNOWN_USAGE
    assert "synthetic-secret"not in destination.read_text(encoding="utf-8")


def test_unavailable_ledger_cannot_erase_partial_output_or_invent_zero_usage(controlled,monkeypatch):
    from app import call_ledger
    def unavailable(**kwargs):raise RuntimeError("PRIVATE-LEDGER-DETAIL")
    monkeypatch.setattr(call_ledger,"summarize_calls",unavailable)
    client=controlled();error=timeout(client)
    try:
        assert error.partial_result["turns"][0]["diagnostics"]["public_streamed_answer"]=="Public partial main-conversation"
        receipt=next(iter(error.partial_result["usage_by_run"].values()))
        assert receipt=={"status":"unavailable","usage_status":"unknown","amount_status":"unknown","error_kind":"RuntimeError"}
        assert "PRIVATE-"not in json.dumps(error.partial_result)
    finally:client.release.set();error.finish_cleanup()


def test_cleanup_diagnostic_error_preserves_first_failed_public_evidence():
    partial={"turns":[{"answer":"Public prior output"}],"timeout":{"workers_settled":False}}
    def failed_refresh():raise RuntimeError("PRIVATE-CLEANUP-DETAIL")
    error=runner.ChatRunTimeout(partial,failed_refresh);entry={};saved=[]
    runner.retain_chat_timeout(entry,error,lambda:saved.append(deepcopy(entry)))
    assert len(saved)==2 and all(s["status"]=="execution_failed"for s in saved)
    assert saved[-1]["partial_result"]==partial
    assert saved[-1]["timeout_cleanup_error_kind"]=="RuntimeError"
    assert "PRIVATE-"not in json.dumps(saved)


def test_cleanup_still_runs_when_initial_evidence_cannot_be_persisted():
    cleaned=[];error=runner.ChatRunTimeout({},lambda:cleaned.append(True)or{})
    def failed_persist():raise ValueError("SyntheticPersistenceFailure")
    with pytest.raises(ValueError,match="SyntheticPersistenceFailure"):
        runner.retain_chat_timeout({},error,failed_persist)
    assert cleaned==[True]


def test_public_fixed_locator_keeps_pdf_geometry_and_integrity_metadata():
    location={"page_number":3,"page_index":2,"page_index_base":0,"page_count":14,
        "bbox":[10,20,100,120],"page_bbox":[0,0,595,842],"page_width":595,"page_height":842,
        "rotation":0,"coordinate_space":"pdf_points_top_left","unit":"pt","geometry_verified":True,
        "raw_sha256":"a"*64,"text_sha256":"b"*64,"fingerprint":"c"*64,"integrity_verified":False}
    raw={"revision_id":"fixed-revision","locator_id":"fixed-locator","source_location":{
        **location,"provider_context":"PRIVATE-CONTEXT","reasoning_content":"PRIVATE-REASONING", "api_key":"PRIVATE-KEY"}}
    value=runner.public_evidence([raw])[0]
    assert value["source_location"]==location
    assert value["revision_id"]=="fixed-revision"and value["locator_id"]=="fixed-locator"
    assert "PRIVATE-"not in json.dumps(value)
