"""Real Git binary/index moves, interruption reconciliation and scope fences."""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import workspace_transfers as transfers, workspace, run_store, terminal_sessions
from app.db import SessionLocal
from app.main import app
from app.models import Conversation, DesktopState, WorkspaceTransfer, TaskRecord
from tests.test_worktrees import source, repo, git, create


@pytest.fixture
def setup(source):
    root, scope = source
    tree = create(scope)
    transfers._blocked.clear()
    with SessionLocal() as db:
        db.add(Conversation(id='move_chat', project_id=scope['project_id']))
        db.add(Conversation(id='other_chat', project_id=tree['project_id']))
        db.commit()
    yield root, scope, tree
    transfers._blocked.clear()


def prepare(tree):
    return transfers.prepare('move_chat', transfers.Prepare(request_id=uuid4().hex,
        target_project_id=tree['project_id'], workspace_revision=0, draft_revision=0))


def prepare_mode(tree, mode):
    return transfers.prepare('move_chat', transfers.Prepare(request_id=uuid4().hex,
        target_project_id=tree['project_id'], workspace_revision=0, draft_revision=0, mode=mode))


def decide(row, action='apply'):
    return transfers.decide('move_chat', row['id'], action)


def dirty(root):
    (root / 'a.txt').write_bytes(b'staged\r\n\x00\xff')
    git(root, 'add', 'a.txt')
    (root / 'a.txt').write_bytes(b'unstaged\r\n\x00\xfe')
    (root / '中文.bin').write_bytes(b'\x00\x80\xffuntracked')


def test_transfer_keeps_binary_staging_and_invalidates_all_affected_chats(setup):
    root, scope, tree = setup; target = Path(tree['path'])
    baseline = (target / 'a.txt').read_bytes()
    dirty(root)
    index = git(root, 'ls-files', '--stage')
    row = prepare(tree)
    assert row['review']['blockers'] == []
    result = decide(row)
    assert result['status'] == 'applied', result
    assert git(root, 'status', '--porcelain') == ''
    assert (root / 'a.txt').read_bytes() == baseline
    assert not (root / '中文.bin').exists()
    assert (target / 'a.txt').read_bytes() == b'unstaged\r\n\x00\xfe'
    assert (target / '中文.bin').read_bytes() == b'\x00\x80\xffuntracked'
    assert git(target, 'ls-files', '--stage') == index
    assert decide(row) == result
    with SessionLocal() as db:
        assert db.get(Conversation, 'move_chat').project_id == tree['project_id']
        for cid in ('move_chat', 'other_chat'):
            assert db.get(Conversation, cid).workspace_revision == 1
            assert db.get(DesktopState, 'conversation:' + cid).value['workspace_revision'] == 1
        assert transfers.backup_path(db.get(WorkspaceTransfer, row['id'])).is_file()


@pytest.mark.parametrize('phase,side,kind', [('backed_up', None, None),
    ('after_write', 'target', 'file'), ('after_write', 'target', 'index'),
    ('after_write', 'source', 'file'), ('after_write', 'source', 'index'), ('before_bind', None, None)])
@pytest.mark.parametrize('rollback', [False, True])
def test_durable_interruption_can_resume_or_restore_both_sides(setup, monkeypatch, phase, side, kind, rollback):
    root, scope, tree = setup; target = Path(tree['path'])
    dirty(root); before = git(root, 'ls-files', '--stage')
    row = prepare(tree)
    def crash(record, event):
        if event == phase and (not side or record.journal['phase'] == f'{side}_{kind}'):
            raise RuntimeError('injected process interruption')
    monkeypatch.setattr(transfers, '_checkpoint', crash)
    result = decide(row)
    assert result['status'] == 'interrupted', result
    with pytest.raises(HTTPException): workspace.root_path(scope)
    client = TestClient(app)
    assert client.delete('/api/conversations/move_chat').status_code == 409
    transfers._blocked.clear(); transfers.recover_interrupted()
    monkeypatch.setattr(transfers, '_checkpoint', lambda *args: None)
    result = decide(row, 'rollback' if rollback else 'apply')
    assert result['status'] == ('rolled_back' if rollback else 'applied'), result
    active, clean = (root, target) if rollback else (target, root)
    assert (active / 'a.txt').read_bytes() == b'unstaged\r\n\x00\xfe'
    assert (active / '中文.bin').read_bytes() == b'\x00\x80\xffuntracked'
    assert git(active, 'ls-files', '--stage') == before
    assert git(clean, 'status', '--porcelain') == ''


@pytest.mark.parametrize('side', ['source', 'target'])
def test_review_catches_same_status_changed_bytes(setup, side):
    root, scope, tree = setup; dirty(root)
    row = prepare(tree)
    (root if side == 'source' else Path(tree['path'])).joinpath('a.txt').write_bytes(b'later change')
    with pytest.raises(HTTPException): decide(row)
    with SessionLocal() as db: assert db.get(Conversation, 'move_chat').project_id == scope['project_id']


def test_late_edits_after_interruption_are_never_overwritten(setup, monkeypatch):
    root, scope, tree = setup; dirty(root)
    row = prepare(tree)
    def crash(record, event):
        if event == 'after_write': raise RuntimeError('stop')
    monkeypatch.setattr(transfers, '_checkpoint', crash)
    assert decide(row)['status'] == 'interrupted'
    (Path(tree['path']) / 'a.txt').write_bytes(b'new user edit')
    monkeypatch.setattr(transfers, '_checkpoint', lambda *args: None)
    for action in ('apply', 'rollback'):
        result = decide(row, action)
        assert result['status'] == 'interrupted' and '无法自动辨认' in result['error']
    assert (Path(tree['path']) / 'a.txt').read_bytes() == b'new user edit'
    assert (root / 'a.txt').read_bytes() == b'unstaged\r\n\x00\xfe'


@pytest.mark.parametrize('conflict', ['target-dirty', 'diverged', 'ignored-collision'])
def test_conflicts_are_reviewable_without_rebinding(setup, conflict):
    root, scope, tree = setup; target = Path(tree['path'])
    if conflict == 'ignored-collision':
        (root / '.git/info/exclude').write_text('cache.bin\n')
        (root / 'cache.bin').write_bytes(b'staged tracked')
        git(root, 'add', '-f', 'cache.bin')
        (target / 'cache.bin').write_bytes(b'target cache')
    else:
        dirty(root)
        if conflict == 'target-dirty': (target / 'a.txt').write_text('target data')
        if conflict == 'diverged': git(target, 'commit', '--allow-empty', '-m', 'different')
    row = prepare(tree)
    assert row['review']['blockers']
    with pytest.raises(HTTPException): decide(row)
    assert decide(row, 'cancel')['status'] == 'cancelled'


@pytest.mark.parametrize('activity', ['foreground', 'background', 'terminal'])
def test_other_conversations_and_overlapping_projects_block_transfer(setup, activity):
    root, scope, tree = setup; dirty(root)
    with SessionLocal() as db:
        parent = workspace.register(db, str(root.parent), '父目录项目')
        db.add(Conversation(id='overlap_chat', project_id=parent.id)); db.commit()
        pid = parent.id
    try:
        if activity == 'foreground': assert run_store.begin('chat_overlap_transfer', 'overlap_chat')
        if activity == 'background':
            with SessionLocal() as db:
                db.add(TaskRecord(id='pending_transfer', type='agent', title='pending', status='pending', conversation_id='overlap_chat')); db.commit()
        if activity == 'terminal': terminal_sessions._sessions['transfer_fake'] = SimpleNamespace(project_id=pid, worker=SimpleNamespace(is_alive=lambda: True))
        with pytest.raises(HTTPException): prepare(tree)
    finally:
        if activity == 'foreground': run_store.finish('chat_overlap_transfer', 'cancelled')
        terminal_sessions._sessions.pop('transfer_fake', None)


def test_staged_delete_rename_and_unstaged_delete(setup):
    root, scope, tree = setup; target = Path(tree['path'])
    git(root, 'mv', 'a.txt', 'renamed.txt')
    (root / 'renamed.txt').unlink()
    row = prepare(tree); result = decide(row)
    assert result['status'] == 'applied', result
    assert git(root, 'status', '--porcelain') == ''
    assert 'renamed.txt' in git(target, 'ls-files')
    assert not (target / 'renamed.txt').exists() and not (target / 'a.txt').exists()


def test_real_process_exit_reclaims_only_its_own_git_locks(setup):
    import os
    import subprocess
    import sys
    from app import worktrees
    root, scope, tree = setup; dirty(root)
    row = prepare(tree)
    code = f'''from pathlib import Path
import os
from app import workspace_transfers as t, worktrees
worktrees.DATA_DIR = Path({str(worktrees.DATA_DIR)!r})
def crash(row, phase):
    if phase == 'after_write': os._exit(91)
t._checkpoint = crash
t.decide('move_chat', {row['id']!r}, 'apply')
'''
    process = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], env=os.environ.copy(),
        creationflags=0x08000000, timeout=60, capture_output=True)
    assert process.returncode == 91, process.stderr.decode(errors='replace')
    assert (root / '.git/index.lock').exists()
    transfers.recover_interrupted()
    result = decide(row)
    assert result['status'] == 'applied', result
    assert not (root / '.git/index.lock').exists()
    assert git(root, 'status', '--porcelain') == ''


def test_ignored_files_stay_local_and_git_gc_retains_staged_backup(setup, monkeypatch):
    root, scope, tree = setup; target = Path(tree['path'])
    (root / '.git/info/exclude').write_text('cache.bin\n')
    (root / 'cache.bin').write_bytes(b'source cache')
    (target / 'cache.bin').write_bytes(b'target cache')
    dirty(root); before = git(root, 'ls-files', '--stage')
    row = prepare(tree)
    monkeypatch.setattr(transfers, '_checkpoint', lambda r, p: (_ for _ in ()).throw(RuntimeError('stop')) if p == 'before_bind' else None)
    assert decide(row)['status'] == 'interrupted'
    git(root, 'gc', '--prune=now')
    monkeypatch.setattr(transfers, '_checkpoint', lambda *args: None)
    result = decide(row, 'rollback')
    assert result['status'] == 'rolled_back', result
    assert git(root, 'ls-files', '--stage') == before
    assert (root / 'cache.bin').read_bytes() == b'source cache'
    assert (target / 'cache.bin').read_bytes() == b'target cache'


def test_target_edit_at_source_cleanup_keeps_source_original(setup, monkeypatch):
    root, scope, tree = setup; dirty(root)
    row = prepare(tree)
    def late_edit(record, phase):
        if phase == 'before_write' and record.journal['phase'] == 'source_file':
            (Path(tree['path']) / 'a.txt').write_bytes(b'late target edit')
    monkeypatch.setattr(transfers, '_checkpoint', late_edit)
    result = decide(row)
    assert result['status'] == 'interrupted', result
    assert (root / 'a.txt').read_bytes() == b'unstaged\r\n\x00\xfe'
    assert (Path(tree['path']) / 'a.txt').read_bytes() == b'late target edit'


def test_foreign_lock_cannot_be_removed_by_recovery(setup, monkeypatch):
    root, scope, tree = setup; dirty(root)
    row = prepare(tree)
    monkeypatch.setattr(transfers, '_checkpoint', lambda r, p: (_ for _ in ()).throw(RuntimeError('stop')) if p == 'backed_up' else None)
    assert decide(row)['status'] == 'interrupted'
    lock = root / '.git/index.lock'; lock.write_bytes(b'foreign lock')
    monkeypatch.setattr(transfers, '_checkpoint', lambda *args: None)
    assert decide(row)['status'] == 'interrupted'
    assert lock.read_bytes() == b'foreign lock'
    lock.unlink()
    assert decide(row, 'rollback')['status'] == 'rolled_back'


def test_changed_ignore_rules_with_ignored_files_are_reviewed_as_conflict(setup):
    root, scope, tree = setup
    (root / '.gitignore').write_text('local-cache.bin\n')
    (root / 'local-cache.bin').write_bytes(b'keep my local file')
    row = prepare(tree)
    assert any('忽略规则有改动' in reason for reason in row['review']['blockers'])
    with pytest.raises(HTTPException): decide(row)
    assert (root / 'local-cache.bin').read_bytes() == b'keep my local file'


def test_final_git_state_mismatch_does_not_report_success(setup, monkeypatch):
    root, scope, tree = setup; dirty(root)
    row = prepare(tree)
    def late_file(record, phase):
        if phase == 'before_bind': (root / 'late.txt').write_text('new after cleanup')
    monkeypatch.setattr(transfers, '_checkpoint', late_file)
    result = decide(row)
    assert result['status'] == 'interrupted', result
    assert 'Git 状态与审阅结果不同' in result['error']
    assert (root / 'late.txt').read_text() == 'new after cleanup'
    with SessionLocal() as db: assert db.get(Conversation, 'move_chat').project_id == scope['project_id']


def test_complete_git_state_transfer_hands_branch_to_detached_target(setup):
    root, scope, tree = setup
    target = Path(tree['path'])
    git(target, 'commit', '--allow-empty', '-m', 'Target tip')
    target_head = git(target, 'rev-parse', 'HEAD')
    target_index = git(target, 'ls-files', '--stage')
    source_head = git(root, 'rev-parse', 'HEAD')
    dirty(root)
    source_index = git(root, 'ls-files', '--stage')

    row = prepare_mode(tree, 'git_state')
    assert row['review']['mode'] == 'git_state'
    assert row['review']['blockers'] == []
    result = decide(row)
    assert result['status'] == 'applied', result

    assert git(root, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    assert git(root, 'rev-parse', 'HEAD') == target_head
    assert git(target, 'symbolic-ref', '--quiet', '--short', 'HEAD') == 'main'
    assert git(target, 'rev-parse', 'HEAD') == source_head
    assert (target / 'a.txt').read_bytes() == b'unstaged\r\n\x00\xfe'
    assert (target / '中文.bin').read_bytes() == b'\x00\x80\xffuntracked'
    assert git(target, 'ls-files', '--stage') == source_index
    assert git(root, 'status', '--porcelain') == ''
    assert git(root, 'ls-files', '--stage') == target_index


def test_interrupted_git_state_transfer_can_be_rolled_back(setup, monkeypatch):
    root, scope, tree = setup
    target = Path(tree['path'])
    git(target, 'commit', '--allow-empty', '-m', 'Target tip')
    target_head = git(target, 'rev-parse', 'HEAD')
    source_head = git(root, 'rev-parse', 'HEAD')
    dirty(root)
    row = prepare_mode(tree, 'git_state')
    monkeypatch.setattr(transfers, '_checkpoint', lambda record, phase: (_ for _ in ()).throw(RuntimeError('stop')) if phase == 'before_bind' else None)
    assert decide(row)['status'] == 'interrupted'
    monkeypatch.setattr(transfers, '_checkpoint', lambda *args: None)
    result = decide(row, 'rollback')
    assert result['status'] == 'rolled_back', result.get('error') or result
    assert git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD') == 'main'
    assert git(root, 'rev-parse', 'HEAD') == source_head
    assert git(target, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    assert git(target, 'rev-parse', 'HEAD') == target_head
    assert (root / 'a.txt').read_bytes() == b'unstaged\r\n\x00\xfe'
    assert (root / '中文.bin').read_bytes() == b'\x00\x80\xffuntracked'
    assert git(target, 'status', '--porcelain') == ''
