"""Disposable returned-unknown UI records, never real tools or user material."""
from uuid import uuid4
from app.main import app
_original_routes = list(app.router.routes)
from app.db import SessionLocal
from app.models import Conversation, ForegroundRun
from app.runtime_models import RuntimeRun
from app.agent.checkpoint import AgentState
from app.operation_receipts import signature


@app.post('/api/test-fixture/operations/seed')
def seed():
    cid, rid, attempt = uuid4().hex, 'chat_' + uuid4().hex, uuid4().hex
    name, args, key = 'mcp__fixture__save_record', {'record': '本地合成核对材料', 'api_key': 'fixture-hidden-secret'}, 'tool/0/0'
    sig = signature(name, args)
    state = AgentState(question='操作结果核对合成测试', conversation_id=cid, phase='model', attempt_id=attempt,
        writer_token='fixture-private-writer', runtime_config={'conversation_id':cid, 'model_id':'synthetic'},
        memory_side_effects={sig:{'operation_key':key,'execution_unknown':True,'user_input_count':0}},
        tool_failures=[{'id':'failure/'+key,'tool_name':name,'arguments':args,'signature':sig,'operation_key':key,
            'phase':'write','status':'open','execution_unknown':True,'error':'本地合成工具已经接收请求，但回复丢失。'}],
        operations={key:{'executed':True,'result':'{"execution_unknown":true,"error":"fixture reply loss"}'}})
    with SessionLocal.begin() as db:
        db.add_all([Conversation(id=cid,title=state.question),RuntimeRun(id=rid,kind='foreground',conversation_id=cid,
            current_attempt_id=attempt,status='interrupted',revision=3),ForegroundRun(id=rid,conversation_id=cid,
            question=state.question,status='interrupted',agent_checkpoint=state.snapshot())])
    return {'run_id':rid,'conversation_id':cid,'real_provider_calls':0}


@app.post('/api/test-fixture/operations/{run_id}/revision')
def revision(run_id: str):
    with SessionLocal.begin() as db:db.get(RuntimeRun,run_id).revision+=1
    return {'ok':True}


app.router.routes[:] = [route for route in app.router.routes if route not in _original_routes] + _original_routes
