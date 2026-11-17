"""History edits are new, bounded work; originals and action facts survive."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.models import Conversation, DesktopState, ForegroundRun, QARecord
from app.history_models import HistoryRevision
from app import history_revisions as revisions, source_store, data_cleanup
from app.source_runtime import freeze_sources
from app.runtime_context import bind_runtime_config
from app.llm import LLMResponse
from app.timeutil import utcnow

client = TestClient(app)


def seed(db, *, source=False):
    now = utcnow()
    db.add(Conversation(id="revision-original", title="Original"))
    db.flush()
    binding = {}
    if source:
        scope = source_store.SourceScope("conversation", "revision-original")
        item = source_store.capture_source(db, scope=scope,
            authorization=source_store.SourceAuthorization(scope, "revision-input", "upload"), kind="upload",
            title="frozen.md", media_type="text/markdown", raw_bytes=b"# fixed\nvalue 17", text="# fixed\nvalue 17", text_origin="provided_text")
        binding = freeze_sources(db, "revision-original", [item["revision_id"]], {})
    rows = [QARecord(id="qa_before", conversation_id="revision-original", question="Before", answer="Earlier answer", created_at=now),
            QARecord(id="qa_target", conversation_id="revision-original", question="Original question", answer="Original answer",
                     runtime_config=binding, created_at=now + timedelta(seconds=1)),
            QARecord(id="qa_later", conversation_id="revision-original", question="Unrelated later instruction", answer="Later answer", created_at=now+timedelta(seconds=2))]
    db.add_all(rows); db.commit()
    return rows[1]


def request(mode="answer_only", **changes):
    return {"record_id": "qa_target", "question": "Revised question", "mode": mode, "request_id": "revision-request-1", **changes}


def test_revision_is_idempotent_branch_before_target_with_independent_fixed_source(db):
    original = seed(db, source=True)
    old_id = original.runtime_config["source_revision_ids"][0]
    result = client.post("/api/conversations/revision-original/revisions", json=request())
    assert result.status_code == 200, result.text
    branch = result.json()["branch_conversation_id"]
    assert client.post("/api/conversations/revision-original/revisions", json=request()).json() == result.json()
    assert client.post("/api/conversations/revision-original/revisions", json=request(question="conflict")).status_code == 409
    db.expire_all()
    assert db.get(QARecord,"qa_target").answer == "Original answer"
    copied = db.scalars(select(QARecord).where(QARecord.conversation_id == branch)).all()
    assert [row.question for row in copied] == ["Before"]
    draft = db.get(DesktopState, "conversation:"+branch).value
    assert draft["text"] == "Revised question"
    new_id = draft["source_revision_ids"][0]
    assert new_id != old_id
    assert not db.scalars(select(ForegroundRun).where(ForegroundRun.conversation_id==branch)).all()
    data_cleanup.remove(db, "revision-original"); db.commit(); db.expire_all()
    item = source_store.read_revision(db, new_id, scope=source_store.SourceScope("conversation",branch),include_raw=True)
    assert item["raw_bytes"] == b"# fixed\nvalue 17"
    row = db.get(HistoryRevision,result.json()["id"])
    assert row.original_question == "" and row.original_answer_sha256 == ""
    assert revisions.public_revision(row)["origin_deleted"]
    assert row.revised_question == "Revised question"


def test_first_message_revision_creates_usable_empty_branch_and_binding_cannot_escalate(db):
    seed(db)
    result = client.post("/api/conversations/revision-original/revisions", json=request(record_id="qa_before")).json()
    cid = result["branch_conversation_id"]
    assert client.get(f"/api/conversations/{cid}").json()["messages"] == []
    bound = revisions.bind_for_run(db,cid,"chat_revision_run","edited in composer",{"approval_mode":"full_access"})
    assert revisions.answer_only(bound)
    assert revisions.bind_for_run(db,cid,"chat_revision_run","edited in composer",bound)==bound
    with pytest.raises(revisions.HistoryRevisionError):
        revisions.bind_for_run(db,cid,"chat_revision_run","different question",bound)
    assert "history_revision" not in revisions.bind_for_run(db,cid,"chat_subsequent","new turn",bound)


def test_new_task_is_explicit_and_origin_ownership_checked(db):
    seed(db)
    db.add(Conversation(id="other"));db.commit()
    assert client.post("/api/conversations/other/revisions",json=request()).status_code==404
    result=client.post("/api/conversations/revision-original/revisions",json=request("new_task")).json()
    assert not revisions.answer_only(revisions.bind_for_run(db,result["branch_conversation_id"],"chat_new_task","new request",{}))


@pytest.mark.parametrize("approval_mode", ["auto", "full_access"])
def test_answer_revision_blocks_injected_writes_services_children_and_hooks(db, monkeypatch, approval_mode):
    from app.agent import loop
    from app import approval_store
    names = ["list_papers", "edit_project_file", "run_project_command", "deepseek_research", "spawn_subagent", "mcp__custom__write"]
    class Model:
        def __init__(self): self.specs=[];self.requests=[]
        def stream_chat(self,messages,tools=None):
            self.specs=deepcopy(tools);self.requests.append(deepcopy(messages))
            if not any(message["role"]=="tool" for message in messages):
                yield "final",LLMResponse("",[{"id":"call_"+str(i),"name":name,"arguments":{}} for i,name in enumerate(names)])
            else:
                yield "final",LLMResponse("The revised answer uses existing information. No external action was executed.")
    model=Model();executed=[]
    monkeypatch.setattr(loop,"get_llm",lambda:model)
    monkeypatch.setattr(loop,"get_tool_specs",lambda:[{"name":name,"parameters":{"type":"object","properties":{}}} for name in names])
    monkeypatch.setattr(loop.settings,"memory_recall",False)
    monkeypatch.setattr(loop,"execute_tool",lambda db,name,args,**kw: executed.append(name) or '{"papers":[]}')
    monkeypatch.setattr(loop.hooks_store,"run_hooks",lambda *a,**kw:pytest.fail("revision executed a command hook"))
    monkeypatch.setattr(approval_store,"create",lambda *a,**kw:pytest.fail("revision requested write approval"))
    binding={"history_revision":{"id":"revision","mode":"answer_only"},"approval_mode":approval_mode}
    events=list(loop.run_agent(db,"Please revise the response",conversation_id="revision-run",runtime_config=binding))
    assert executed==["list_papers"]
    assert {event["name"] for kind,event in events if kind=="policy_block"}==set(names[1:])
    assert not set(names[1:]) & {s["name"] for s in model.specs}
    assert any("历史答案修订" in str(msg) for msg in model.requests[0])
    from app.agent.tools import execute_tool
    with bind_runtime_config(binding):
        assert json.loads(execute_tool(db,"save_research_artifact",{},conversation_id="revision-run"))["blocked_by_history_revision"]


def test_revision_policy_is_bound_by_real_chat_admission_not_client_flag(db, monkeypatch):
    seed(db)
    result=client.post("/api/conversations/revision-original/revisions",json=request()).json()
    cid=result["branch_conversation_id"]
    from app.api import chat
    captured=[]
    def fake_agent(*args,**kwargs):
        captured.append(kwargs["runtime_config"])
        yield "answer",{"answer":"Revised", "completed":True,"outcome":"completed"}
    monkeypatch.setattr(chat,"run_agent",fake_agent)
    response=client.post("/api/chat/stream",json={"conversation_id":cid,"question":"draft edited before sending","run_id":"chat_history_binding_run"})
    assert response.status_code==200,response.text
    assert captured and revisions.answer_only(captured[0])
    db.expire_all()
    row=db.get(HistoryRevision,result["id"])
    assert row.run_id=="chat_history_binding_run" and row.submitted_question=="draft edited before sending"
