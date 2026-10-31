"""Real Git archive commits, byte-exact backups, conflicts and interrupted recovery."""
from pathlib import Path
import json
import subprocess
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import worktrees as wt, worktree_archive as archive, run_store
from app.db import SessionLocal
from app.main import app
from app.models import ManagedWorktree, Conversation
from tests.test_git_operations import repo, git
from tests.test_worktrees import source, create


def dirty(scope, **kwargs):
    row = create(scope, **kwargs); path = Path(row['path'])
    (path / 'a.txt').write_bytes(b'working\r\ncontent\x00binary')
    (path / 'new.txt').write_bytes('未跟踪 😀\r\n'.encode())
    return row, path


def archive_row(scope, row):
    review = wt.preview_archive(scope['project_id'], row['id'])
    return wt.archive_worktree(scope['project_id'], row['id'], review['revision'])


def test_archive_preserves_working_index_ignored_binary_and_original_branch(source):
    root, scope = source
    row = create(scope, branch='feature/archive'); path = Path(row['path'])
    before = git(path, 'rev-parse', 'HEAD')
    (path / 'a.txt').write_bytes(b'staged version\n'); git(path, 'add', 'a.txt')
    working = b'working version\r\n\x00\xff'
    (path / 'a.txt').write_bytes(working)
    (path / 'new 中文.txt').write_bytes('未跟踪 😀\r\n'.encode())
    (path / '.gitignore').write_text('private/\n', encoding='utf-8')
    (path / 'private').mkdir(); (path / 'private/token.txt').write_bytes(b'private local bytes')
    archived = archive_row(scope, row)
    assert archived['status'] == 'removed', archived
    assert not path.exists()
    info = archived['archive_info']
    assert info['ignored'] == 1 and info['files'] == 4
    assert git(root, 'show', info['index_head'] + ':a.txt') == 'staged version'
    assert git(root, 'rev-parse', 'feature/archive') == before
    assert git(root, 'rev-parse', info['ref']) == archived['saved_head']
    assert 'private/token.txt' not in git(root, 'ls-tree', '-r', '--name-only', archived['saved_head'])
    assert wt.archive_worktree(scope['project_id'], row['id'], 1)['saved_head'] == archived['saved_head']
    with zipfile.ZipFile(wt.archive_download(scope['project_id'], row['id'])) as backup:
        assert backup.read('files/private/token.txt') == b'private local bytes'
        assert backup.read('files/a.txt') == working
        assert backup.read('original-index')
    restored = wt.restore(scope['project_id'], row['id'], archived['revision'])
    assert restored['status'] == 'ready', restored
    assert git(path, 'branch', '--show-current') == ''
    assert (path / 'a.txt').read_bytes() == working
    assert (path / 'new 中文.txt').read_bytes() == '未跟踪 😀\r\n'.encode()
    assert (path / 'private/token.txt').read_bytes() == b'private local bytes'
    assert git(path, 'diff', '--cached') == ''
    assert (root / 'a.txt').read_text() == 'original\n'


def test_deleted_and_staged_only_content_remain_recoverable(source):
    root, scope = source
    row = create(scope); path = Path(row['path'])
    (path / 'staged.txt').write_text('only in index'); git(path, 'add', 'staged.txt')
    (path / 'staged.txt').unlink(); (path / 'a.txt').unlink()
    result = archive_row(scope, row)
    assert result['status'] == 'removed', result
    assert git(root, 'show', result['archive_info']['index_head'] + ':staged.txt') == 'only in index'
    restored = wt.restore(scope['project_id'], row['id'], result['revision'])
    assert restored['status'] == 'ready' and not (path / 'a.txt').exists() and not (path / 'staged.txt').exists()


@pytest.mark.parametrize('change', ['working', 'untracked', 'ignored', 'index', 'head', 'branch'])
def test_changed_review_refuses_archive_without_modifying_files(source, change):
    _, scope = source
    row, path = dirty(scope)
    review = wt.preview_archive(scope['project_id'], row['id'])
    if change == 'working':
        (path / 'a.txt').write_text('later')
    elif change == 'untracked':
        (path / 'later.txt').write_text('later')
    elif change == 'ignored':
        (path / '.gitignore').write_text('later.txt'); (path / 'later.txt').write_text('later')
    elif change == 'index':
        git(path, 'add', 'new.txt')
    elif change == 'head':
        git(path, 'commit', '--allow-empty', '-m', 'Advance')
    else:
        git(path, 'switch', '-c', 'new-branch')
    with pytest.raises(HTTPException):
        wt.archive_worktree(scope['project_id'], row['id'], review['revision'])
    assert path.exists() and (path / 'new.txt').exists()


@pytest.mark.parametrize('kind', ['nested', 'bare', 'flags', 'merge', 'limit', 'locked'])
def test_unsupported_archive_never_removes_directory(source, monkeypatch, kind):
    _, scope = source
    row, path = dirty(scope)
    if kind == 'nested':
        (path / 'nested').mkdir(); git(path / 'nested', 'init')
    elif kind == 'bare':
        (path / 'nested').mkdir(); git(path / 'nested', 'init', '--bare')
    elif kind == 'flags':
        git(path, 'update-index', '--assume-unchanged', 'a.txt')
    elif kind == 'merge':
        (archive.git_dir(path) / 'MERGE_HEAD').write_text(git(path, 'rev-parse', 'HEAD'))
    elif kind == 'limit':
        monkeypatch.setattr(archive, 'MAX_BYTES', 1)
    else:
        git(path, 'worktree', 'lock', str(path))
    with pytest.raises(HTTPException):
        wt.preview_archive(scope['project_id'], row['id'])
    assert path.exists() and (path / 'new.txt').exists()


def test_active_run_blocks_archiving(source):
    _, scope = source
    row, path = dirty(scope)
    with SessionLocal() as db:
        db.add(Conversation(id='archive_busy', project_id=row['project_id'])); db.commit()
    review = wt.preview_archive(scope['project_id'], row['id'])
    assert run_store.begin('archive_run', 'archive_busy')
    try:
        with pytest.raises(HTTPException):
            wt.archive_worktree(scope['project_id'], row['id'], review['revision'])
        assert path.exists()
    finally:
        run_store.finish('archive_run', 'done')


def test_git_failure_before_snapshot_never_changes_original_head_or_index(source, monkeypatch):
    _, scope = source
    row, path = dirty(scope); head = git(path, 'rev-parse', 'HEAD')
    index = (archive.git_dir(path) / 'index').read_bytes()
    real = archive.command
    def fail(root, *args, **kw):
        if args[0] == 'commit-tree':
            raise RuntimeError('simulated failed snapshot')
        return real(root, *args, **kw)
    monkeypatch.setattr(archive, 'command', fail)
    result = archive_row(scope, row)
    assert result['status'] == 'interrupted'
    assert git(path, 'rev-parse', 'HEAD') == head
    # write-tree may refresh cache-tree metadata; logical staged content must not change.
    assert git(path, 'diff', '--cached') == '' and (path / 'new.txt').exists()
    assert wt.reconcile(scope['project_id'], row['id'], result['revision'])['status'] == 'ready'


def test_failed_removal_preserves_late_files_and_restores_ignored_backup(source, monkeypatch):
    _, scope = source
    row, path = dirty(scope)
    (path / '.gitignore').write_text('private.txt'); (path / 'private.txt').write_bytes(b'private')
    real = wt._run
    def late(root, *args):
        if args[:2] == ('worktree', 'remove'):
            (path / 'late.txt').write_text('keep later work')
        return real(root, *args)
    monkeypatch.setattr(wt, '_run', late)
    result = archive_row(scope, row)
    assert result['status'] == 'interrupted' and result['archive_info'], result
    recovered = wt.reconcile(scope['project_id'], row['id'], result['revision'])
    assert recovered['status'] == 'ready'
    assert (path / 'late.txt').read_text() == 'keep later work'
    assert (path / 'private.txt').read_bytes() == b'private'


def test_changed_files_during_snapshot_leave_directory_intact(source, monkeypatch):
    _, scope = source
    row, path = dirty(scope); original = git(path, 'rev-parse', 'HEAD')
    real = archive.save
    def late(*args):
        info = real(*args); (path / 'late.txt').write_text('after snapshot'); return info
    monkeypatch.setattr(archive, 'save', late)
    result = archive_row(scope, row)
    assert result['status'] == 'interrupted' and result['archive_info']
    assert git(path, 'rev-parse', 'HEAD') == original
    assert wt.reconcile(scope['project_id'], row['id'], result['revision'])['status'] == 'ready'
    assert (path / 'late.txt').read_text() == 'after snapshot'


def test_restore_checks_backup_hash_before_creating_checkout(source):
    _, scope = source
    row, path = dirty(scope); result = archive_row(scope, row)
    backup = wt.archive_download(scope['project_id'], row['id'])
    with backup.open('ab') as file:
        file.write(b'changed')
    with pytest.raises(HTTPException):
        wt.restore(scope['project_id'], row['id'], result['revision'])
    assert not path.exists()


@pytest.mark.parametrize('late', ['none', 'file', 'index'])
def test_interrupted_restore_completes_missing_files_without_overwriting_changes(source, monkeypatch, late):
    _, scope = source
    row, path = dirty(scope); result = archive_row(scope, row)
    real = archive.restore_files
    def fail(*args, **kwargs):
        raise RuntimeError('interrupted before file restore')
    monkeypatch.setattr(archive, 'restore_files', fail)
    result = wt.restore(scope['project_id'], row['id'], result['revision'])
    assert result['status'] == 'interrupted'
    monkeypatch.setattr(archive, 'restore_files', real)
    if late != 'none':
        (path / 'a.txt').write_text('later user edit')
        if late == 'index':
            git(path, 'add', 'a.txt')
        with pytest.raises(HTTPException):
            wt.reconcile(scope['project_id'], row['id'], result['revision'])
        assert (path / 'a.txt').read_text() == 'later user edit'
    else:
        assert wt.reconcile(scope['project_id'], row['id'], result['revision'])['status'] == 'ready'
        assert (path / 'new.txt').read_bytes() == '未跟踪 😀\r\n'.encode()


def test_archive_api_scope_and_backup_download(source):
    _, scope = source
    row, path = dirty(scope); client = TestClient(app)
    target = f"/api/projects/{scope['project_id']}/worktrees/{row['id']}"
    review = client.post(target + '/review-archive', json={}).json()
    response = client.post(target + '/archive', json={'revision': review['revision']})
    assert response.status_code == 200 and response.json()['status'] == 'removed', response.text
    assert client.get(target + '/archive-backup').content[:2] == b'PK'
    assert client.get('/api/projects/other/worktrees/' + row['id'] + '/archive-backup').status_code == 404


def test_crash_after_removal_recovers_saved_snapshot_and_can_archive_again(source, monkeypatch):
    _, scope = source
    row, path = dirty(scope)
    real = wt._save
    class Crash(BaseException):
        pass
    def crash(db, record, status=None):
        if status == 'removed':
            raise Crash()
        real(db, record, status)
    monkeypatch.setattr(wt, '_save', crash)
    with pytest.raises(Crash):
        archive_row(scope, row)
    assert not path.exists()
    monkeypatch.setattr(wt, '_save', real)
    wt.recover_interrupted()
    result = wt.detail(scope['project_id'], row['id'])
    assert result['status'] == 'interrupted' and result['archive_info']
    result = wt.reconcile(scope['project_id'], row['id'], result['revision'])
    assert result['status'] == 'removed'
    result = wt.restore(scope['project_id'], row['id'], result['revision'])
    assert result['status'] == 'ready' and (path / 'new.txt').exists()
    first = result['archive_info']
    (path / 'new.txt').write_text('second revision')
    result = archive_row(scope, row)
    assert result['status'] == 'removed' and result['archive_info']['ref'] != first['ref']
    assert (path.parent / first['backup']).exists()


def test_archive_history_lists_snapshots_and_cleans_only_old_entries(source):
    root, scope = source
    row, path = dirty(scope, branch='feature/history')
    first = archive_row(scope, row)
    first_info = first['archive_info']
    restored = wt.restore(scope['project_id'], row['id'], first['revision'])
    assert restored['status'] == 'ready'
    (path / 'new.txt').write_text('second snapshot', encoding='utf-8')
    second = archive_row(scope, row)
    second_info = second['archive_info']
    history = wt.archive_history(scope['project_id'], row['id'])
    assert history['current_id'] == second_info['id']
    assert {item['id'] for item in history['snapshots']} == {first_info['id'], second_info['id']}
    assert all(item['backup_valid'] for item in history['snapshots'])
    with pytest.raises(HTTPException):
        wt.delete_archive_snapshot(scope['project_id'], row['id'], second_info['id'])
    deleted = wt.delete_archive_snapshot(scope['project_id'], row['id'], first_info['id'])
    assert deleted == {'id': first_info['id'], 'deleted': True}
    remaining = wt.archive_history(scope['project_id'], row['id'])['snapshots']
    assert [item['id'] for item in remaining] == [second_info['id']]
    assert not (path.parent / first_info['backup']).exists()
    with pytest.raises(subprocess.CalledProcessError):
        git(root, 'show-ref', '--verify', first_info['ref'])


def test_legacy_worktree_table_gains_archive_column():
    from sqlalchemy import text
    from app.db import engine, _lightweight_migrations
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE managed_worktrees DROP COLUMN archive_info'))
    _lightweight_migrations()
    with engine.connect() as conn:
        assert 'archive_info' in {r[1] for r in conn.execute(text('PRAGMA table_info(managed_worktrees)'))}


def test_reconcile_restoration_is_counted_as_active_operation(source, monkeypatch):
    _, scope = source
    row, path = dirty(scope); archived = archive_row(scope, row)
    real = archive.restore_files
    monkeypatch.setattr(archive, 'restore_files', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('interrupted')))
    interrupted = wt.restore(scope['project_id'], row['id'], archived['revision'])
    assert interrupted['status'] == 'interrupted' and not wt.active()
    def checked(*args, **kwargs):
        assert wt.active()
        return real(*args, **kwargs)
    monkeypatch.setattr(archive, 'restore_files', checked)
    result = wt.reconcile(scope['project_id'], row['id'], interrupted['revision'])
    assert result['status'] == 'ready' and not wt.active()
