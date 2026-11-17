"""One read projection, correct goal state, durable quiet notifications."""
from datetime import timedelta
import json
from fastapi.testclient import TestClient
from app.main import app
from app.models import Conversation, DesktopState, ForegroundRun, TaskRecord
from app.runtime_models import RuntimeRun
from app.resource_models import PersistentSubtask
from app.run_inputs import RunInput
from app import approval_store, run_center
from app.timeutil import utcnow

client=TestClient(app)


def seed(db):
    now=utcnow()
    db.add(Conversation(id="center",title="Research"))
    db.add_all([
        ForegroundRun(id="chat_running_center",conversation_id="center",question="Current chat",status="running"),
        ForegroundRun(id="chat_budget_center",conversation_id="center",question="Budget",status="paused",agent_checkpoint={"resource_pause":{"kind":"budget"},"private":"do-not-expose"}),
        ForegroundRun(id="chat_input_center",conversation_id="center",question="Question",status="completed",agent_checkpoint={"result":{"outcome":"partial","stop_reason":"needs_clarification","qa_id":"qa_clarify"}}),
        TaskRecord(id="bg_partial",conversation_id="center",type="agent",title="Partial",status="done",result={"outcome":"partial","completed":False,"qa_id":"qa_partial","provider_context":{"signature":"secret"}}),
        TaskRecord(id="bg_done",conversation_id="center",type="agent",title="Done",status="done",result={"completed":True}),
        PersistentSubtask(id="child",parent_run_id="bg_partial",conversation_id="center",fingerprint="child",specification={"topic":"Analyze methods"},state="completed",scope={"credential_ref":"secret"}),
        RunInput(input_id="next",run_id="chat_running_center",conversation_id="center",kind="next_turn",content="Then compare",workspace_revision=0,revision=1,status="queued"),
        RuntimeRun(id="orphan",conversation_id="center",kind="foreground",status="ended",outcome="open",updated_at=now-timedelta(seconds=1)),
    ]);db.commit()


def test_all_execution_kinds_and_distinct_wait_reasons_without_private_payload(db,monkeypatch):
    monkeypatch.setattr(approval_store,"_pending",{})
    seed(db)
    pending=approval_store.create("write",{"secret":"must-not-escape"},"guarded",run_id="chat_running_center")
    data=client.get("/api/run-center?view=all").json()
    byid={item["run_id"]:item for item in data["items"] if item["kind"]!="input"}
    assert byid["chat_running_center"]["status"]=="waiting_permission"
    assert byid["chat_budget_center"]["status"]=="waiting_budget"
    assert byid["chat_input_center"]["status"]=="waiting_input"
    assert byid["bg_partial"]["status"]=="partial"
    assert byid["bg_done"]["status"]=="completed"
    assert byid["orphan"]["status"]=="ended"
    assert byid["child"]["target_run_id"]=="bg_partial" and byid["child"]["target_kind"]=="background"
    assert set(item["kind"] for item in data["items"])=={"foreground","background","subtask","input","run"}
    assert not any(secret in json.dumps(data) for secret in ["do-not-expose","must-not-escape","credential_ref","signature"])
    approval_store.discard(pending.id)


def test_status_filter_pagination_equal_timestamps_no_duplicates_and_scoped_cursor(db):
    stamp=utcnow()
    db.add(Conversation(id="many"))
    for i in range(15):
        db.add(TaskRecord(id=f"task_{i:02}",conversation_id="many",title=str(i),type="agent",status="done",result={"completed":False},updated_at=stamp))
    db.commit()
    first=client.get("/api/run-center?status=partial&limit=7").json()
    second=client.get("/api/run-center",params={"status":"partial","limit":7,"cursor":first["next_cursor"]}).json()
    third=client.get("/api/run-center",params={"status":"partial","limit":7,"cursor":second["next_cursor"]}).json()
    assert first["total"]==15
    assert len({item["item_id"] for page in [first,second,third] for item in page["items"]})==15
    assert client.get("/api/run-center",params={"view":"history","cursor":first["next_cursor"]}).status_code==422
    assert client.get("/api/run-center?cursor=bad-base64").status_code==422


def test_read_receipts_survive_new_session_but_stale_receipt_does_not_ack_new_attempt(db):
    seed(db)
    item=next(v for v in client.get("/api/run-center").json()["items"] if v["run_id"]=="bg_partial")
    assert item["unread"]
    assert client.post("/api/run-center/read",json={"events":[item]}).status_code==200
    assert not next(v for v in client.get("/api/run-center").json()["items"] if v["run_id"]=="bg_partial")["unread"]
    db.add(RuntimeRun(id="bg_partial",kind="background",conversation_id="center",status="ended",outcome="partial",current_attempt_id="new-attempt"));db.commit()
    assert client.post("/api/run-center/read",json={"events":[item]}).json()["acknowledged"]==[]
    assert next(v for v in client.get("/api/run-center").json()["items"] if v["run_id"]=="bg_partial")["unread"]


def test_notification_claim_deduplicates_across_windows_and_mute_is_persistent(db):
    seed(db)
    payload={"item_id":"background:bg_partial"}
    first=client.post("/api/run-center/notification",json=payload).json()
    assert first["item"]["status"]=="partial"
    assert client.post("/api/run-center/notification",json=payload).json()["item"] is None
    assert client.put("/api/run-center/preferences",json={"muted":True}).status_code==200
    assert client.post("/api/run-center/notification",json={"item_id":"background:bg_done"}).json()["item"] is None
    data=client.get("/api/run-center").json()
    assert data["preferences"]["muted"] and not any(item["unread"] for item in data["items"])


def test_production_dispatched_input_with_unconfirmed_launch_stays_visible(db):
    from app.next_turn_dispatch import dispatch_once
    db.add(Conversation(id="dispatch-center"))
    db.add(ForegroundRun(id="chat_dispatch_parent",conversation_id="dispatch-center",question="parent",status="completed"))
    db.add(ForegroundRun(id="chat_dispatch_child",conversation_id="dispatch-center",question="next",status="running"))
    db.add(RunInput(input_id="dispatch-uncertain",run_id="chat_dispatch_parent",conversation_id="dispatch-center",
        kind="next_turn",content="next",workspace_revision=0,revision=1,status="dispatched",
        child_run_id="chat_dispatch_child",dispatch_started_at=utcnow()))
    db.commit()
    assert dispatch_once()==0
    item=next(v for v in client.get('/api/run-center').json()['items'] if v['kind']=='input')
    assert item['status']=='blocked' and '待核实' in item['error']
    assert item['target_run_id']=='chat_dispatch_parent'
    db.expire_all()
    db.get(ForegroundRun,'chat_dispatch_child').started_at=utcnow();db.commit()
    assert not any(v['kind']=='input' for v in client.get('/api/run-center?view=all').json()['items'])
