"""Local durable child receipts; this fixture never calls an external tool."""
from copy import deepcopy
from uuid import uuid4
from app.main import app
_original_routes = list(app.router.routes)
from app.db import SessionLocal
from app.models import Conversation, ForegroundRun
from app.runtime_models import RuntimeRun
from app.agent.checkpoint import AgentState
from app.agent import subagent
from app import subtask_store

subagent._safe_tool_specs = lambda: [{'name': 'mcp__fixture__search'}]


@app.post('/api/test-fixture/subtasks/seed')
def seed():
    cid, rid = uuid4().hex, 'chat_' + uuid4().hex
    task = {'topic': '核对论文方法与证据', 'goal': '保留支持、冲突与未找到的证据'}
    binding = {'run_id': rid, 'attempt_id': 'fixture-parent-a', 'conversation_id': cid, 'model_id': 'synthetic'}
    call = {'id': 'fixture-external', 'name': 'mcp__fixture__search', 'arguments': {'query': '本地合成研究任务'}}
    state = AgentState(question='子任务恢复合成测试', conversation_id=cid, phase='tools', runtime_config=binding,
        calls=[{'id': 'spawn', 'name': 'spawn_subagent', 'arguments': {'tasks': [task]}}],
        pending_operation={'kind': 'tool', 'key': 'tool/1/0', 'name': 'spawn_subagent', 'arguments': {'tasks': [task]}})
    with SessionLocal.begin() as db:
        db.add_all([Conversation(id=cid, title='子任务恢复合成测试'), RuntimeRun(id=rid, kind='foreground', conversation_id=cid,
            current_attempt_id='fixture-parent-a', status='interrupted', revision=2),
            ForegroundRun(id=rid, conversation_id=cid, question=state.question, status='interrupted', runtime_config=binding, agent_checkpoint=state.snapshot())])
    with SessionLocal() as db:
        scope = subtask_store.child_scope(db, binding)
    node, _ = subtask_store.claim(parent_run_id=rid, parent_attempt_id='fixture-parent-a', specification=task, scope=scope, conversation_id=cid)
    from app.runtime_context import bind_runtime_config
    from app import call_ledger
    with bind_runtime_config({**binding, 'subtask_id': node['id'], 'subtask_attempt': node['attempt'], 'subtask_writer_token': node['writer_token']}):
        identity = call_ledger.begin_call(provider='synthetic.local', service='chat', endpoint='https://synthetic.local/chat', model_id='synthetic-child', synthetic=True)
        call_ledger.finish_call(identity, usage={'prompt_tokens': 100, 'completion_tokens': 20})
    cp = {**node['checkpoint'], 'messages': [], 'collected': [], 'calls': [call], 'tool_index': 0, 'pending_operation': deepcopy(call)}
    subtask_store.checkpoint(node['id'], node['writer_token'], cp, state='blocked')
    return {'conversation_id': cid, 'run_id': rid, 'node_id': node['id'], 'real_provider_calls': 0}


@app.post('/api/test-fixture/subtasks/{run_id}/revision')
def update_revision(run_id: str):
    with SessionLocal.begin() as db:
        db.get(RuntimeRun, run_id).revision += 1
    return {'ok': True}


app.router.routes[:] = [route for route in app.router.routes if route not in _original_routes] + _original_routes
