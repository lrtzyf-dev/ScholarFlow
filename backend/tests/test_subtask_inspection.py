from copy import deepcopy
import json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import Conversation, ForegroundRun, DeletedConversation
from app.runtime_models import RuntimeRun
from app.resource_models import PersistentSubtask, PersistentSubtaskAttempt, SubtaskOperationResolution
from app.agent.checkpoint import AgentState
from app.agent import subagent
from app import subtask_store


def seed(db, monkeypatch, tool='mcp__test__search'):
    monkeypatch.setattr(subagent, '_safe_tool_specs', lambda: [{'name': tool}])
    rid, cid = 'chat_subtask_review', 'subtask-review'
    task = {'topic': '对照论文方法', 'goal': '记录支持与冲突证据'}
    binding = {'run_id': rid, 'attempt_id': 'parent-a', 'conversation_id': cid, 'model_id': 'model'}
    call = {'id': 'external-a', 'name': tool, 'arguments': {'query': 'testing', 'api_key': 'fixture-secret'}}
    state = AgentState(question='research', conversation_id=cid, phase='tools', runtime_config=binding,
        calls=[{'id': 'spawn', 'name': 'spawn_subagent', 'arguments': {'tasks': [task]}}],
        pending_operation={'kind': 'tool', 'key': 'tool/1/0', 'name': 'spawn_subagent', 'arguments': {'tasks': [task]}})
    db.add_all([Conversation(id=cid, title='Review'), RuntimeRun(id=rid, kind='foreground', conversation_id=cid,
        status='interrupted', current_attempt_id='parent-a', revision=2), ForegroundRun(id=rid, conversation_id=cid,
        question='research', status='interrupted', runtime_config=binding, agent_checkpoint=state.snapshot())])
    db.commit()
    scope = subtask_store.child_scope(db, binding)
    node, _ = subtask_store.claim(parent_run_id=rid, parent_attempt_id='parent-a', specification=task, scope=scope, conversation_id=cid)
    cp = {**node['checkpoint'], 'messages': [{'role': 'system', 'content': 'private-system-secret'},
        {'role': 'assistant', 'reasoning_content': 'private-reasoning', '_provider': {'secret': 'provider-context'}}],
        'collected': [], 'calls': [call], 'tool_index': 0, 'pending_operation': deepcopy(call)}
    subtask_store.checkpoint(node['id'], node['writer_token'], cp, state='blocked')
    return rid, cid, node


def form(client, rid, node_id, action='skip'):
    tree = client.get('/api/subtasks/runs/' + rid).json()
    node = client.get(f'/api/subtasks/nodes/{node_id}/attempts').json()['node']
    return {'request_id': 'resolution-unique-1', 'expected_parent_revision': tree['parent']['revision'],
        'expected_attempt': node['attempt'], 'operation_fingerprint': node['progress']['pending']['fingerprint'],
        'action': action, 'reference': 'Remote request log manually inspected; synthetic fixture'}


def test_history_omits_private_checkpoints_and_preserves_unknown_operation(db, monkeypatch):
    rid, cid, node = seed(db, monkeypatch)
    client = TestClient(app)
    tree = client.get('/api/subtasks/runs/' + rid).json()
    detail = client.get(f'/api/subtasks/nodes/{node["id"]}/attempts').json()
    assert tree['recovery']['safe_children'] is False
    assert detail['node']['progress']['pending']['name'] == 'mcp__test__search'
    raw = json.dumps([tree, detail])
    for forbidden in ('private-system-secret', 'private-reasoning', 'provider-context', 'fixture-secret', 'writer_token'):
        assert forbidden not in raw
    assert detail['items'][0]['progress']['pending']
    center = client.get('/api/run-center?view=all').json()['items']
    assert all(item['child_history_available'] for item in center if item['target_run_id'] == rid)
    from app.api.foreground_runs import _resume_available
    assert not _resume_available(db.get(ForegroundRun, rid))


@pytest.mark.parametrize('action', ['retry', 'skip'])
def test_reconciliation_is_explicit_idempotent_fenced_and_parent_owned(db, monkeypatch, action):
    rid, cid, node = seed(db, monkeypatch)
    client = TestClient(app)
    body = form(client, rid, node['id'], action)
    url = f'/api/subtasks/nodes/{node["id"]}/reconcile'
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['provider_verified'] is False
    assert client.post(url, json=body).json()['id'] == response.json()['id']
    assert client.post(url, json={**body, 'action': 'retry' if action == 'skip' else 'skip'}).status_code == 409
    db.expire_all()
    saved = db.get(PersistentSubtask, node['id'])
    assert saved.checkpoint['pending_operation'] is None
    assert saved.state == 'blocked' and saved.writer_token is None
    assert saved.checkpoint['tool_index'] == (1 if action == 'skip' else 0)
    if action == 'skip':
        assert json.loads(saved.checkpoint['collected'][0]['result'])['user_skipped'] is True
    assert db.query(PersistentSubtaskAttempt).first().checkpoint['pending_operation']
    assert db.query(SubtaskOperationResolution).count() == 1
    with pytest.raises(subtask_store.SubtaskConflict):
        subtask_store.checkpoint(node['id'], node['writer_token'], {})
    assert client.get('/api/subtasks/runs/' + rid).json()['recovery']['safe_children'] is True
    from app.api.foreground_runs import _resume_available
    assert _resume_available(db.get(ForegroundRun, rid))
    # No independent execution happened, and the parent pending receipt remains.
    assert db.get(ForegroundRun, rid).agent_checkpoint['pending_operation']['name'] == 'spawn_subagent'
    assert saved.attempt == 1


@pytest.mark.parametrize('change', ['revision', 'attempt', 'hash', 'running', 'in_process', 'not_parent_dispatch', 'archived', 'deleted'])
def test_reconciliation_rejects_changed_scope_or_active_parent(db, monkeypatch, change):
    rid, cid, node = seed(db, monkeypatch)
    client = TestClient(app); body = form(client, rid, node['id'])
    if change == 'revision': body['expected_parent_revision'] -= 1
    if change == 'attempt': body['expected_attempt'] += 1
    if change == 'hash': body['operation_fingerprint'] = '0' * 64
    if change == 'running': db.get(RuntimeRun, rid).status = 'running'
    if change == 'in_process': monkeypatch.setattr('app.run_store.active_runs', lambda: {rid: cid})
    if change == 'not_parent_dispatch':
        row = db.get(ForegroundRun, rid); row.agent_checkpoint = {**row.agent_checkpoint, 'pending_operation': None}
    if change == 'archived':
        from app.timeutil import utcnow
        db.get(Conversation, cid).archived_at = utcnow()
    if change == 'deleted': db.add(DeletedConversation(id=cid))
    db.commit()
    response = client.post(f'/api/subtasks/nodes/{node["id"]}/reconcile', json=body)
    assert response.status_code == (410 if change == 'deleted' else 409), response.text
    assert db.query(SubtaskOperationResolution).count() == 0


def test_only_local_proven_child_shows_recovery_and_deleted_data_unavailable(db, monkeypatch):
    rid, cid, node = seed(db, monkeypatch, tool='read_project_file')
    from app.api.foreground_runs import _resume_available
    assert _resume_available(db.get(ForegroundRun, rid))
    parent = db.get(ForegroundRun, rid)
    cp = deepcopy(parent.agent_checkpoint); cp['pending_operation']['arguments']['tasks'] = []
    parent.agent_checkpoint = cp; db.commit()
    assert not _resume_available(parent)
    client = TestClient(app)
    db.add(DeletedConversation(id=cid)); db.commit()
    assert client.get('/api/subtasks/runs/' + rid).status_code == 410
    assert client.get(f'/api/subtasks/nodes/{node["id"]}/attempts').status_code == 410


def test_retry_respects_current_tool_policy_after_user_reconciliation(db, monkeypatch):
    rid, cid, node = seed(db, monkeypatch)
    client = TestClient(app)
    assert client.post(f'/api/subtasks/nodes/{node["id"]}/reconcile', json=form(client, rid, node['id'], 'retry')).status_code == 200
    monkeypatch.setattr(subagent, '_safe_tool_specs', lambda: [])
    assert client.get('/api/subtasks/runs/' + rid).json()['recovery']['safe_children'] is False


def test_all_metered_exports_inherit_parent_and_link_exact_child_attempt(db, monkeypatch):
    from app import call_ledger
    from app.runtime_context import bind_runtime_config
    rid, cid, original = seed(db, monkeypatch)
    saved = subtask_store.list_children(rid)[0]
    current, _ = subtask_store.claim(parent_run_id=rid, parent_attempt_id='parent-a', specification=saved['specification'],
        scope=saved['scope'], conversation_id=cid)
    binding = {'run_id': rid, 'attempt_id': 'parent-a', 'subtask_id': current['id'],
        'subtask_attempt': current['attempt'], 'subtask_writer_token': current['writer_token']}
    with bind_runtime_config(binding):
        with call_ledger.tracked_call(provider='local.test', service='files_post', endpoint='https://local.test/files') as call:
            call.usage({'requests': 1})
        data = call.snapshot()
        assert data['run_id'] == rid and data['attempt_id'] == 'parent-a'
        assert data['subtask'] == {'id': current['id'], 'attempt': 2}
        assert 'subtask_writer_token' not in json.dumps(data)
    client = TestClient(app)
    assert len(client.get(f'/api/subtasks/nodes/{original["id"]}/calls?attempt=2').json()['items']) == 1
    assert client.get(f'/api/subtasks/nodes/{original["id"]}/calls?attempt=1').json()['items'] == []
    assert len(client.get(f'/api/pricing/conversations/{cid}/calls').json()['items']) == 1
    with bind_runtime_config({**binding, 'subtask_writer_token': 'stale'}):
        with pytest.raises(call_ledger.CallLedgerConflict):
            call_ledger.begin_call(provider='local.test', service='chat', endpoint='https://local.test/chat')
    with bind_runtime_config({'run_id': rid, 'attempt_id': 'parent-a'}):
        with pytest.raises(call_ledger.CallLedgerConflict):
            call_ledger.begin_call(provider='local.test', service='files_post', endpoint='https://local.test/files', retry_of_call_id=call.id)
    db.get(RuntimeRun, rid).current_attempt_id = 'new-parent-attempt'; db.commit()
    with bind_runtime_config(binding):
        with pytest.raises(call_ledger.CallLedgerConflict):
            call_ledger.begin_call(provider='local.test', service='chat', endpoint='https://local.test/chat')
