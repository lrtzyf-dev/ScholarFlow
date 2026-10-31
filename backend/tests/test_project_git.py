"""Real Git repositories, worktrees and hostile path/config cases."""
import os
from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import workspace, project_git
from app.agent.tools import execute_tool, tool_risk
from app.execution_mode import PLAN_READ_TOOLS
from app.main import app

client = TestClient(app)


def git(root, *args):
    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *args], cwd=root, check=True, capture_output=True,
                          env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1'}).stdout.decode('utf-8').strip()


@pytest.fixture
def project(db, tmp_path):
    root = tmp_path / 'git-project'; root.mkdir()
    git(root, 'init', '-b', 'main')
    (root / 'main.py').write_text('print("original")\n', encoding='utf-8')
    (root / 'old name.txt').write_text('old name\n', encoding='utf-8')
    git(root, 'add', '--', 'main.py', 'old name.txt')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Initial')
    item = workspace.register(db, str(root)); db.commit()
    return workspace.project_scope(item)


def test_status_diff_history_and_agent_scope(project, db):
    root = Path(project['root_path'])
    (root / 'main.py').write_text('print("staged")\n', encoding='utf-8')
    git(root, 'add', '--', 'main.py')
    (root / 'main.py').write_text('print("working")\n', encoding='utf-8')
    (root / 'new file.txt').write_text('untracked <img src=x onerror=alert(1)>', encoding='utf-8')
    response = client.get(f"/api/projects/{project['project_id']}/git/status")
    assert response.status_code == 200
    status = response.json()
    assert status['available'] and status['branch'] == 'main' and status['head']
    assert status['total'] == 2 and not status['linked_worktree']
    main = next(e for e in status['entries'] if e['path'] == 'main.py')
    assert main['staged'] and main['unstaged'] and main['index'] == main['worktree'] == 'M'
    new = next(e for e in status['entries'] if e['path'] == 'new file.txt')
    assert new['untracked'] and not new['staged']
    staged = project_git.diff(project, 'main.py', 'staged')
    working = project_git.diff(project, 'main.py', 'unstaged')
    assert '+print("staged")' in staged['patch'] and 'working' not in staged['patch']
    assert '+print("working")' in working['patch'] and '-print("staged")' in working['patch']
    assert '<img' in project_git.diff(project, 'new file.txt', 'untracked')['preview']
    assert project_git.history(project)[0]['subject'] == 'Initial'
    for name in ['git_project_status', 'git_project_diff']:
        assert tool_risk(name) == 'safe' and name in PLAN_READ_TOOLS
    import json
    result = json.loads(execute_tool(db, 'git_project_diff', {'path': 'main.py', 'version': 'staged'}, project_scope=project))
    assert result['patch'] == staged['patch']
    for args in [{'path': 'main.py', 'version': 'staged', 'root': 'C:/'}, {'path': '../secret', 'version': 'staged'}]:
        assert 'error' in json.loads(execute_tool(db, 'git_project_diff', args, project_scope=project))
    assert 'error' in json.loads(execute_tool(db, 'git_project_status', {}))


def test_rename_spaces_binary_and_untracked_page(project):
    root = Path(project['root_path'])
    git(root, 'mv', '--', 'old name.txt', 'renamed name.txt')
    (root / 'binary.bin').write_bytes(b'\x00\x01\x02')
    response = project_git.status(project)
    renamed = next(e for e in response['entries'] if e['path'] == 'renamed name.txt')
    assert renamed['original_path'] == 'old name.txt' and renamed['staged']
    assert project_git.diff(project, 'renamed name.txt', 'staged')['patch']
    with pytest.raises(HTTPException): project_git.diff(project, 'missing.txt', 'untracked')


def test_untracked_binary_rejected_and_pathspec_injection(project):
    root = Path(project['root_path'])
    (root / 'binary.bin').write_bytes(b'\x00\x01\x02')
    with pytest.raises(HTTPException): project_git.diff(project, 'binary.bin', 'untracked')
    for value in ['../main.py', 'C:/Windows/win.ini', ':!main.py', '.git/config']:
        with pytest.raises(HTTPException): project_git.diff(project, value, 'staged')
    with pytest.raises(HTTPException): project_git.diff(project, 'main.py', 'unknown')
    with pytest.raises(HTTPException): project_git.status(project, -1)


def test_non_repo_parent_repo_and_replaced_root(project, db, tmp_path):
    empty = tmp_path / 'empty'; empty.mkdir()
    item = workspace.register(db, str(empty)); db.commit()
    assert not project_git.status(workspace.project_scope(item))['available']
    assert client.get(f'/api/projects/{item.id}/git/history').json()['commits'] == []
    nested = Path(project['root_path']) / 'nested'; nested.mkdir()
    child = workspace.register(db, str(nested)); db.commit()
    assert not project_git.status(workspace.project_scope(child))['available']
    moved = Path(project['root_path']).with_name('moved'); Path(project['root_path']).rename(moved)
    Path(project['root_path']).mkdir()
    with pytest.raises(HTTPException): project_git.status(project)


def test_worktree_branch_and_pagination(project, db, tmp_path):
    root = Path(project['root_path'])
    linked = tmp_path / 'linked'
    git(root, 'worktree', 'add', '-b', 'feature', str(linked))
    linked_project = workspace.register(db, str(linked)); db.commit()
    status = project_git.status(workspace.project_scope(linked_project))
    assert status['available'] and status['linked_worktree'] and status['branch'] == 'feature'
    for i in range(205):
        (root / f'file-{i:03}.txt').write_text(str(i))
    first = project_git.status(project)
    second = project_git.status(project, first['next_offset'])
    assert first['total'] == 205 and len(first['entries']) == 200 and len(second['entries']) == 5
    assert second['next_offset'] is None


def test_missing_git_timeout_and_output_limit(project, monkeypatch):
    monkeypatch.setattr(project_git.shutil, 'which', lambda _: None)
    with pytest.raises(HTTPException) as missing: project_git.status(project)
    assert missing.value.status_code == 503
    monkeypatch.undo()
    def timeout(*a, **kw): raise subprocess.TimeoutExpired('git', 20)
    monkeypatch.setattr(project_git.subprocess, 'run', timeout)
    with pytest.raises(HTTPException) as expired: project_git.status(project)
    assert expired.value.status_code == 504


def test_external_diff_command_not_run(project):
    root = Path(project['root_path'])
    marker = root / 'external-ran.txt'
    git(root, 'config', 'diff.external', f'cmd /c echo unsafe > "{marker}"')
    (root / 'main.py').write_text('changed\n')
    assert '+changed' in project_git.diff(project, 'main.py', 'unstaged')['patch']
    assert not marker.exists()


def test_diff_and_untracked_pagination(project):
    root = Path(project['root_path'])
    (root / 'main.py').write_text(''.join(f'line {i}\n' for i in range(850)), encoding='utf-8')
    first = project_git.diff(project, 'main.py', 'unstaged')
    second = project_git.diff(project, 'main.py', 'unstaged', first['next_offset'])
    third = project_git.diff(project, 'main.py', 'unstaged', second['next_offset'])
    assert first['next_offset'] == 400 and second['next_offset'] == 800 and third['next_offset'] is None
    assert 'line 849' in third['patch']
    (root / 'new.txt').write_text('A' * 12000, encoding='utf-8')
    first = project_git.diff(project, 'new.txt', 'untracked')
    second = project_git.diff(project, 'new.txt', 'untracked', first['next_offset'])
    assert len(first['preview']) == 8000 and len(second['preview']) == 4000 and second['next_offset'] is None
    (root / 'new.txt').write_text('changed', encoding='utf-8')
    with pytest.raises(HTTPException): project_git.diff(project, 'new.txt', 'untracked', first['next_offset'], first['snapshot'])


def test_stale_diff_and_status_pages(project):
    root = Path(project['root_path'])
    (root / 'main.py').write_text('changed\n')
    first = project_git.diff(project, 'main.py', 'unstaged')
    (root / 'main.py').write_text('changed again\n')
    with pytest.raises(HTTPException): project_git.diff(project, 'main.py', 'unstaged', 1, first['snapshot'])
    first = project_git.status(project)
    (root / 'new.txt').write_text('new')
    with pytest.raises(HTTPException): project_git.status(project, 1, first['snapshot'])


def test_git_environment_cannot_override_registered_scope(project, monkeypatch, tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir(); git(outside, 'init')
    monkeypatch.setenv('GIT_DIR', str(outside / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(outside))
    assert project_git.status(project)['branch'] == 'main'


def test_junction_diff_and_long_line_rejected(project, tmp_path):
    root = Path(project['root_path'])
    outside = tmp_path / 'outside'; outside.mkdir()
    (outside/'secret.txt').write_text('secret')
    link = root / 'escape'
    subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],check=True,capture_output=True)
    try:
        with pytest.raises(HTTPException): project_git.diff(project, 'escape/secret.txt', 'unstaged')
    finally: link.rmdir()
    (root/'main.py').write_text('A' * 32769)
    with pytest.raises(HTTPException) as error: project_git.diff(project, 'main.py', 'unstaged')
    assert error.value.status_code == 413


def test_tracked_dependency_paths_do_not_break_status(project):
    root = Path(project['root_path']); (root/'node_modules').mkdir()
    (root/'node_modules/a.js').write_text('tracked dependency')
    git(root,'add','--','node_modules/a.js')
    assert any(e['path']=='node_modules/a.js' for e in project_git.status(project)['entries'])
    with pytest.raises(HTTPException): project_git.diff(project,'node_modules/a.js','staged')


@pytest.mark.parametrize('mode', ['plan', 'execute'])
def test_real_agent_dispatch_uses_server_scope(project, db, monkeypatch, mode):
    import json
    from app.models import Conversation
    from app.llm import LLMResponse
    from app.agent import loop
    db.add(Conversation(id='git-agent',project_id=project['project_id'])); db.commit()
    root = Path(project['root_path']); (root/'main.py').write_text('agent can inspect\n')
    results = []
    class Inspector:
        def stream_chat(self,messages,tools=None):
            if not any(m['role']=='tool' for m in messages):
                assert {'git_project_status','git_project_diff'} <= {t['name'] for t in tools}
                yield 'final', LLMResponse('', [
                    {'id':'git1','name':'git_project_status','arguments':{}},
                    {'id':'git2','name':'git_project_diff','arguments':{'path':'main.py','version':'unstaged'}},
                ])
            else:
                results.extend(json.loads(m['content']) for m in messages if m['role']=='tool')
                yield 'final', LLMResponse('The repository has a changed main.py file.')
    monkeypatch.setattr(loop,'get_llm',Inspector)
    monkeypatch.setattr(loop,'make_plan',lambda *a: [])
    monkeypatch.setattr(loop.settings,'memory_recall',False)
    monkeypatch.setattr(loop.settings,'approval_mode','read_only')
    events=list(loop.run_agent(db,'Inspect Git',conversation_id='git-agent',execution_mode=mode,
                             workspace={**project,'rules':[],'files':[]}))
    assert results[0]['branch']=='main' and '+agent can inspect' in results[1]['patch']
    assert not any(k=='approval_request' for k,_ in events)


def test_unborn_detached_and_tracked_binary(project, db, tmp_path):
    root = Path(project['root_path'])
    git(root,'checkout','--detach')
    result = project_git.status(project)
    assert result['branch'] is None and result['head']
    (root/'binary.bin').write_bytes(b'\x00before')
    git(root,'add','--','binary.bin')
    git(root,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','binary')
    (root/'binary.bin').write_bytes(b'\x00after')
    assert project_git.diff(project,'binary.bin','unstaged')['binary']
    unborn = tmp_path/'unborn'; unborn.mkdir(); git(unborn,'init','-b','empty')
    item = workspace.register(db,str(unborn)); db.commit()
    result = project_git.status(workspace.project_scope(item))
    assert result['available'] and result['branch']=='empty' and result['head'] is None
    assert project_git.history(workspace.project_scope(item)) == []
