"""Real index/commit writes, stale reviews, lost responses and interrupted hooks."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import git_operations as ops, git_process, workspace
from app.db import SessionLocal
from app.main import app
from app.models import GitOperation
from tests.test_project_git import git


@pytest.fixture
def repo(db, tmp_path):
    root = tmp_path / 'repository'; root.mkdir()
    git(root, 'init', '-b', 'main')
    for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
        git(root, 'config', key, value)
    (root / 'a.txt').write_text('original\n')
    git(root, 'add', '.'); git(root, 'commit', '-m', 'Initial')
    project = workspace.register(db, str(root)); db.commit()
    git_process.start()
    return root, workspace.project_scope(project)


def prepare(scope, action, paths=None, message='', key='request_0001'):
    return ops.prepare(scope, ops.Request(request_id=key, action=action, paths=paths, message=message))


def apply(scope, row):
    return ops.decide(scope['project_id'], row['id'], row['revision'])


def test_stage_unstage_preserve_worktree_and_lost_responses(repo):
    root, scope = repo
    (root / 'a.txt').write_text('staged\n')
    row = prepare(scope, 'stage', ['a.txt'])
    assert '+staged' in row['patch'] and row['status'] == 'prepared'
    assert not git(root, 'diff', '--cached')
    assert prepare(scope, 'stage', ['a.txt'])['id'] == row['id']
    assert apply(scope, row)['status'] == 'applied'
    assert apply(scope, row)['status'] == 'applied'
    assert '+staged' in git(root, 'diff', '--cached')
    row = prepare(scope, 'unstage', ['a.txt'], key='request_0002')
    assert apply(scope, row)['status'] == 'applied'
    assert not git(root, 'diff', '--cached')
    assert (root / 'a.txt').read_text() == 'staged\n'


def test_commit_only_reviewed_index_preserves_later_working_edits(repo):
    root, scope = repo
    (root / 'a.txt').write_text('staged\n'); git(root, 'add', 'a.txt')
    row = prepare(scope, 'commit', message='提交中文\n\nDetails')
    (root / 'a.txt').write_text('later working copy\n')
    result = apply(scope, row)
    assert result['status'] == 'applied', result
    assert git(root, 'show', 'HEAD:a.txt') == 'staged'
    assert (root / 'a.txt').read_text() == 'later working copy\n'
    assert git(root, 'log', '-1', '--format=%B').startswith('提交中文')
    assert apply(scope, row)['result_head'] == result['result_head']
    assert git(root, 'rev-list', '--count', 'HEAD') == '2'


@pytest.mark.parametrize('change', ['working', 'index', 'branch', 'head'])
def test_stale_stage_review_conflicts_even_if_status_is_still_modified(repo, change):
    root, scope = repo
    (root / 'a.txt').write_text('before review\n')
    row = prepare(scope, 'stage', ['a.txt'])
    if change == 'working':
        (root / 'a.txt').write_text('after review\n')
    elif change == 'index':
        git(root, 'add', 'a.txt')
    elif change == 'branch':
        git(root, 'checkout', '-b', 'other')
    else:
        git(root, 'commit', '--allow-empty', '-m', 'External commit')
    result = apply(scope, row)
    assert result['status'] == 'conflict'
    assert '重新生成审阅' in result['error']


def test_commit_rejects_changed_index(repo):
    root, scope = repo
    (root / 'a.txt').write_text('reviewed\n'); git(root, 'add', 'a.txt')
    row = prepare(scope, 'commit', message='Review me')
    (root / 'a.txt').write_text('external staging\n'); git(root, 'add', 'a.txt')
    assert apply(scope, row)['status'] == 'conflict'
    assert git(root, 'rev-list', '--count', 'HEAD') == '1'


def test_commit_detects_identity_config_changes(repo):
    root, scope = repo
    (root / 'a.txt').write_text('staged'); git(root, 'add', 'a.txt')
    row = prepare(scope, 'commit', message='Identity review')
    git(root, 'config', 'user.name', 'Someone else')
    assert apply(scope, row)['status'] == 'conflict'


def test_stage_detects_changed_effective_attributes(repo):
    root, scope = repo
    (root / 'a.txt').write_text('changed')
    row = prepare(scope, 'stage', ['a.txt'])
    (root / '.gitattributes').write_text('*.txt -text\n')
    assert apply(scope, row)['status'] == 'conflict'
    assert git(root, 'diff', '--cached') == ''


def test_missing_identity_is_actionable_without_changing_config(repo):
    root, scope = repo
    (root / 'a.txt').write_text('staged'); git(root, 'add', 'a.txt')
    git(root, 'config', 'user.name', '')
    with pytest.raises(HTTPException) as error:
        prepare(scope, 'commit', message='No author')
    assert error.value.status_code == 409
    assert git(root, 'config', 'user.name') == ''


def test_index_lock_failure_requires_new_review(repo):
    root, scope = repo
    (root / 'a.txt').write_text('changed')
    row = prepare(scope, 'stage')
    lock = root / '.git/index.lock'; lock.write_text('external process')
    try:
        result = apply(scope, row)
        assert result['status'] == 'failed' and 'index.lock' in result['output']
    finally:
        lock.unlink()
    assert apply(scope, row)['status'] == 'failed'
    assert apply(scope, prepare(scope, 'stage', key='request_0002'))['status'] == 'applied'


def test_concurrent_confirmation_creates_only_one_commit(repo):
    from concurrent.futures import ThreadPoolExecutor
    root, scope = repo
    (root / 'a.txt').write_text('staged'); git(root, 'add', 'a.txt')
    row = prepare(scope, 'commit', message='Only once')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(scope, row), range(2)))
    assert all(r['status'] == 'applied' for r in results)
    assert results[0]['result_head'] == results[1]['result_head']
    assert git(root, 'rev-list', '--count', 'HEAD') == '2'


def test_mutation_timeout_is_unknown_and_never_replayed(repo, monkeypatch):
    root, scope = repo
    (root / 'a.txt').write_text('changed')
    row = prepare(scope, 'stage')
    calls = []
    def timeout(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired('git', 60)
    monkeypatch.setattr(git_process, 'run', timeout)
    assert apply(scope, row)['status'] == 'interrupted'
    assert apply(scope, row)['status'] == 'interrupted'
    assert len(calls) == 1


def test_literal_paths_rename_delete_binary_and_bulk(repo):
    root, scope = repo
    git(root, 'mv', 'a.txt', 'renamed.txt')
    row = prepare(scope, 'unstage')
    assert set(row['paths']) == {'a.txt', 'renamed.txt'}
    assert apply(scope, row)['status'] == 'applied'
    assert not (root / 'a.txt').exists() and (root / 'renamed.txt').exists()
    (root / '[literal].txt').write_text('literal\n')
    (root / 'binary.bin').write_bytes(b'\0binary')
    row = prepare(scope, 'stage', key='request_0002')
    assert '[binary]' in row['patch']
    assert apply(scope, row)['status'] == 'applied'
    assert '[literal].txt' in git(root, 'ls-files')
    assert git(root, 'diff', '--name-only') == ''


def test_unborn_unstage_never_deletes_working_files(repo, db, tmp_path):
    _, _ = repo
    root = tmp_path / 'unborn'; root.mkdir(); git(root, 'init', '-b', 'empty')
    (root / 'new.txt').write_text('keep me')
    git(root, 'add', '.')
    project = workspace.register(db, str(root)); db.commit(); scope = workspace.project_scope(project)
    row = prepare(scope, 'unstage')
    assert apply(scope, row)['status'] == 'applied'
    assert git(root, 'ls-files') == '' and (root / 'new.txt').read_text() == 'keep me'


def test_linked_worktree_commits_on_own_branch(repo, db, tmp_path):
    root, _ = repo
    linked = tmp_path / 'linked'; git(root, 'worktree', 'add', '-b', 'feature', str(linked))
    project = workspace.register(db, str(linked)); db.commit(); scope = workspace.project_scope(project)
    (linked / 'a.txt').write_text('linked\n')
    assert apply(scope, prepare(scope, 'stage'))['status'] == 'applied'
    result = apply(scope, prepare(scope, 'commit', message='Linked', key='request_0002'))
    assert result['status'] == 'applied'
    assert git(root, 'log', '-1', '--format=%s') == 'Initial'
    assert git(linked, 'log', '-1', '--format=%s') == 'Linked'


def test_cancel_request_collision_and_wrong_project(repo):
    root, scope = repo
    (root / 'a.txt').write_text('changed')
    row = prepare(scope, 'stage')
    with pytest.raises(HTTPException):
        prepare(scope, 'unstage')
    with pytest.raises(HTTPException):
        ops.decide('other_project', row['id'], 1)
    result = ops.decide(scope['project_id'], row['id'], 1, cancel=True)
    assert result['status'] == 'cancelled'
    assert apply(scope, row)['status'] == 'cancelled'
    assert not git(root, 'diff', '--cached')


@pytest.mark.parametrize('marker', ['MERGE_HEAD', 'CHERRY_PICK_HEAD', 'rebase-merge'])
def test_special_git_workflow_rejected(repo, marker):
    root, scope = repo
    (root / 'a.txt').write_text('changed')
    path = root / '.git' / marker
    path.mkdir() if marker == 'rebase-merge' else path.write_text(git(root, 'rev-parse', 'HEAD'))
    with pytest.raises(HTTPException) as error:
        prepare(scope, 'stage')
    assert error.value.status_code == 409


def test_real_hook_failure_preserved_and_not_replayed(repo):
    root, scope = repo
    (root / 'a.txt').write_text('staged'); git(root, 'add', 'a.txt')
    hook = root / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\necho hook-refused >&2\nexit 1\n', encoding='utf-8', newline='\n')
    hook.chmod(0o755)
    result = apply(scope, prepare(scope, 'commit', message='Hook test'))
    assert result['status'] == 'failed' and 'hook-refused' in result['output']
    hook.unlink()
    assert apply(scope, result)['status'] == 'failed'
    assert git(root, 'rev-list', '--count', 'HEAD') == '1'


def test_restart_marks_running_unknown_but_retains_prepared(repo):
    root, scope = repo
    (root / 'a.txt').write_text('changed')
    row = prepare(scope, 'stage')
    other = prepare(scope, 'stage', key='request_0002')
    with SessionLocal() as db:
        db.get(GitOperation, row['id']).status = 'running'; db.commit()
    ops.recover_interrupted()
    with SessionLocal() as db:
        assert db.get(GitOperation, row['id']).status == 'interrupted'
        assert db.get(GitOperation, other['id']).status == 'prepared'
    assert not git(root, 'diff', '--cached')


def test_shutdown_terminates_hook_tree_and_does_not_replay(repo):
    root, scope = repo
    (root / 'a.txt').write_text('changed'); git(root, 'add', 'a.txt')
    hook = root / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\necho started > hook-started\nsleep 2\necho bad > hook-finished\n', newline='\n')
    hook.chmod(0o755)
    row = prepare(scope, 'commit', message='Stop hook')
    results = []
    thread = threading.Thread(target=lambda: results.append(apply(scope, row)))
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not (root / 'hook-started').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (root / 'hook-started').exists()
        git_process.shutdown(); thread.join(10)
        assert not thread.is_alive() and not ops.active()
        assert results[0]['status'] in ('failed', 'interrupted')
        time.sleep(2.5)
        assert not (root / 'hook-finished').exists()
        assert git(root, 'rev-list', '--count', 'HEAD') == '1'
    finally:
        git_process.start()


@pytest.mark.skipif(os.name != 'nt', reason='Windows job ownership')
def test_process_crash_kills_hook_descendants(repo, tmp_path):
    root, _ = repo
    hook = root / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\necho started > hook-started\nsleep 2\necho bad > hook-finished\n', newline='\n')
    hook.chmod(0o755)
    (root / 'a.txt').write_text('staged'); git(root, 'add', 'a.txt')
    script = """
from app import git_process
import os, subprocess, sys
git_process.run(['git', '-C', sys.argv[1], 'commit', '-m', 'Crash test'], input=b'',
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
"""
    child = subprocess.Popen([sys.executable, '-c', script, str(root)],
                             env={**os.environ, 'DATA_DIR': str(tmp_path / 'child-data')},
                             creationflags=0x08000000)
    try:
        deadline = time.monotonic() + 10
        while not (root / 'hook-started').exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(.05)
        assert (root / 'hook-started').exists()
        child.kill(); child.wait(5)
        time.sleep(2.5)
        assert not (root / 'hook-finished').exists()
        assert git(root, 'rev-list', '--count', 'HEAD') == '1'
    finally:
        if child.poll() is None:
            child.kill(); child.wait(5)


def test_http_review_history_and_draft_cas(repo):
    root, scope = repo
    (root / 'a.txt').write_text('changed')
    client = TestClient(app); base = f"/api/projects/{scope['project_id']}/git"
    payload = {'request_id': 'http_request01', 'action': 'stage', 'paths': ['a.txt']}
    response = client.post(base + '/operations', json=payload)
    assert response.status_code == 200, response.text
    row = response.json()
    assert client.get(base + '/operations').json()['operations'][0]['id'] == row['id']
    assert client.get(base + '/operations/' + row['id']).json()['patch']
    assert client.post(base + f"/operations/{row['id']}/apply", json={'revision': 1}).json()['status'] == 'applied'
    path = f"/api/desktop-state/projects/{scope['project_id']}/git"
    value = {'message': 'Draft commit', 'pending': None}
    assert client.put(path, json={'revision': 0, 'value': value}).status_code == 200
    assert client.put(path, json={'revision': 0, 'value': {'message': 'Other'}}).status_code == 409
    assert client.get(path).json()['value'] == value
