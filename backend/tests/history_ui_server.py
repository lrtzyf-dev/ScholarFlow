"""UI fixture only: isolated data plus a transport that cannot call real APIs."""
import sys
from datetime import timedelta
from provider_adapter_ui_server import app, calls
from app.db import SessionLocal
from app.models import Conversation, QARecord, ForegroundRun, TaskRecord
from app.resource_models import PersistentSubtask
from app.source_runtime import freeze_sources
from app import source_store
from app.timeutil import utcnow


@app.post("/__test__/seed-history")
def seed():
    with SessionLocal() as db:
        now=utcnow()
        db.add(Conversation(id="history-ui",title="修订验收会话"));db.flush()
        scope=source_store.SourceScope("conversation","history-ui")
        item=source_store.capture_source(db,scope=scope,
            authorization=source_store.SourceAuthorization(scope,"ui-history-source","upload"),kind="upload",
            title="fixed-version.md",media_type="text/markdown",raw_bytes=b"# fixed version\nValue: 17",text="# fixed version\nValue: 17",text_origin="provided_text")
        binding=freeze_sources(db,"history-ui",[item["revision_id"]],{})
        db.add_all([
            QARecord(id="qa_ui_before",conversation_id="history-ui",question="前一条问题",answer="前一条答案",created_at=now),
            QARecord(id="qa_ui_target",conversation_id="history-ui",question="原始问题",answer="原始答案",runtime_config=binding,created_at=now+timedelta(seconds=1)),
            QARecord(id="qa_ui_later",conversation_id="history-ui",question="后续问题不应混入",answer="后续答案",created_at=now+timedelta(seconds=2)),
            ForegroundRun(id="chat_ui_budget",conversation_id="history-ui",question="等待预算的长任务",status="paused",agent_checkpoint={"resource_pause":{"kind":"budget"}}),
            TaskRecord(id="ui_partial",conversation_id="history-ui",type="agent",title="比较未全部完成",status="done",result={"completed":False,"outcome":"partial","qa_id":"qa_ui_target"}),
            PersistentSubtask(id="ui_child",parent_run_id="ui_partial",conversation_id="history-ui",fingerprint="ui-child",specification={"topic":"子任务分析"},state="interrupted"),
        ]);db.commit()
        return {"conversation_id":"history-ui","source_id":item["revision_id"]}

app.router.routes.insert(0,app.router.routes.pop())

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=int(sys.argv[1]))
