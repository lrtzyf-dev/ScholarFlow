"""Real managed worktree creation, isolated index, safe removal and restoration."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import worktrees as wt, workspace, git_process, run_store, terminal_sessions
from app.agent import loop as agent_loop
from app.db import SessionLocal
from app.main import app
from app.models import ManagedWorktree, Conversation, Project, TaskRecord
from app.llm import LLMResponse
from tests.test_git_operations import repo, git


@pytest.fixture
def source(repo, tmp_path, monkeypatch):
    root, scope = repo
    data = tmp_path / 'appdata'; data.mkdir()
    monkeypatch.setattr(wt, 'DATA_DIR', data)
    wt._unavailable.clear()
    yield root, scope
    wt._unavailable.clear()


def create(scope, **kw):
    return wt.create(scope['project_id'], wt.Create(**{'request_id': 'worktree_001', 'name': '独立工作区 😀', **kw}))


def test_agent_worktree_tools_list_and_create_with_idempotent_request(source, db):
    from app.agent.tools import execute_tool

    listed = json.loads(execute_tool(db, 'list_project_worktrees', {}, project_scope=source[1]))
    assert listed['source_project_id'] == source[1]['project_id']
    assert listed['worktrees'] == []

    request = {'request_id': 'agent_wt_001', 'name': '模型创建的工作树', 'ref': 'HEAD', 'branch': 'agent/preview'}
    created = json.loads(execute_tool(db, 'create_project_worktree', request, project_scope=source[1]))
    assert created['status'] == 'ready' and created['branch'] == 'agent/preview', created
    repeated = json.loads(execute_tool(db, 'create_project_worktree', request, project_scope=source[1]))
    assert repeated['id'] == created['id']
    listed = json.loads(execute_tool(db, 'list_project_worktrees', {}, project_scope=source[1]))
    assert [row['id'] for row in listed['worktrees']] == [created['id']]


@pytest.mark.parametrize(('decision', 'expected_status'), [('approve', 'ready'), ('deny', None)])
def test_agent_worktree_creation_uses_approval_and_never_creates_after_denial(
    source, db, monkeypatch, decision, expected_status
):
    """模型创建工作树必须先暂停等审批；拒绝后不能产生目录或项目记录。"""
    from app import approval_store

    class WorktreeLLM:
        def chat(self, messages, tools=None, **kwargs):
            # 任务计划器的结构化响应；返回空步骤，主流程会使用确定性计划。
            return LLMResponse(content='{"steps": []}')

        def stream_chat(self, messages, tools=None):
            if not any(m.get('role') == 'tool' for m in messages):
                yield ('final', LLMResponse(content='', tool_calls=[
                    {'id': 'wt-call', 'name': 'create_project_worktree', 'arguments': {
                        'request_id': 'agent_wt_apv', 'name': '审批工作树', 'ref': 'HEAD',
                    }}
                ]))
                return
            yield ('final', LLMResponse(content='工作树审批流程已完成。'))

    monkeypatch.setattr(agent_loop, 'get_llm', lambda: WorktreeLLM())
    monkeypatch.setattr(agent_loop.settings, 'approval_mode', 'auto')
    db.add(Conversation(id='agent-worktree-approval', project_id=source[1]['project_id']))
    db.commit()
    saved_workspace = workspace.capture(db, 'agent-worktree-approval', source[1]['project_id'], [])

    events = []
    generator = agent_loop.run_agent(
        db, '创建一个隔离工作树', conversation_id='agent-worktree-approval',
        workspace=saved_workspace,
    )
    for kind, event in generator:
        events.append((kind, event))
        if kind == 'approval_request':
            assert event['tool_name'] == 'create_project_worktree'
            assert event['risk'] == 'guarded'
            assert approval_store.resolve(event['approval_id'], decision, 'once')

    result_events = [event for kind, event in events if kind == 'tool_result'
                     and event.get('name') == 'create_project_worktree']
    assert result_events, [kind for kind, _ in events]
    assert any(kind == 'approval_request' for kind, _ in events)
    assert any(kind == 'approval_result' and event['decision'] == decision
               for kind, event in events)

    listing = wt.listing(source[1]['project_id'])
    if expected_status:
        assert len(listing['worktrees']) == 1
        assert listing['worktrees'][0]['status'] == expected_status
    else:
        assert listing['worktrees'] == []
        assert 'denied_by_user' in result_events[0]['preview']


def test_agent_worktree_review_remove_restore_and_archive_tools(source, db):
    """模型工具必须复用持久审阅 revision，且归档恢复保留工作文件。"""
    from app.agent.tools import execute_tool

    row = create(source[1])
    review = json.loads(execute_tool(db, 'review_project_worktree_removal',
                                     {'worktree_id': row['id']}, project_scope=source[1]))
    assert review['removal_snapshot']['head'] == row['base_head']
    removed = json.loads(execute_tool(db, 'remove_project_worktree', {
        'worktree_id': row['id'], 'revision': review['revision']}, project_scope=source[1]))
    assert removed['status'] == 'removed' and not Path(row['path']).exists()
    assert json.loads(execute_tool(db, 'remove_project_worktree', {
        'worktree_id': row['id'], 'revision': review['revision']}, project_scope=source[1]))['status'] == 'removed'
    restored = json.loads(execute_tool(db, 'restore_project_worktree', {
        'worktree_id': row['id'], 'revision': removed['revision']}, project_scope=source[1]))
    assert restored['status'] == 'ready' and Path(row['path']).exists()

    archive_row = wt.create(source[1]['project_id'], wt.Create(
        request_id='agent_archive_001', name='模型归档工作树', ref='HEAD'))
    archive_path = Path(archive_row['path'])
    (archive_path / 'draft.txt').write_text('keep this draft')
    archive_review = json.loads(execute_tool(db, 'review_project_worktree_archive', {
        'worktree_id': archive_row['id']}, project_scope=source[1]))
    assert archive_review['removal_snapshot']['kind'] == 'archive'
    archived = json.loads(execute_tool(db, 'archive_project_worktree', {
        'worktree_id': archive_row['id'], 'revision': archive_review['revision']}, project_scope=source[1]))
    assert archived['status'] == 'removed' and archived['archive_info']['phase'] == 'removed'
    restored_archive = json.loads(execute_tool(db, 'restore_project_worktree', {
        'worktree_id': archive_row['id'], 'revision': archived['revision']}, project_scope=source[1]))
    assert restored_archive['status'] == 'ready'
    assert (Path(archive_row['path']) / 'draft.txt').read_text() == 'keep this draft'


@pytest.mark.parametrize(('decision', 'expected_status'), [('approve', 'removed'), ('deny', 'ready')])
def test_agent_worktree_removal_requires_approval_after_model_review(
    source, db, monkeypatch, decision, expected_status
):
    """Agent 先生成移除审阅，再对真正的目录删除请求审批；拒绝不改变工作树。"""
    from app import approval_store

    row = create(source[1])
    class RemovalLLM:
        def chat(self, messages, tools=None, **kwargs):
            return LLMResponse(content='{"steps": []}')

        def stream_chat(self, messages, tools=None):
            tool_messages = [m for m in messages if m.get('role') == 'tool']
            if not tool_messages:
                yield ('final', LLMResponse(content='', tool_calls=[
                    {'id': 'review-call', 'name': 'review_project_worktree_removal',
                     'arguments': {'worktree_id': row['id']}}
                ]))
                return
            if len(tool_messages) == 1:
                reviewed = json.loads(tool_messages[0]['content'])
                yield ('final', LLMResponse(content='', tool_calls=[
                    {'id': 'remove-call', 'name': 'remove_project_worktree',
                     'arguments': {'worktree_id': row['id'], 'revision': reviewed['revision']}}
                ]))
                return
            yield ('final', LLMResponse(content='工作树处理结果已记录。'))

    monkeypatch.setattr(agent_loop, 'get_llm', lambda: RemovalLLM())
    monkeypatch.setattr(agent_loop.settings, 'approval_mode', 'auto')
    db.add(Conversation(id='agent-worktree-remove', project_id=source[1]['project_id']))
    db.commit()
    saved_workspace = workspace.capture(db, 'agent-worktree-remove', source[1]['project_id'], [])

    events = []
    for kind, event in agent_loop.run_agent(
        db, '移除这个托管工作树', conversation_id='agent-worktree-remove', workspace=saved_workspace
    ):
        events.append((kind, event))
        if kind == 'approval_request':
            assert event['tool_name'] == 'remove_project_worktree'
            assert approval_store.resolve(event['approval_id'], decision, 'once')

    assert any(kind == 'approval_request' for kind, _ in events)
    assert any(kind == 'approval_result' and event['decision'] == decision
               for kind, event in events)
    assert wt.detail(source[1]['project_id'], row['id'])['status'] == expected_status
    assert Path(row['path']).exists() is (decision == 'deny')


def test_real_creation_isolated_files_index_and_idempotency(source):
    root, scope = source
    (root / 'a.txt').write_text('parent dirty\n')
    row = create(scope)
    assert row['status'] == 'ready', row
    path = Path(row['path'])
    assert path.exists() and (path / 'a.txt').read_text() == 'original\n'
    assert git(path, 'rev-parse', 'HEAD') == git(root, 'rev-parse', 'HEAD')
    assert create(scope)['id'] == row['id']
    with pytest.raises(HTTPException):
        create(scope, name='different')
    (path / 'a.txt').write_text('only here\n'); git(path, 'add', 'a.txt')
    assert git(root, 'diff', '--cached') == '' and '+only here' in git(path, 'diff', '--cached')
    assert (root / 'a.txt').read_text() == 'parent dirty\n'
    with SessionLocal() as db:
        project = db.get(Project, row['project_id'])
        assert workspace.read_text(workspace.project_scope(project), 'a.txt')['text'].replace('\r\n', '\n') == 'only here\n'
    assert len(wt.listing(scope['project_id'])['worktrees']) == 1


def test_new_branch_and_from_managed_worktree_use_selected_head(source):
    root, scope = source
    row = create(scope, branch='feature/中文')
    assert row['status'] == 'ready' and row['branch'] == 'feature/中文', row
    path = Path(row['path'])
    (path / 'a.txt').write_text('new head'); git(path, 'add', '.'); git(path, 'commit', '-m', 'New head')
    child = create({'project_id': row['project_id']}, request_id='worktree_002')
    assert child['status'] == 'ready' and child['source_project_id'] == scope['project_id'], child
    assert child['base_head'] == git(path, 'rev-parse', 'HEAD')
    assert child['base_head'] != git(root, 'rev-parse', 'HEAD')


def test_remove_preserves_detached_commit_and_restore_keeps_history(source):
    root, scope = source
    row = create(scope); path = Path(row['path'])
    (path / 'a.txt').write_text('saved commit'); git(path, 'add', '.'); git(path, 'commit', '-m', 'Detached work')
    head = git(path, 'rev-parse', 'HEAD')
    with SessionLocal() as db:
        project = db.get(Project, row['project_id']); old_scope = workspace.project_scope(project)
        db.add(Conversation(id='worktree_chat', project_id=project.id)); db.commit()
    review = wt.preview_remove(scope['project_id'], row['id'])
    assert review['removal_snapshot']['head'] == head
    removed = wt.remove(scope['project_id'], row['id'], review['revision'])
    assert removed['status'] == 'removed' and not path.exists(), removed
    assert git(root, 'rev-parse', 'refs/scholarflow/worktrees/' + row['id']) == head
    assert wt.remove(scope['project_id'], row['id'], review['revision'])['status'] == 'removed'
    with pytest.raises(HTTPException):
        workspace.root_path(old_scope)
    restored = wt.restore(scope['project_id'], row['id'], removed['revision'])
    assert restored['status'] == 'ready' and restored['project_id'] == row['project_id'], restored
    assert git(path, 'rev-parse', 'HEAD') == head and (path / 'a.txt').read_text() == 'saved commit'
    with SessionLocal() as db:
        assert db.get(Conversation, 'worktree_chat').project_id == row['project_id']
        assert workspace.root_path(workspace.project_scope(db.get(Project, row['project_id']))) == path


@pytest.mark.parametrize('kind', ['modified', 'untracked', 'ignored', 'staged', 'nested-repo'])
def test_removal_never_discards_unsaved_or_ignored_files(source, kind):
    root, scope = source
    (root / '.gitignore').write_text('ignored.txt\n'); git(root, 'add', '.'); git(root, 'commit', '-m', 'Ignore')
    row = create(scope); path = Path(row['path'])
    review = wt.preview_remove(scope['project_id'], row['id'])
    if kind == 'nested-repo':
        nested = path / 'nested'; nested.mkdir(); git(nested, 'init')
    else:
        target = path / ('ignored.txt' if kind == 'ignored' else 'a.txt' if kind in ('modified', 'staged') else 'untracked.txt')
        target.write_text('must keep')
        if kind == 'staged':
            git(path, 'add', 'a.txt')
    with pytest.raises(HTTPException):
        wt.remove(scope['project_id'], row['id'], review['revision'])
    assert path.exists() and wt.detail(scope['project_id'], row['id'])['status'] == 'ready'


@pytest.mark.parametrize('kind', ['head', 'branch', 'revision', 'locked'])
def test_old_removal_review_cannot_remove_changed_checkout(source, kind):
    root, scope = source
    row = create(scope); path = Path(row['path'])
    review = wt.preview_remove(scope['project_id'], row['id'])
    if kind == 'head':
        git(path, 'commit', '--allow-empty', '-m', 'New head')
    elif kind == 'branch':
        git(path, 'switch', '-c', 'changed')
    elif kind == 'locked':
        git(root, 'worktree', 'lock', str(path))
    with pytest.raises(HTTPException):
        wt.remove(scope['project_id'], row['id'], review['revision'] + (kind == 'revision'))
    assert path.exists()


@pytest.mark.parametrize('kind', ['foreground', 'background', 'terminal', 'nested-project'])
def test_active_operations_block_removal(source, monkeypatch, kind):
    root, scope = source
    row = create(scope); path = Path(row['path'])
    review = wt.preview_remove(scope['project_id'], row['id'])
    pid = row['project_id']
    if kind == 'nested-project':
        (path / 'sub').mkdir()
        with SessionLocal() as db:
            pid = workspace.register(db, str(path / 'sub')).id; db.commit()
    with SessionLocal() as db:
        db.add(Conversation(id='worktree_active', project_id=pid)); db.commit()
        if kind == 'background':
            db.add(TaskRecord(id='wt_task', type='agent', status='running', conversation_id='worktree_active')); db.commit()
    if kind in ('foreground', 'nested-project'):
        assert run_store.begin('worktree_run_' + kind, 'worktree_active')
    elif kind == 'terminal':
        from types import SimpleNamespace
        monkeypatch.setitem(terminal_sessions._sessions, 'fake_terminal', SimpleNamespace(project_id=pid, worker=SimpleNamespace(is_alive=lambda: True)))
    try:
        with pytest.raises(HTTPException):
            wt.remove(scope['project_id'], row['id'], review['revision'])
        assert path.exists()
    finally:
        if kind in ('foreground', 'nested-project'):
            run_store.finish('worktree_run_' + kind, 'done')


def test_concurrent_create_request_runs_once(source):
    _, scope = source
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(scope), range(2)))
    assert results[0]['status'] == 'ready' and results[0]['id'] == results[1]['id']


def test_restart_adopts_completed_creation_without_replay(source, monkeypatch):
    _, scope = source
    real = wt._ready
    class Crash(BaseException):
        pass
    monkeypatch.setattr(wt, '_ready', lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        create(scope)
    wt.recover_interrupted()
    row = wt.listing(scope['project_id'])['worktrees'][0]
    assert row['status'] == 'interrupted'
    assert create(scope)['status'] == 'interrupted'
    monkeypatch.setattr(wt, '_ready', real)
    restored = wt.reconcile(scope['project_id'], row['id'], row['revision'])
    assert restored['status'] == 'ready'


def test_failed_git_is_recorded_and_not_automatically_replayed(source, monkeypatch):
    _, scope = source
    calls = []
    def fail(*args):
        calls.append(1); raise RuntimeError('simulated interrupted process')
    monkeypatch.setattr(wt, '_run', fail)
    row = create(scope)
    assert row['status'] == 'interrupted' and 'simulated' in row['error']
    assert create(scope)['id'] == row['id'] and len(calls) == 1


def test_replaced_checkout_and_outside_directory_never_removed(source):
    _, scope = source
    row = create(scope); path = Path(row['path'])
    review = wt.preview_remove(scope['project_id'], row['id'])
    moved = path.parent / 'original'; path.rename(moved); path.mkdir(); (path / 'keep').write_text('safe')
    with pytest.raises(HTTPException):
        wt.remove(scope['project_id'], row['id'], review['revision'])
    assert (path / 'keep').read_text() == 'safe' and moved.exists()


def test_api_drafts_scope_and_unmanaged_worktrees(source):
    root, scope = source
    client = TestClient(app); base = f"/api/projects/{scope['project_id']}/worktrees"
    bad = client.post(base, json={'request_id': 'request_api', 'name': 'Work', 'path': 'D:/outside'})
    assert bad.status_code == 422
    response = client.post(base, json={'request_id': 'request_api', 'name': 'Work', 'branch': 'feature/api'})
    row = response.json(); assert response.status_code == 200 and row['status'] == 'ready', row
    assert client.get(base).json()['external'][0]['worktree'].replace('\\', '/') == str(root).replace('\\', '/')
    assert client.get('/api/projects/other/worktrees/' + row['id']).status_code == 404
    assert client.post(base + '/arbitrary/remove', json={'revision': 1}).status_code == 404
    assert client.post(base, json={'request_id': 'request_bad', 'name': 'Work', 'branch': 'feature/api'}).status_code == 409
    assert client.post(base, json={'request_id': 'request_bad', 'name': 'Work', 'ref': 'nonexistent'}).status_code == 409
    draft = f"/api/desktop-state/projects/{scope['project_id']}/worktrees"
    value = {'name': 'Persistent', 'ref': 'HEAD', 'branch': '', 'pending': {'request_id': 'pending_request', 'name': 'Lost response', 'ref': 'HEAD'}}
    assert client.put(draft, json={'revision': 0, 'value': value}).status_code == 200
    assert client.put(draft, json={'revision': 0, 'value': value}).status_code == 409
    assert client.get(draft).json()['value']['pending']['request_id'] == 'pending_request'


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_hidden_tracked_changes_cannot_be_deleted(source, flag):
    _, scope = source
    row = create(scope); path = Path(row['path'])
    git(path, 'update-index', flag, 'a.txt')
    (path / 'a.txt').write_text('hidden changes')
    assert not git(path, 'status', '--porcelain')
    with pytest.raises(HTTPException):
        wt.preview_remove(scope['project_id'], row['id'])
    assert (path / 'a.txt').read_text() == 'hidden changes'


def test_restart_after_directory_removal_reconciles_without_repeating_git(source, monkeypatch):
    _, scope = source
    row = create(scope)
    review = wt.preview_remove(scope['project_id'], row['id'])
    real = wt._save
    class Crash(BaseException):
        pass
    def save(db, record, status=None):
        if status == 'removed':
            raise Crash()
        real(db, record, status)
    monkeypatch.setattr(wt, '_save', save)
    with pytest.raises(Crash):
        wt.remove(scope['project_id'], row['id'], review['revision'])
    assert not Path(row['path']).exists()
    monkeypatch.setattr(wt, '_save', real)
    wt.recover_interrupted()
    interrupted = wt.detail(scope['project_id'], row['id'])
    assert wt.reconcile(scope['project_id'], row['id'], interrupted['revision'])['status'] == 'removed'


def test_failed_removal_can_reattach_original_directory_with_late_user_edits(source, monkeypatch):
    _, scope = source
    row = create(scope); path = Path(row['path'])
    review = wt.preview_remove(scope['project_id'], row['id'])
    real = wt._run
    def race(root, *args):
        if args[:2] == ('worktree', 'remove'):
            (path / 'late.txt').write_text('keep user work')
            raise RuntimeError('Git refused late edits')
        return real(root, *args)
    monkeypatch.setattr(wt, '_run', race)
    interrupted = wt.remove(scope['project_id'], row['id'], review['revision'])
    assert interrupted['status'] == 'interrupted'
    restored = wt.reconcile(scope['project_id'], row['id'], interrupted['revision'])
    assert restored['status'] == 'ready' and 'Git refused late edits' in restored['output']
    assert (path / 'late.txt').read_text() == 'keep user work'
