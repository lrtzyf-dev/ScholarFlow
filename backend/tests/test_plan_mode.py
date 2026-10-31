"""Planning restrictions and durable, single-owner reviewed-plan handoff."""
import json
import threading
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import approval_store, cancel_store, run_store, task_runner
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.db import _lightweight_migrations
from app.execution_mode import PlanConflict
from app.llm import LLMResponse, MockLLM
from app.main import app
from app.models import Conversation, ForegroundRun, QARecord, TaskRecord

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ('_active', '_finished', '_early_cancels'):
        monkeypatch.setattr(run_store, name, {})
    monkeypatch.setattr(task_runner, '_running', {})
    monkeypatch.setattr(task_runner, '_shutting_down', threading.Event())
    monkeypatch.setattr(cancel_store, '_flags', {})
    monkeypatch.setattr(approval_store, '_pending', {})
    monkeypatch.setattr(loop, 'make_plan', lambda *a: [dict(title='Read and verify', phase='search', status='pending')])
    monkeypatch.setattr(loop.settings, 'memory_recall', False)


class Model:
    def __init__(self, names=()):
        self.names, self.specs, self.messages = names, [], []

    def stream_chat(self, messages, tools=None):
        self.specs = deepcopy(tools)
        self.messages = deepcopy(messages)
        if self.names and not any(m['role'] == 'tool' for m in messages):
            yield 'final', LLMResponse('', [dict(id=f'c{i}', name=name, arguments={}) for i, name in enumerate(self.names)])
        else:
            yield 'final', LLMResponse('Plan: inspect inputs, then produce and verify the output. Nothing has been executed.')


@pytest.mark.parametrize('approval_mode', ['read_only', 'auto', 'full_access'])
def test_plan_filters_specs_and_blocks_injected_calls_before_hooks_or_approval(db, monkeypatch, approval_mode):
    names = ['list_papers', 'spawn_subagent', 'import_paper', 'mcp__custom__search', 'mcp__custom__write']
    model, invoked = Model(names), []
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop, 'get_tool_specs', lambda: [dict(name=n) for n in names])
    monkeypatch.setattr(loop.settings, 'approval_mode', approval_mode)
    from app import mcp_policy
    monkeypatch.setattr(mcp_policy, 'approval_for', lambda name: 'never')
    def forbidden(*a, **k):
        pytest.fail('planning must never invoke command hooks or request write approval')
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', forbidden)
    monkeypatch.setattr(approval_store, 'create', forbidden)
    monkeypatch.setattr(loop, 'execute_tool', lambda db, name, args, **kw: invoked.append(name) or json.dumps({'papers': []}))
    events = list(loop.run_agent(db, 'Create a report', conversation_id='c_plan', execution_mode='plan'))
    assert {s['name'] for s in model.specs} == {'list_papers'}
    assert invoked == ['list_papers']
    assert {e['name'] for k, e in events if k == 'policy_block'} == set(names[1:])
    assert '[SCHOLARFLOW_PLAN_MODE]' in model.messages[0]['content']
    record = db.query(QARecord).one()
    assert record.execution_mode == 'plan' and not record.plan_run_id
    assert all(s['status'] == 'pending' for s in record.plan_steps)
    answer = next(e for k, e in events if k == 'answer')
    assert answer['execution_mode'] == 'plan' and answer['plan_steps'] == record.plan_steps


def test_plan_checkpoint_restores_restrictions_and_rejects_mode_escalation(db, monkeypatch):
    model, snapshots, invoked = Model(['list_papers', 'spawn_subagent']), [], []
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop, 'execute_tool', lambda db, name, args, **k: invoked.append(name) or '{}')
    def forbidden(*a, **k):
        pytest.fail('planning resumed a hook')
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', forbidden)
    source = loop.run_agent(db, 'Question', conversation_id='c_plan', execution_mode='plan',
                            save_checkpoint=lambda snapshot: snapshots.append(deepcopy(snapshot)))
    for kind, _ in source:
        if kind == 'tool_result':
            break
    source.close()
    snapshot = snapshots[-1]
    assert snapshot['execution_mode'] == 'plan'
    with pytest.raises(ValueError, match='执行模式'):
        list(loop.run_agent(db, 'Question', conversation_id='c_plan', checkpoint=snapshot))
    events = list(loop.run_agent(db, 'Question', conversation_id='c_plan', execution_mode='plan', checkpoint=snapshot))
    assert invoked == ['list_papers']
    assert any(k == 'policy_block' for k, _ in events)


def seed_plan(db, cid='c_plan', **kwargs):
    db.add(Conversation(id=cid, title='Planning'))
    plan = QARecord(id='qa_plan', conversation_id=cid, question='Task', answer='Read inputs, create report, verify citations.',
                    execution_mode='plan', plan_steps=[dict(title='Create report', phase='write', status='pending')], **kwargs)
    db.add(plan)
    db.commit()
    return plan


def request(run_id='chat_plan_first', **extra):
    return dict(id=run_id, conversation_id='c_plan', question='Execute reviewed plan', reviewed_plan_id='qa_plan', **extra)


def test_reviewed_plan_claim_crosses_foreground_background_and_retry_requires_idle(db, monkeypatch):
    seed_plan(db)
    monkeypatch.setattr(task_runner, '_launch', lambda db, task: task_runner.task_to_dict(task))
    assert client.post('/api/foreground-runs', json=request()).status_code == 200
    assert client.post('/api/foreground-runs', json=request('chat_plan_second')).status_code == 409
    body = dict(question='Run plan', conversation_id='c_plan', reviewed_plan_id='qa_plan')
    assert client.post('/api/tasks/start', json=body).status_code == 409
    body['retry_plan_run_id'] = 'chat_plan_first'
    assert client.post('/api/tasks/start', json=body).status_code == 409
    db.expire_all()
    db.get(ForegroundRun, 'chat_plan_first').status = 'failed'
    db.commit()
    run_store.begin('chat_plan_first', 'c_plan')
    assert client.post('/api/tasks/start', json=body).status_code == 409, 'terminal record can still have active cleanup'
    run_store.finish('chat_plan_first', 'failed')
    result = client.post('/api/tasks/start', json=body)
    assert result.status_code == 200, result.text
    task_id = result.json()['id']
    assert result.json()['reviewed_plan_id'] == 'qa_plan'
    assert client.post('/api/foreground-runs', json=request('chat_plan_third', retry_plan_run_id='chat_plan_first')).status_code == 409
    db.expire_all()
    assert db.get(QARecord, 'qa_plan').plan_run_id == task_id


def test_retry_claim_is_idempotent_between_checkpoint_and_stream_and_injects_server_plan(db, monkeypatch):
    source = seed_plan(db)
    db.add(ForegroundRun(id='chat_plan_old', conversation_id='c_plan', question='Task', status='failed'))
    source.plan_run_id = 'chat_plan_old'
    db.commit()
    model = Model()
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **k: dict(ran=0, blocked=False))
    req = request('chat_plan_retry', retry_plan_run_id='chat_plan_old')
    assert client.post('/api/foreground-runs', json=req).status_code == 200
    response = client.post('/api/chat/stream', json={**req, 'run_id': req['id']})
    assert response.status_code == 200, response.text
    assert 'event: answer' in response.text
    assert source.answer in model.messages[0]['content']
    db.expire_all()
    answer = db.query(QARecord).filter(QARecord.id != source.id).one()
    assert answer.execution_mode == 'execute' and answer.reviewed_plan_id == source.id


def test_reviewed_stream_requires_durable_run_and_cannot_replay_after_registry_restart(db, monkeypatch):
    seed_plan(db)
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **k: dict(ran=0, blocked=False))
    req = {**request(), 'run_id': 'chat_plan_first'}
    assert client.post('/api/chat/stream', json=req).status_code == 409
    assert client.post('/api/foreground-runs', json=request()).status_code == 200
    assert 'event: answer' in client.post('/api/chat/stream', json=req).text
    run_store._active.clear()
    run_store._finished.clear()
    assert client.post('/api/chat/stream', json=req).status_code == 409
    db.expire_all()
    run = db.get(ForegroundRun, 'chat_plan_first')
    assert run.started_at is not None
    # Simulate a crash before the durable run was marked terminal.
    run.status = 'running'
    db.commit()
    assert client.post('/api/chat/stream', json=req).status_code == 409
    db.expire_all()
    assert db.query(QARecord).count() == 2


@pytest.mark.parametrize('patch', [dict(execution_mode='plan'), dict(reviewed_plan_id=None)])
def test_stream_cannot_change_mode_or_plan_source_of_checkpoint(db, patch):
    seed_plan(db)
    assert client.post('/api/foreground-runs', json=request()).status_code == 200
    body = {**request(), 'run_id': 'chat_plan_first', **patch}
    assert client.post('/api/chat/stream', json=body).status_code in (409, 422)


@pytest.mark.parametrize('kind', ['wrong_conversation', 'refused', 'empty', 'normal_answer'])
def test_only_valid_plan_in_same_conversation_can_be_claimed(db, kind):
    source = seed_plan(db)
    if kind == 'wrong_conversation': source.conversation_id = 'c_other'
    elif kind == 'refused': source.refusal = 1
    elif kind == 'empty': source.answer = ' '
    else: source.execution_mode = 'execute'
    db.commit()
    assert client.post('/api/foreground-runs', json=request()).status_code == 409
    db.expire_all()
    assert not db.get(QARecord, source.id).plan_run_id


def test_background_resume_rejects_obsolete_plan_owner_and_mode_mismatch(db, monkeypatch):
    source = seed_plan(db, plan_run_id='task_new')
    snapshot = AgentState(question='Task', conversation_id='c_plan', phase='model', reviewed_plan_id=source.id,
                           reviewed_plan=source.answer).snapshot()
    task = TaskRecord(id='task_old', type='agent', title='Old run', status='failed', conversation_id='c_plan',
                      payload=dict(question='Task', reviewed_plan_id=source.id), checkpoint=snapshot)
    db.add(task)
    db.commit()
    monkeypatch.setattr(task_runner, '_launch', lambda *a: pytest.fail('obsolete execution launched'))
    assert client.post('/api/tasks/task_old/resume').status_code == 409
    assert client.post('/api/tasks/task_old/restart').status_code == 409
    with pytest.raises(PlanConflict):
        list(loop.run_agent(db, 'Task', conversation_id='c_plan', run_id=task.id, checkpoint=snapshot, reviewed_plan_id=source.id))
    task.payload = dict(question='Task', execution_mode='plan')
    db.commit()
    assert client.post('/api/tasks/task_old/resume').status_code == 409


def test_history_fork_and_draft_keep_planning_without_copying_execution_ownership(db):
    image = dict(media_type='image/png', data='YWJj')
    seed_plan(db, plan_run_id='chat_plan_first', paper_id='paper_scope', plan_images=[image])
    message = client.get('/api/conversations/c_plan').json()['messages'][0]
    assert message['execution_mode'] == 'plan' and message['plan_run_id'] == 'chat_plan_first'
    fork = client.post('/api/conversations/c_plan/fork', json={}).json()['id']
    copied = client.get(f'/api/conversations/{fork}').json()['messages'][0]
    assert copied['execution_mode'] == 'plan' and not copied['plan_run_id']
    assert copied['plan_steps'] == message['plan_steps']
    assert copied['paper_id'] == 'paper_scope' and copied['plan_images'] == [image]
    value = dict(text='draft', execution_mode='plan')
    saved = client.put('/api/desktop-state/conversations/c_plan', json=dict(revision=0, value=value))
    assert saved.status_code == 200
    assert client.get('/api/desktop-state/conversations/c_plan').json()['value']['execution_mode'] == 'plan'
    assert client.get('/api/desktop-state/conversations/c_new').json()['value']['execution_mode'] == 'execute'


def test_legacy_migration_and_checkpoint_default_to_execute(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "old.db"}')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE qa_records (id TEXT PRIMARY KEY)'))
        conn.execute(text("INSERT INTO qa_records VALUES ('old')"))
        conn.execute(text('CREATE TABLE foreground_runs (id TEXT PRIMARY KEY, updated_at DATETIME)'))
        conn.execute(text("INSERT INTO foreground_runs (id) VALUES ('old')"))
    monkeypatch.setattr('app.db.engine', engine)
    _lightweight_migrations()
    _lightweight_migrations()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT execution_mode, plan_run_id, plan_steps, reviewed_plan_id FROM qa_records')).one() == ('execute', None, None, None)
        assert conn.execute(text('SELECT execution_mode, reviewed_plan_id FROM foreground_runs')).one() == ('execute', None)
    assert AgentState.model_validate(dict(question='Legacy')).execution_mode == 'execute'


def test_offline_plan_is_explicitly_unexecuted():
    response = MockLLM().chat([dict(role='system', content='[SCHOLARFLOW_PLAN_MODE]'), dict(role='user', content='Plan report')])
    assert '尚未执行' in response.content and not response.tool_calls
