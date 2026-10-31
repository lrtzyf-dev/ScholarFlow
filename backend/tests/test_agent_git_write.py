"""Agent policy and durable review use real Git repositories and index/HEAD changes."""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import git_operations as ops, cancel_store
from app.agent import loop
from app.db import SessionLocal, engine, init_db
from app.llm import LLMResponse
from app.main import app
from app.models import Conversation, GitOperation, QARecord
from tests.test_git_operations import repo, git  # reusable real repository fixture


@pytest.fixture
def project(repo, db, monkeypatch):
    root, scope = repo
    db.add(Conversation(id='git_chat', project_id=scope['project_id'])); db.commit()
    monkeypatch.setattr(loop, 'make_plan', lambda *a: [])
    monkeypatch.setattr(loop.settings, 'memory_recall', False)
    monkeypatch.setattr(loop.settings, 'memory_capture', False)
    monkeypatch.setattr(loop.settings, 'approval_mode', 'read_only')
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **kw: {'ran': 0, 'blocked': False, 'feedback': ''})
    cancel_store.clear('git_run')
    yield root, {**scope, 'files': [], 'rules': []}
    cancel_store.clear('git_run')
    loop.approval_store.discard_for_run('git_run')


def model(monkeypatch, arguments, mode='execute'):
    results = []
    class Model:
        def stream_chat(self, messages, tools=None):
            assert ('git_project_write' in {spec['name'] for spec in tools}) == (mode != 'plan')
            replies = [m for m in messages if m['role'] == 'tool']
            if replies:
                results.append(json.loads(replies[-1]['content']))
                yield 'final', LLMResponse('已处理 Git 请求，具体状态以操作记录为准。')
            else:
                yield 'final', LLMResponse('', [{'id': 'git-write', 'name': 'git_project_write', 'arguments': arguments}])
    monkeypatch.setattr(loop, 'get_llm', Model)
    return results


def run(db, scope, **kwargs):
    return loop.run_agent(db, 'Handle the requested Git change', conversation_id='git_chat',
        run_id='git_run', workspace=scope, **kwargs)


@pytest.mark.parametrize('action', ['stage', 'unstage', 'commit'])
def test_approval_then_real_index_or_commit_and_persisted_trace(project, db, monkeypatch, action):
    root, scope = project
    (root / 'a.txt').write_text('修改内容\n', encoding='utf-8')
    if action != 'stage':
        git(root, 'add', 'a.txt')
    before = git(root, 'ls-files', '--stage')
    args = {'action': action, **({'message': '测试提交 中文 😀'} if action == 'commit' else {'paths': ['a.txt']})}
    results = model(monkeypatch, args)
    def approve(id):
        pending = loop.approval_store.get(id)
        a = pending.arguments
        assert a['project_id'] == scope['project_id'] and a['revision'] == 1
        assert a['paths'] == ['a.txt'] and '+修改内容' in a['patch_preview']
        assert git(root, 'ls-files', '--stage') == before
        assert git(root, 'rev-list', '--count', 'HEAD') == '1'
        with SessionLocal() as store:
            row = store.get(GitOperation, a['operation_id'])
            assert row.status == 'prepared' and row.agent_owner['run_id'] == 'git_run'
        # Neither direct manual HTTP nor a forged owner may bypass model approval.
        client = TestClient(app)
        for decision in ['apply', 'cancel']:
            assert client.post(f"/api/projects/{scope['project_id']}/git/operations/{a['operation_id']}/{decision}",
                json={'revision': 1}).status_code == 403
        with pytest.raises(HTTPException):
            ops.decide(scope['project_id'], a['operation_id'], 1, agent_owner={'run_id': 'different'})
        with pytest.raises(HTTPException):
            ops.decide(scope['project_id'], a['operation_id'], 2,
                agent_owner={'run_id': 'git_run', 'conversation_id': 'git_chat'})
        loop.approval_store.resolve(id, 'approve')
    monkeypatch.setattr(loop.approval_store, 'wait', approve)
    events = list(run(db, scope))
    assert results[0]['status'] == 'applied'
    assert any(kind == 'git_operation' and event['status'] == 'applied' for kind, event in events)
    db.expire_all()
    assert any(e['kind'] == 'git_operation' and e['status'] == 'applied' for e in db.query(QARecord).one().trace)
    assert (root / 'a.txt').read_text(encoding='utf-8') == '修改内容\n'
    if action == 'stage':
        assert '+修改内容' in git(root, 'diff', '--cached')
    elif action == 'unstage':
        assert not git(root, 'diff', '--cached') and '+修改内容' in git(root, 'diff')
    else:
        assert git(root, 'rev-list', '--count', 'HEAD') == '2'
        assert git(root, 'log', '-1', '--format=%B') == '测试提交 中文 😀'


@pytest.mark.parametrize('case', ['deny', 'plan', 'hook', 'full', 'external-index', 'cancel'])
def test_policy_conflict_and_cancellation(project, db, monkeypatch, case):
    root, scope = project
    (root / 'a.txt').write_text('candidate\n')
    results = model(monkeypatch, {'action': 'stage', 'paths': ['a.txt']}, 'plan' if case == 'plan' else 'execute')
    if case == 'full':
        monkeypatch.setattr(loop.settings, 'approval_mode', 'full_access')
    if case == 'hook':
        monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda event, *a, **kw:
            {'ran': 1, 'blocked': event == 'PreToolUse', 'feedback': 'blocked'})
    def decide(id):
        assert case not in ('full', 'plan', 'hook')
        if case == 'external-index':
            (root / 'a.txt').write_text('external\n'); git(root, 'add', 'a.txt')
        if case == 'cancel':
            cancel_store.request_cancel('git_run')
        loop.approval_store.resolve(id, 'deny' if case == 'deny' else 'approve')
    monkeypatch.setattr(loop.approval_store, 'wait', decide)
    events = list(run(db, scope, execution_mode='plan' if case == 'plan' else 'execute'))
    db.expire_all()
    rows = db.query(GitOperation).all()
    if case in ('plan', 'hook'):
        assert not rows and results[0]['error']
    else:
        expected = {'deny': 'cancelled', 'cancel': 'cancelled', 'full': 'applied', 'external-index': 'conflict'}[case]
        assert rows[0].status == expected
        assert any(kind == 'git_operation' and event['status'] == expected for kind, event in events)
    if case == 'external-index':
        assert '+external' in git(root, 'diff', '--cached') and '+candidate' not in git(root, 'diff', '--cached')
    elif case != 'full':
        assert not git(root, 'diff', '--cached')


@pytest.mark.parametrize('extra', [{'project_id': 'other'}, {'request_id': 'forged_id'}, {'paths': ['../outside']},
    {'paths': ['D:/outside']}, {'action': 'push'}, {'action': 'commit', 'message': ''}, {'paths': []}])
def test_invalid_model_arguments_never_prepare_or_approve(project, db, monkeypatch, extra):
    root, scope = project
    (root / 'a.txt').write_text('changed')
    results = model(monkeypatch, {'action': 'stage', **extra})
    monkeypatch.setattr(loop.approval_store, 'wait', lambda id: pytest.fail('invalid request reached approval'))
    list(run(db, scope))
    assert results[0]['error']
    assert db.query(GitOperation).count() == 0 and not git(root, 'diff', '--cached')


def test_lost_result_checkpoint_retry_returns_original_commit(project, db, monkeypatch):
    root, scope = project
    (root / 'a.txt').write_text('commit once'); git(root, 'add', 'a.txt')
    model(monkeypatch, {'action': 'commit', 'message': 'Exactly one commit'})
    monkeypatch.setattr(loop.approval_store, 'wait', lambda id: loop.approval_store.resolve(id, 'approve'))
    saved = {}
    class SimulatedCrash(BaseException):
        pass
    def persist(snapshot):
        if 'tool/0/0' in snapshot['operations']:
            raise SimulatedCrash()
        saved.clear(); saved.update(deepcopy(snapshot))
    with pytest.raises(SimulatedCrash):
        list(run(db, scope, save_checkpoint=persist))
    assert saved['pending_operation']['key'] == 'tool/0/0'
    assert git(root, 'rev-list', '--count', 'HEAD') == '2'
    saved['resolution'] = 'retry'
    monkeypatch.setattr(loop.approval_store, 'wait', lambda id: pytest.fail('completed write must not be approved twice'))
    list(run(db, scope, checkpoint=saved))
    assert git(root, 'rev-list', '--count', 'HEAD') == '2' and db.query(GitOperation).count() == 1


def test_restart_cancels_waiting_agent_review_and_never_replays_interrupted(project, db):
    root, scope = project
    (root / 'a.txt').write_text('not executed')
    owner = {'conversation_id': 'git_chat', 'run_id': 'git_run'}
    args = {'action': 'stage', 'paths': ['a.txt']}
    waiting = ops.prepare_agent(scope, 'git_chat', args, owner=owner, operation_key='tool/0/0')
    interrupted = ops.prepare_agent(scope, 'git_chat', args, owner=owner, operation_key='tool/0/1')
    with SessionLocal() as store:
        store.get(GitOperation, interrupted['id']).status = 'running'; store.commit()
    ops.recover_interrupted()
    assert ops.prepare_agent(scope, 'git_chat', args, owner=owner, operation_key='tool/0/0')['status'] == 'cancelled'
    assert ops.decide(scope['project_id'], interrupted['id'], 1, agent_owner=owner)['status'] == 'interrupted'
    assert not git(root, 'diff', '--cached')


def test_closing_generator_at_review_boundary_cancels_proposal(project, db, monkeypatch):
    root, scope = project
    (root / 'a.txt').write_text('not executed')
    model(monkeypatch, {'action': 'stage'})
    source = run(db, scope)
    for kind, event in source:
        if kind == 'git_operation':
            source.close(); break
    db.expire_all()
    assert db.query(GitOperation).one().status == 'cancelled'
    assert not git(root, 'diff', '--cached')


def test_existing_manual_review_survives_owner_column_migration(repo, db):
    root, scope = repo
    (root / 'a.txt').write_text('manual')
    row = ops.prepare(scope, ops.Request(request_id='old_review', action='stage'))
    db.close()
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE git_operations DROP COLUMN agent_owner'))
    init_db()
    assert ops.decide(scope['project_id'], row['id'], row['revision'])['status'] == 'applied'


def test_review_displays_unicode_filenames_without_octal_escaping(project):
    root, scope = project
    path = root / '源文件 😀.txt'; path.write_text('original\n', encoding='utf-8')
    git(root, 'add', path.name); git(root, 'commit', '-m', 'Unicode filename')
    path.write_text('changed\n', encoding='utf-8')
    from app.project_git import diff
    assert '源文件 😀.txt' in diff(scope, path.name, 'unstaged')['patch']
    row = ops.prepare_agent(scope, 'git_chat', {'action': 'stage', 'paths': [path.name]},
        owner={'conversation_id': 'git_chat', 'run_id': 'git_run'}, operation_key='unicode')
    assert '源文件 😀.txt' in row['patch'] and '\\346' not in row['patch']
