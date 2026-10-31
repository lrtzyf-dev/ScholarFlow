"""Real repository attachment changes, ABA guards and multi-window admission."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import workspace, workspace_switches as switches, worktrees, run_store, task_runner, project_changes, project_commands, git_operations
from app.agent.checkpoint import AgentState
from app.api.desktop_state import ConversationState
from app.db import SessionLocal, engine, _lightweight_migrations
from app.main import app
from app.models import Conversation, DesktopState, QARecord, TaskRecord, ForegroundRun, DeletedConversation, WorkspaceSwitch
from tests.test_worktrees import source, repo, git, create


@pytest.fixture
def setup(source):
    root, scope = source
    row = create(scope)
    assert row['status'] == 'ready'
    with SessionLocal() as db:
        db.add(Conversation(id='switch_chat', project_id=scope['project_id'], title='保留此会话'))
        db.commit()
    return root, scope, row


def prepare(target, *, cid='switch_chat', **overrides):
    with SessionLocal() as db:
        conv = db.get(Conversation, cid)
        draft = db.get(DesktopState, 'conversation:' + cid)
        values = dict(request_id=uuid4().hex, target_project_id=target,
            workspace_revision=conv.workspace_revision, draft_revision=draft.revision if draft else 0)
    return switches.prepare(cid, switches.Prepare(**(values | overrides)))


def move(target):
    row = prepare(target)
    return switches.apply('switch_chat', row['id'])


def test_switch_preserves_checkout_bytes_index_history_and_editor(setup):
    root, scope, tree = setup
    path = Path(tree['path'])
    for directory, value in ((root, b'source\r\n\xff'), (path, b'target\x00\x01')):
        (directory / 'a.txt').write_bytes(value); git(directory, 'add', '.')
        (directory / 'untracked.bin').write_bytes(b'\x80\0' + value)
    before = [(git(p, 'rev-parse', 'HEAD'), git(p, 'ls-files', '--stage'), (p/'a.txt').read_bytes()) for p in (root, path)]
    value = ConversationState(text='文字与图片草稿', file_paths=['a.txt'], editor={
        'project_id': scope['project_id'], 'path': 'a.txt', 'content': '尚未保存到文件', 'sha256': None}).model_dump()
    with SessionLocal() as db:
        saved_scope = workspace.capture(db, 'switch_chat', scope['project_id'])
        db.add(QARecord(id='old_answer', conversation_id='switch_chat', question='原问题', answer='原回答', workspace=saved_scope))
        db.add(DesktopState(key='conversation:switch_chat', value=value))
        db.commit()
    review = prepare(tree['project_id'])
    assert review['snapshot']['source']['changes'] == 2
    applied = switches.apply('switch_chat', review['id'])
    assert applied['status'] == 'applied'
    # Lost response and process-local state loss do not apply a second time.
    assert switches.apply('switch_chat', review['id']) == applied
    with SessionLocal() as db:
        conv = db.get(Conversation, 'switch_chat')
        assert (conv.project_id, conv.workspace_revision, conv.title) == (tree['project_id'], 1, '保留此会话')
        saved = db.get(DesktopState, 'conversation:switch_chat')
        assert saved.revision == 2 and saved.value == value | {'workspace_revision': 1, 'file_paths': []}
        assert db.get(QARecord, 'old_answer').workspace == saved_scope
    assert before == [(git(p, 'rev-parse', 'HEAD'), git(p, 'ls-files', '--stage'), (p/'a.txt').read_bytes()) for p in (root, path)]
    assert (root/'untracked.bin').read_bytes() != (path/'untracked.bin').read_bytes()


def test_aba_blocks_old_plan_checkpoint_tasks_and_new_request(setup, monkeypatch):
    root, scope, tree = setup
    with SessionLocal() as db:
        original = workspace.capture(db, 'switch_chat', scope['project_id'], ['a.txt'])
        db.add(QARecord(id='old_plan', conversation_id='switch_chat', question='计划', answer='原计划', execution_mode='plan', workspace=original))
        state = AgentState(question='旧任务', paper_id=None, skills=[], conversation_id='switch_chat', workspace=original)
        db.add(TaskRecord(id='old_task', type='agent', title='旧任务', status='interrupted', conversation_id='switch_chat',
            payload={'question': '旧任务', 'workspace': original}, checkpoint=state.snapshot()))
        db.commit()
    move(tree['project_id']); move(scope['project_id'])
    with SessionLocal() as db:
        with pytest.raises(HTTPException, match=''): workspace.validate_scope(db, 'switch_chat', original)
        with pytest.raises(HTTPException): workspace.capture(db, 'switch_chat', scope['project_id'])
        with pytest.raises(HTTPException): workspace.capture(db, 'switch_chat', scope['project_id'], reviewed_plan_id='old_plan', workspace_revision=2)
        assert workspace.capture(db, 'switch_chat', scope['project_id'], workspace_revision=2)['workspace_revision'] == 2
    for action in (task_runner.resume_task, task_runner.restart_task):
        with SessionLocal() as db, pytest.raises(HTTPException): action(db, 'old_task')
    client = TestClient(app)
    for url, data in [('/api/chat/stream', {'question':'stale'}), ('/api/tasks/start', {'question':'stale'}),
                      ('/api/foreground-runs', {'id':'chat_old_scope_001', 'question':'stale'})]:
        response = client.post(url, json={'conversation_id':'switch_chat', 'project_id':scope['project_id'], **data})
        assert response.status_code == 409, response.text
    # New foreground captures the current scope; old history does not get rewritten.
    response = client.post('/api/foreground-runs', json={'id':'chat_new_scope_001','question':'fresh',
        'conversation_id':'switch_chat','project_id':scope['project_id'],'workspace_revision':2})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('change', ['draft', 'head', 'branch', 'status', 'archived', 'active', 'pending-task', 'foreground'])
def test_confirmation_rechecks_review_and_execution(setup, change):
    root, scope, tree = setup
    review = prepare(tree['project_id'])
    try:
        with SessionLocal() as db:
            if change == 'draft': db.add(DesktopState(key='conversation:switch_chat', value=ConversationState(text='后写入').model_dump()))
            if change == 'archived': db.get(Conversation, 'switch_chat').archived_at = datetime.utcnow()
            if change == 'pending-task': db.add(TaskRecord(id='pending', type='agent', title='待执行', status='pending', conversation_id='switch_chat'))
            db.commit()
        if change == 'head': git(Path(tree['path']), 'commit', '--allow-empty', '-m', 'changed')
        if change == 'branch': git(Path(tree['path']), 'switch', '-c', 'changed-branch')
        if change == 'status': (root/'new.txt').write_text('late edit')
        if change == 'active': assert run_store.begin('chat_switch_active_001', 'switch_chat')
        if change == 'foreground':
            r = TestClient(app).post('/api/foreground-runs', json={'id':'chat_switch_pending_001', 'question':'queued',
                'conversation_id':'switch_chat', 'project_id':scope['project_id']})
            assert r.status_code == 200
        with pytest.raises(HTTPException): switches.apply('switch_chat', review['id'])
        with SessionLocal() as db: assert db.get(Conversation, 'switch_chat').project_id == scope['project_id']
    finally:
        if change == 'active': run_store.finish('chat_switch_active_001', 'cancelled')


def test_two_prepared_targets_are_serialized_and_do_not_rebind_late(setup):
    root, scope, tree = setup
    other = create(scope, request_id='second_worktree')
    first, second = prepare(tree['project_id']), prepare(other['project_id'])
    def apply(row):
        try: return switches.apply('switch_chat', row['id'])['status']
        except HTTPException: return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(apply, (first, second)))
    assert sorted(results) == ['applied', 'conflict']
    with SessionLocal() as db: assert db.get(Conversation, 'switch_chat').workspace_revision == 1


def test_old_draft_cas_cannot_reintroduce_file_paths_after_reload(setup):
    root, scope, tree = setup
    client = TestClient(app)
    old = ConversationState(text='本窗口独有文字', file_paths=['a.txt']).model_dump()
    move(tree['project_id'])
    remote = client.get('/api/desktop-state/conversations/switch_chat').json()
    response = client.put('/api/desktop-state/conversations/switch_chat', json={'revision':remote['revision'], 'value':old})
    assert response.status_code == 409
    assert response.json()['detail']['value']['file_paths'] == []
    assert client.get('/api/desktop-state/conversations/switch_chat').json() == remote


def test_new_file_edit_and_command_after_return_use_current_generation(setup):
    root, scope, tree = setup
    (root/'a.txt').write_text('before change')
    client = TestClient(app)
    before = workspace.read_text(scope, 'a.txt')['sha256']
    edit = {'path':'a.txt', 'operation':'update','expected_sha256':before,'content':'new content'}
    old = client.post(f"/api/projects/{scope['project_id']}/changes", json={'request_id':'old_edit_key', 'conversation_id':'switch_chat', 'edit':edit}).json()
    old_command = project_commands.prepare(scope, 'switch_chat', {'command':"Set-Content forbidden.txt 'should not run'", 'summary':'旧命令'}, run_id='old_command_run', operation_key='old_command_key')
    owner = {'conversation_id':'switch_chat', 'run_id':'old_git_run'}
    old_git = git_operations.prepare_agent(scope, 'switch_chat', {'action':'stage','paths':['a.txt']}, owner=owner, operation_key='old_git_key')
    move(tree['project_id']); move(scope['project_id'])
    with pytest.raises(HTTPException): project_changes.apply(old['id'], revision=old['revision'])
    assert project_commands.run(old_command['id'], run_id='old_command_run')['status'] == 'conflict'
    assert not (root/'forbidden.txt').exists()
    assert git_operations.decide(scope['project_id'], old_git['id'], old_git['revision'], agent_owner=owner)['status'] == 'conflict'
    assert git(root, 'diff', '--cached') == ''
    reused = client.post(f"/api/projects/{scope['project_id']}/changes", json={'request_id':'old_edit_key', 'conversation_id':'switch_chat', 'workspace_revision':2, 'edit':edit})
    assert reused.status_code == 409
    fresh = client.post(f"/api/projects/{scope['project_id']}/changes", json={'request_id':'new_edit_key', 'conversation_id':'switch_chat', 'workspace_revision':2,'edit':edit})
    assert fresh.status_code == 200, fresh.text
    row = fresh.json()
    assert project_changes.apply(row['id'], revision=row['revision'])['status'] == 'applied'
    with SessionLocal() as db: current = workspace.capture(db, 'switch_chat', scope['project_id'], workspace_revision=2)
    command = project_commands.prepare(current, 'switch_chat', {'command':'Write-Output scope-ok', 'summary':'核对切换后的目录'}, run_id='chat_new_era', operation_key='command_new_era')
    result = project_commands.run(command['id'], run_id='chat_new_era')
    assert result['status'] == 'succeeded', result
    assert 'scope-ok' in result['output']
    old_undo = project_changes.undo(row['id'], 'switch_chat')
    move(tree['project_id']); move(scope['project_id'])
    fresh_undo = project_changes.undo(row['id'], 'switch_chat')
    assert fresh_undo['id'] != old_undo['id']
    assert project_changes.apply(fresh_undo['id'], revision=fresh_undo['revision'])['status'] == 'applied'
    assert (root/'a.txt').read_text() == 'before change'
    assert project_changes.undo(row['id'], 'switch_chat')['id'] == fresh_undo['id']


def test_rejects_unrelated_removed_locked_or_deleted(setup, tmp_path):
    root, scope, tree = setup
    with SessionLocal() as db:
        other = tmp_path/'other'; other.mkdir()
        pid = workspace.register(db, str(other)).id; db.commit()
    with pytest.raises(HTTPException): prepare(pid)
    git(root, 'worktree', 'lock', tree['path'])
    with pytest.raises(HTTPException): prepare(tree['project_id'])
    git(root, 'worktree', 'unlock', tree['path'])
    review = prepare(tree['project_id'])
    removal = worktrees.preview_remove(scope['project_id'], tree['id'])
    worktrees.remove(scope['project_id'], tree['id'], removal['revision'])
    with pytest.raises(HTTPException): switches.apply('switch_chat', review['id'])
    with SessionLocal() as db:
        db.add(DeletedConversation(id='switch_chat')); db.commit()
    with pytest.raises(HTTPException) as error: switches.listing('switch_chat')
    assert error.value.status_code == 410


def test_fork_retains_generation_and_requests_are_durable(setup):
    root, scope, tree = setup
    review = prepare(tree['project_id'])
    request = switches.Prepare(request_id=review['request_id'], target_project_id=tree['project_id'], workspace_revision=0, draft_revision=0)
    assert switches.prepare('switch_chat', request)['id'] == review['id']
    switches.apply('switch_chat', review['id'])
    assert switches.prepare('switch_chat', request)['status'] == 'applied'
    with SessionLocal() as db:
        current = workspace.capture(db, 'switch_chat', tree['project_id'], workspace_revision=1)
        db.add(QARecord(conversation_id='switch_chat', question='新问题', answer='新回答', workspace=current)); db.commit()
    client = TestClient(app)
    fork = client.post('/api/conversations/switch_chat/fork', json={})
    assert fork.status_code == 200, fork.text
    cid = fork.json()['id']
    assert client.get(f'/api/conversations/{cid}/project').json()['workspace_revision'] == 1
    assert client.get(f'/api/desktop-state/conversations/{cid}').json()['value']['workspace_revision'] == 1
    stale = client.put(f'/api/desktop-state/conversations/{cid}', json={'revision':0, 'value':ConversationState().model_dump()})
    assert stale.status_code == 409 and stale.json()['detail']['value']['workspace_revision'] == 1
    assert len(switches.listing('switch_chat')['switches']) == 1


def test_cancel_and_request_ownership_never_switch_another_chat(setup):
    root, scope, tree = setup
    row = prepare(tree['project_id'])
    with SessionLocal() as db:
        db.add(Conversation(id='another_chat', project_id=scope['project_id'])); db.commit()
    with pytest.raises(HTTPException): switches.apply('another_chat', row['id'])
    with pytest.raises(HTTPException): prepare(tree['project_id'], cid='another_chat', request_id=row['request_id'])
    assert switches.cancel('switch_chat', row['id'])['status'] == 'cancelled'
    with pytest.raises(HTTPException): switches.apply('switch_chat', row['id'])
    with SessionLocal() as db: assert db.get(Conversation, 'switch_chat').workspace_revision == 0


def test_replaced_directory_refuses_review_without_touching_replacement(setup):
    root, scope, tree = setup
    row = prepare(tree['project_id'])
    path = Path(tree['path']); original = path.with_name('preserved-checkout')
    path.rename(original); path.mkdir(); (path/'a.txt').write_text('replacement data')
    with pytest.raises(HTTPException): switches.apply('switch_chat', row['id'])
    assert (path/'a.txt').read_text() == 'replacement data'
    assert (original/'a.txt').read_text() == 'original\n'


def test_schema_upgrade_defaults_existing_conversations_to_zero():
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE conversations DROP COLUMN workspace_revision'))
        conn.execute(text("INSERT INTO conversations(id,title,archive_revision,created_at,updated_at) VALUES ('legacy','旧会话',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    _lightweight_migrations(); _lightweight_migrations()
    with SessionLocal() as db: assert db.get(Conversation, 'legacy').workspace_revision == 0
