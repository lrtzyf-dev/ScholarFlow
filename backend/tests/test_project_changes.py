"""Exercise actual Windows writes and recovery against isolated files and SQLite."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import workspace, workspace_write, project_changes as changes
from app.main import app
from app.models import Conversation, ProjectChange, TaskRecord
from app.db import SessionLocal

client = TestClient(app)


@pytest.fixture
def project(db, tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    (root / 'a.txt').write_bytes(b'old\n')
    project = workspace.register(db, str(root))
    db.add(Conversation(id='c_edit', project_id=project.id)); db.commit()
    return workspace.project_scope(project)


def propose(project, path='a.txt', content='new\n', operation='update', **kw):
    original = changes.read_original(project, path)[1]
    return changes.prepare(project, 'c_edit', dict(path=path, content=content,
        operation=operation, expected_sha256=original, summary='Test change'), **kw)


@pytest.mark.parametrize('operation', ['update', 'create', 'delete'])
def test_actual_write_review_idempotence_and_undo(project, operation):
    path = 'nested/deep/new.txt' if operation == 'create' else 'a.txt'
    content = None if operation == 'delete' else 'new\n'
    item = propose(project, path, content, operation, operation_key='test-operation')
    file = Path(project['root_path']) / path
    before = file.read_bytes() if file.exists() else None
    assert client.get(f"/api/project-changes/{item['id']}").json()['diff']['total_lines'] > 0
    result = changes.apply(item['id'], revision=1)
    assert result['status'] == 'applied'
    assert (file.read_bytes() if file.exists() else None) == (content.encode() if content else None)
    assert changes.apply(item['id'], revision=1) == result  # lost response retry
    reverse = changes.undo(item['id'], 'c_edit')
    assert changes.undo(item['id'], 'c_edit')['id'] == reverse['id']
    changes.apply(reverse['id'])
    assert (file.read_bytes() if file.exists() else None) == before


def test_bom_crlf_and_original_undo_bytes(project):
    path = Path(project['root_path']) / 'a.txt'
    before = b'\xef\xbb\xbfline\r\nlast'
    path.write_bytes(before)
    item = propose(project, content='updated\nlast\n')
    changes.apply(item['id'])
    assert path.read_bytes() == b'\xef\xbb\xbfupdated\r\nlast\r\n'
    changes.apply(changes.undo(item['id'], 'c_edit')['id'])
    assert path.read_bytes() == before


def test_external_change_and_revision_conflicts(project):
    item = propose(project)
    path = Path(project['root_path']) / 'a.txt'; path.write_text('external')
    with pytest.raises(HTTPException): changes.apply(item['id'])
    assert path.read_text() == 'external'
    detail = client.get(f"/api/project-changes/{item['id']}").json()
    assert detail['status'] == 'conflict'
    with pytest.raises(HTTPException): changes.apply(item['id'], revision=1)
    assert client.get(f"/api/project-changes/{item['id']}/content?version=before").json()['content'] == 'old\n'
    path.write_bytes(b'old\n'); changes.apply(item['id'], revision=detail['revision'])


@pytest.mark.parametrize('path', ['../escape.txt', 'C:/escape.txt', 'a.txt:stream', 'src/../x', 'CON', 'a/NUL.txt', 'a./b', '.git/config'])
def test_escaping_write_targets(project, path):
    with pytest.raises(HTTPException):
        changes.prepare(project, 'c_edit', dict(path=path, operation='create', content='bad'))


@pytest.mark.parametrize('content', ['\x00binary', '中' * 100000, 'line\n' * 20001], ids=['binary', 'oversize', 'too-many-lines'])
def test_invalid_content(project, content):
    with pytest.raises(HTTPException): propose(project, content=content)


def test_noop_malformed_and_request_reuse(project):
    with pytest.raises(HTTPException): propose(project, content='old\n')
    with pytest.raises(HTTPException): changes.prepare(project, 'c_edit', {'path':'a.txt', 'content':'new'})
    first = propose(project, operation_key='idempotent')
    assert propose(project, operation_key='idempotent')['id'] == first['id']
    with pytest.raises(HTTPException): propose(project, content='different', operation_key='idempotent')


def test_create_race_and_rules_conflict(project):
    item = propose(project, 'new.txt', 'planned', 'create')
    file = Path(project['root_path']) / 'new.txt'; file.write_text('external')
    with pytest.raises(HTTPException): changes.apply(item['id'])
    assert file.read_text() == 'external'
    item = propose(project)
    (file.parent / 'AGENTS.md').write_text('new rules')
    with pytest.raises(HTTPException): changes.apply(item['id'])
    assert (file.parent / 'a.txt').read_bytes() == b'old\n'


def test_external_delete_is_conflict(project):
    item = propose(project)
    (Path(project['root_path'])/'a.txt').unlink()
    with pytest.raises(HTTPException): changes.apply(item['id'])
    assert client.get(f"/api/project-changes/{item['id']}").json()['status'] == 'conflict'


def test_junction_hardlink_and_root_replacement(project, tmp_path):
    root = Path(project['root_path']); outside = tmp_path / 'outside'; outside.mkdir()
    (outside/'secret').write_text('safe')
    link = root/'link'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(HTTPException): propose(project, 'link/secret')
        with pytest.raises(HTTPException): propose(project, 'link/new', 'bad', 'create')
    finally: link.rmdir()
    os.link(root/'a.txt', root/'hard.txt')
    with pytest.raises(HTTPException): propose(project)
    (root/'hard.txt').unlink()
    item = propose(project)
    moved = root.with_name('moved'); root.rename(moved); root.mkdir()
    (root/'a.txt').write_text('replacement')
    with pytest.raises(HTTPException): changes.apply(item['id'])
    assert (root/'a.txt').read_text() == 'replacement'
    assert (moved/'a.txt').read_bytes() == b'old\n'


def test_native_handle_excludes_writers_and_rename(project):
    root = Path(project['root_path'])
    with workspace_write.locked_target(project, 'a.txt') as file:
        with pytest.raises(OSError): (root/'a.txt').write_text('other')
        with pytest.raises(OSError): root.rename(root.with_name('moved'))
        with pytest.raises(OSError): (root/'a.txt').rename(root/'b.txt')
        assert file.read() == b'old\n'


@pytest.mark.parametrize('stage,expected', [('before','proposed'), ('partial','interrupted'), ('after','applied')])
def test_crash_reconciliation_never_blindly_retries(project, monkeypatch, stage, expected):
    item = propose(project)
    original = workspace_write.write_content
    def crash(file, data):
        if stage == 'partial': original(file, b'partial')
        if stage == 'after': original(file, data)
        raise SystemExit('simulated process termination')
    monkeypatch.setattr(workspace_write, 'write_content', crash)
    with pytest.raises(SystemExit): changes.apply(item['id'])
    with SessionLocal() as db: assert db.get(ProjectChange, item['id']).status == 'applying'
    changes.recover_interrupted()
    detail = client.get(f"/api/project-changes/{item['id']}").json()
    assert detail['status'] == expected
    if stage == 'partial':
        with pytest.raises(HTTPException): changes.apply(item['id'])
        assert (Path(project['root_path'])/'a.txt').read_bytes() == b'partial'


def test_archived_deleted_cross_project_and_active_task(project, db, tmp_path):
    item = propose(project)
    conv = db.get(Conversation, 'c_edit'); conv.archived_at = datetime.utcnow(); db.commit()
    assert client.post(f"/api/project-changes/{item['id']}/apply", json={'revision':1}).status_code == 409
    conv.archived_at = None; db.commit()
    root = tmp_path/'other'; root.mkdir(); other = workspace.register(db, str(root))
    db.add(Conversation(id='c_other', project_id=other.id)); db.commit()
    changes.apply(item['id'])
    with pytest.raises(HTTPException): changes.undo(item['id'], 'c_other')
    other_item = propose(project, content='again', run_id='task_owner')
    db.add(TaskRecord(id='task_owner', type='agent', status='pending', payload={})); db.commit()
    assert client.post(f"/api/project-changes/{other_item['id']}/apply", json={'revision':1}).status_code == 409
    assert client.post(f"/api/project-changes/{other_item['id']}/reject", json={'revision':1}).status_code == 409
    db.delete(db.get(TaskRecord,'task_owner')); db.commit()
    assert client.delete('/api/conversations/c_edit').status_code == 200
    with pytest.raises(HTTPException): changes.apply(other_item['id'])
    assert client.get(f"/api/project-changes/{item['id']}").status_code == 200


def test_diff_pagination_escaping_and_no_final_newline_patch(project):
    item = propose(project, content='\n'.join(f'<script>{i}</script>' for i in range(850)))
    one = client.get(f"/api/project-changes/{item['id']}").json()
    two = client.get(f"/api/project-changes/{item['id']}?offset={one['diff']['next_offset']}").json()
    assert len(one['diff']['lines']) == len(two['diff']['lines']) == 400
    patch = client.get(f"/api/project-changes/{item['id']}/patch").text
    assert '\\ No newline at end of file' in patch and '+<script>849</script>\n' in patch


def agent_model(project, monkeypatch):
    from app.agent import loop
    from app.llm import LLMResponse
    class Editor:
        def stream_chat(self, messages, tools=None):
            if not any(m['role'] == 'tool' for m in messages):
                yield 'final', LLMResponse('', [{'id':'edit1','name':'edit_project_file','arguments': {
                    'path':'a.txt','operation':'update','expected_sha256':changes.digest(b'old\n'),
                    'content':'agent changed\n','summary':'Model edit'}}])
            else: yield 'final', LLMResponse('File change processed.')
    monkeypatch.setattr(loop, 'get_llm', Editor)
    monkeypatch.setattr(loop, 'make_plan', lambda *a: [])
    monkeypatch.setattr(loop.settings, 'memory_recall', False)
    monkeypatch.setattr(loop.settings, 'memory_capture', False)
    return loop


@pytest.mark.parametrize('mode,decision,expected', [('plan','approve',None), ('execute','deny','rejected'), ('execute','approve','applied'), ('full','approve','applied'), ('hook','approve',None)])
def test_agent_respects_plan_hooks_and_approval(project, db, monkeypatch, mode, decision, expected):
    loop = agent_model(project, monkeypatch)
    monkeypatch.setattr(loop.settings, 'approval_mode', 'full_access' if mode == 'full' else 'read_only')
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda name, *a, **kw: {'ran': int(mode=='hook'), 'blocked': mode=='hook' and name=='PreToolUse', 'feedback':''})
    def approve(id):
        pending = loop.approval_store.get(id)
        assert 'content' not in pending.arguments and pending.arguments['change_id']
        assert (Path(project['root_path'])/'a.txt').read_bytes() == b'old\n'
        assert client.get('/api/project-changes/' + pending.arguments['change_id']).json()['diff']['added'] == 1
        loop.approval_store.resolve(id, decision)
    monkeypatch.setattr(loop.approval_store, 'wait', approve)
    events = list(loop.run_agent(db, 'Edit a file', conversation_id='c_edit', run_id='chat_edit_test',
        execution_mode='plan' if mode=='plan' else 'execute', workspace={**project,'files':[],'rules':[]}))
    db.expire_all(); rows = db.query(ProjectChange).all()
    assert [row.status for row in rows] == ([expected] if expected else [])
    assert (Path(project['root_path'])/'a.txt').read_bytes() == (b'agent changed\n' if expected=='applied' else b'old\n')
    assert bool([e for k,e in events if k=='approval_request']) == (mode=='execute')
    assert any(k=='answer' for k,_ in events)


def test_agent_checkpoint_retry_does_not_repeat_write(project, db, monkeypatch):
    from copy import deepcopy
    loop = agent_model(project, monkeypatch)
    monkeypatch.setattr(loop.settings, 'approval_mode', 'full_access')
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **kw: {'ran':0,'blocked':False})
    snapshots = []
    apply = changes.apply
    def crash(*a, **kw):
        apply(*a, **kw)
        raise SystemExit('lost result after actual write')
    monkeypatch.setattr(changes, 'apply', crash)
    args = dict(conversation_id='c_edit',run_id='chat_retry_edit',workspace={**project,'files':[],'rules':[]})
    with pytest.raises(SystemExit):
        list(loop.run_agent(db, 'Edit a file', **args, save_checkpoint=lambda x:snapshots.append(deepcopy(x))))
    saved = snapshots[-1]; assert saved['pending_operation']['name'] == 'edit_project_file'
    saved['resolution'] = 'retry'
    monkeypatch.setattr(changes, 'apply', apply)
    monkeypatch.setattr(workspace_write, 'write_content', lambda *a: pytest.fail('repeat write'))
    events = list(loop.run_agent(db, 'Edit a file', **args, checkpoint=saved))
    assert any(kind=='answer' for kind,_ in events)
    assert db.query(ProjectChange).count() == 1


def test_editor_draft_preserves_cas_and_restart_storage(project):
    url = '/api/desktop-state/conversations/c_edit'
    editor = {'project_id':project['project_id'], 'path':'a.txt','operation':'update','sha256':changes.digest(b'old\n'),'content':'unsaved edit'}
    initial = client.get(url).json()
    value = {**initial['value'],'text':'chat draft','editor':editor}
    saved = client.put(url,json={'revision':initial['revision'],'value':value})
    assert saved.status_code == 200
    assert client.get(url).json()['value']['editor'] == editor
    assert client.put(url,json={'revision':initial['revision'],'value':{**value,'editor':None}}).status_code == 409


@pytest.mark.parametrize('stage,status,content', [('before','proposed',b'before'),('partial','interrupted',b'part'),('after','applied',b'after')])
def test_real_process_exit_and_new_process_recovery(tmp_path, stage, status, content):
    probe = str(Path(__file__).with_name('file_change_crash_probe.py'))
    env = {**os.environ,'DATA_DIR':str(tmp_path),'PYTHONIOENCODING':'utf-8'}
    crash = subprocess.run([sys.executable,probe,stage],env=env,capture_output=True,timeout=20)
    assert crash.returncode == 73, crash.stderr.decode('utf-8')
    restored = subprocess.run([sys.executable,probe,'recover'],env=env,capture_output=True,timeout=20)
    assert restored.returncode == 0, restored.stderr.decode('utf-8')
    assert restored.stdout.decode().strip() == status
    assert (tmp_path/'project/a.txt').read_bytes() == content
