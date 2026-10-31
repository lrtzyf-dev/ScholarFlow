"""Reviewed transfer of uncommitted or complete Git state between worktrees.

No checkout/reset/clean command is run against either user's worktree. Each file
and index mutation has durable before/after bytes and a write-ahead cursor. After
interruption only exact before or after states are accepted; unfamiliar bytes
are left untouched. The explicit ``git_state`` mode hands a source branch to a
clean detached receiver without silently merging divergent histories.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime
import hashlib
import os
from pathlib import Path
import threading
from uuid import uuid4
import zipfile

from fastapi import HTTPException
from sqlalchemy import text
from typing import Literal

from . import (workspace, workspace_write, workspace_switches as switches, worktrees,
              worktree_archive as archive, project_git as git, git_operations,
              project_changes, run_store, terminal_sessions)
from .db import SessionLocal
from .models import (Conversation, DesktopState, Project, TaskRecord, WorkspaceSwitch,
                     WorkspaceTransfer)

class Prepare(switches.Prepare):
    mode: Literal['changes', 'git_state'] = 'changes'
_blocked = {}
_local = threading.local()
_active = threading.Event()
PENDING = ('applying', 'interrupted', 'rolling_back')


def overlap(a, b):
    a, b = Path(a), Path(b)
    return a.is_relative_to(b) or b.is_relative_to(a)


def require_available(path):
    for id, roots in list(_blocked.items()):
        if id != getattr(_local, 'allowed', None) and any(overlap(path, root) for root in roots):
            workspace.fail('此目录有未完成的 Git 改动迁移，请在工作树页继续或撤回迁移', 409)


def require_conversation_available(db, cid):
    if db.query(WorkspaceTransfer).filter(WorkspaceTransfer.conversation_id == cid,
            WorkspaceTransfer.status.in_(PENDING), WorkspaceTransfer.id != (getattr(_local, 'allowed', None) or '')).first():
        workspace.fail('此会话有未完成的 Git 改动迁移，请先继续或撤回', 409)


@contextmanager
def _coordinated():
    # ProjectChange already takes its guard before run_store; keep that order.
    with worktrees._guard, git_operations._guard, project_changes._guard, run_store.coordinated(), terminal_sessions._guard:
        yield


def _get(db, cid, id):
    row = db.get(WorkspaceTransfer, id)
    if not row or row.conversation_id != cid:
        workspace.fail('Git 改动迁移记录不存在', 404)
    return row


def describe(row):
    result = {key: getattr(row, key) for key in ('id', 'conversation_id', 'source_project_id',
        'target_project_id', 'status', 'error', 'created_at', 'completed_at')}
    snap = row.snapshot
    result['review'] = {k: snap[k] for k in ('source', 'target', 'changes', 'blockers', 'affected', 'relation') if k in snap}
    result['review'].update(mode=snap.get('mode', 'changes'), files=snap.get('transfer_files', len(snap.get('paths', []))),
        source_branch=snap.get('source_branch'), target_branch=snap.get('target_branch'))
    result['progress'] = {k: row.journal.get(k) for k in ('cursor', 'total', 'phase')}
    result['backup_available'] = bool(row.journal.get('backup_sha256'))
    return result


def _affected(db, roots):
    projects = [p for p in db.query(Project).all() if any(overlap(p.root_path, root) for root in roots)]
    ids = {p.id for p in projects}
    convs = db.query(Conversation).filter(Conversation.project_id.in_(ids)).all()
    return projects, convs


def _idle(db, roots):
    from .api.conversations import _require_idle
    from .task_runner import running_task_ids
    projects, convs = _affected(db, roots)
    for conv in convs:
        _require_idle(db, conv.id, '迁移 Git 改动')
    for task in db.query(TaskRecord).filter((TaskRecord.status.in_(['pending', 'running'])) |
            (TaskRecord.id.in_(running_task_ids()))).all():
        scope = (task.checkpoint or {}).get('workspace') or (task.payload or {}).get('workspace') or {}
        if scope.get('root_path') and any(overlap(scope['root_path'], r) for r in roots):
            workspace.fail('受影响目录仍有后台任务，请先停止并等待结束', 409)
    ids = {p.id for p in projects}
    if any(s.project_id in ids and s.worker.is_alive() for s in terminal_sessions._sessions.values()):
        workspace.fail('受影响目录仍有终端进程，请先结束终端', 409)
    return [{'id': c.id, 'title': c.title, 'project_id': c.project_id,
             'workspace_revision': c.workspace_revision or 0} for c in convs]


def _state(root, *, reserved=False):
    state = archive.inspect(root, worktrees._matching(root, root),
        ignore_locks=('index.lock', 'HEAD.lock') if reserved else ())
    directory = archive.git_dir(root)
    index = directory / 'index'
    if not index.is_file() or index.is_symlink() or index.is_junction():
        workspace.fail('索引文件不可用，请先在外部 Git 工具处理', 409)
    if git._text(git._git(root, 'rev-parse', '--shared-index-path')).strip():
        workspace.fail('迁移暂不支持拆分索引，请先在外部 Git 工具转为普通索引', 409)
    modes = git._git(root, 'ls-files', '--stage', '-z').split(b'\0')
    if any(v and v.split(b' ', 1)[0] not in (b'100644', b'100755') for v in modes):
        workspace.fail('迁移只支持普通文件，暂不支持符号链接或子模块', 409)
    state['index_sha256'] = archive.digest(index.read_bytes())
    state['effective_config'] = archive.digest(git._git(root, 'config', '--null', '--list'))
    state['git_dir'] = str(directory)
    state['git_identity'] = workspace.identity(directory)
    return state


def _inspect(db, conv, target, stack, mode='changes'):
    info = switches._inspect(db, conv, target, stack)
    roots = [Path(info[k]['root_path']) for k in ('source', 'target')]
    affected = _idle(db, roots)
    states = {k: _state(root) for k, root in zip(('source', 'target'), roots)}
    entries = git._entries(git._git(roots[0], 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
    changes = sorted({p for e in entries for p in (e['path'], e.get('original_path')) if p})
    blockers = []
    backup_root = Path(worktrees.DATA_DIR).resolve() / 'workspace-transfers'
    if any(overlap(backup_root, root) for root in roots):
        blockers.append('迁移备份目录与项目重叠，请使用独立于应用数据目录的项目。')
    left, right = states['source'], states['target']
    relation = 'same'
    if left['head'] != right['head']:
        counts = git._text(git._git(roots[0], 'rev-list', '--left-right', '--count', left['head'] + '...' + right['head'])).strip().split()
        relation = f'来源独有 {counts[0]} 个提交，目标独有 {counts[1]} 个提交'
        if mode == 'changes':
            blockers.append('双方提交不同：' + relation + '。改动迁移要求双方使用同一提交。')
    if mode == 'git_state':
        if not left.get('branch'):
            blockers.append('来源当前没有分支，无法携带分支和未推送提交；可使用普通改动迁移。')
        if right.get('branch'):
            blockers.append('目标已经签出分支，完整 Git 状态迁移要求目标为分离 HEAD。')
        if left.get('branch') and not right.get('branch'):
            relation = f'携带来源分支 {left["branch"].removeprefix("refs/heads/")}（{left["head"][:12]}）'
    if info['target']['changes']:
        target_entries = git._entries(git._git(roots[1], 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
        blockers.append('目标已有 Git 改动：' + '、'.join(e['path'] for e in target_entries[:30]))
    if left['effective_config'] != right['effective_config']:
        blockers.append('双方 Git 配置不同，请先核对换行、过滤器和工作树配置。')
    if not changes and mode == 'changes':
        blockers.append('来源没有待迁移改动，可使用“切换当前会话”。')
    if len(changes) > 1000 and mode == 'changes':
        blockers.append('单次迁移最多 1,000 个改动路径。')
    ignored = set(left['ignored']) | set(right['ignored'])
    if ignored and any(Path(p).name == '.gitignore' for p in changes):
        blockers.append('忽略规则有改动且双方存在被忽略文件，请先单独处理 .gitignore，避免清理来源后忽略范围变化。')
    files = {k: {f['path']: f for f in s['files']} for k, s in states.items()}
    if mode == 'git_state' and not blockers:
        source_files = {f['path']: f['sha256'] for f in left['files'] if f['path'] not in left['ignored']}
        target_files = {f['path']: f['sha256'] for f in right['files'] if f['path'] not in right['ignored']}
        changes = sorted(set(source_files) | set(target_files))
        changes = [rel for rel in changes if source_files.get(rel) != target_files.get(rel)]
    for rel in changes:
        archive._relative(rel)
        if rel in ignored:
            blockers.append('待迁移路径与被忽略文件重名：' + rel)
        if any(p != rel and (p.casefold() == rel.casefold() or p.startswith(rel + '/') or rel.startswith(p + '/'))
                for side in files.values() for p in side):
            blockers.append('文件与目录或大小写路径冲突：' + rel)
    info.update(states=states, changes=entries, paths=changes, affected=affected,
                blockers=list(dict.fromkeys(blockers)), relation=relation, mode=mode,
                source_branch=left.get('branch'), target_branch=right.get('branch'), transfer_files=len(changes))
    return info


def prepare(cid, request):
    with _coordinated(), SessionLocal() as db, ExitStack() as stack:
        db.execute(text('BEGIN IMMEDIATE'))
        old = db.query(WorkspaceTransfer).filter_by(request_id=request.request_id).first()
        if old:
            if (old.conversation_id, old.target_project_id, old.workspace_revision, old.snapshot['draft_revision']) != (
                    cid, request.target_project_id, request.workspace_revision, request.draft_revision):
                workspace.fail('请求编号已用于其他迁移', 409)
            return describe(old)
        conv = switches._conversation(db, cid)
        workspace.check_revision(db, cid, request.workspace_revision)
        draft = switches._draft(db, cid)
        if (draft.revision if draft else 0) != request.draft_revision:
            workspace.fail('草稿已变化，请重新载入并审阅', 409)
        snapshot = _inspect(db, conv, request.target_project_id, stack, request.mode)
        snapshot['draft_revision'] = request.draft_revision
        row = WorkspaceTransfer(conversation_id=cid, request_id=request.request_id,
            source_project_id=conv.project_id, target_project_id=request.target_project_id,
            workspace_revision=conv.workspace_revision or 0, snapshot=snapshot)
        db.add(row); db.commit()
        return describe(row)


def _folder(row, *, create=False):
    base = Path(worktrees.DATA_DIR).resolve(strict=True)
    folder = base / 'workspace-transfers' / row.id
    if create:
        folder.mkdir(parents=True, exist_ok=False)
    if folder.resolve(strict=True) != folder or any(p.is_symlink() or p.is_junction() for p in (folder.parent, folder)):
        workspace.fail('迁移备份目录已变化', 409)
    if row.journal.get('folder_identity') and workspace.identity(folder) != row.journal['folder_identity']:
        workspace.fail('迁移备份目录已被替换', 409)
    return folder


def backup_path(row):
    path = _folder(row) / 'backup.zip'
    if path.is_symlink() or path.is_junction() or path.resolve(strict=True) != path:
        workspace.fail('迁移备份路径已变化', 409)
    with path.open('rb') as file:
        if hashlib.file_digest(file, 'sha256').hexdigest() != row.journal.get('backup_sha256'):
            workspace.fail('迁移备份校验失败，未写入文件', 409)
    return path


def _bytes(root, rel):
    if not (root / rel).exists():
        return None
    with ExitStack() as stack:
        file = archive._locked(root, rel, stack)
        if os.fstat(file.fileno()).st_nlink != 1:
            workspace.fail('迁移暂不支持多硬链接文件：' + rel, 409)
        return file.read(archive.MAX_BYTES + 1)


def _save(db, row, **changes):
    row.journal = {**row.journal, **changes}
    db.commit()


def _make_backup(db, row):
    folder = _folder(row, create=True)
    roots = {k: Path(row.snapshot[k]['root_path']) for k in ('source', 'target')}
    states = row.snapshot['states']
    operations = []
    with (folder / 'backup.zip').open('xb') as file:
        with zipfile.ZipFile(file, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as z:
            z.writestr('review.json', archive.encoded(row.snapshot))
            for side in ('target', 'source'):
                other = 'source' if side == 'target' else 'target'
                before_files = {f['path']: f for f in states[side]['files']}
                after_files = {f['path']: f for f in states[other]['files']}
                for rel in row.snapshot['paths']:
                    before, after = _bytes(roots[side], rel), _bytes(roots[other], rel)
                    for data, expected in ((before, before_files.get(rel)), (after, after_files.get(rel))):
                        if (archive.digest(data) if data is not None else None) != (expected['sha256'] if expected else None):
                            workspace.fail('备份期间文件已变化，请重新审阅：' + rel, 409)
                    if before == after:
                        continue
                    op = {'side': side, 'path': rel, 'kind': 'file'}
                    for label, data in (('before', before), ('after', after)):
                        op[label] = archive.digest(data) if data is not None else None
                        if data is not None:
                            z.writestr(f'{len(operations)}/{label}', data)
                    operations.append(op)
                before = (Path(states[side]['git_dir']) / 'index').read_bytes()
                if archive.digest(before) != states[side]['index_sha256']:
                    workspace.fail('备份期间暂存区已变化，请重新审阅', 409)
                # Fresh index has no source filesystem stat cache. Compare logical
                # entries to reject intent-to-add instead of silently staging it.
                index = folder / (side + '-index')
                original = folder / (side + '-tree-index')
                original.write_bytes((Path(states[other]['git_dir']) / 'index').read_bytes())
                tree = archive.command(roots[other], 'write-tree', index=original).decode().strip()
                # Keep staged blobs reachable even while both real indexes are
                # being reconciled, and across an external Git garbage collect.
                commit = archive.command(roots[other], 'commit-tree', tree, '-p', states[other]['head'],
                    input=b'ScholarFlow transfer index backup\n').decode().strip()
                archive.command(roots[other], 'update-ref', 'refs/scholarflow/workspace-transfers/' + row.id + '/' + other, commit)
                archive.command(roots[side], 'read-tree', tree, index=index)
                if archive.command(roots[side], 'ls-files', '--stage', '-z', index=index) != git._git(roots[other], 'ls-files', '--stage', '-z'):
                    workspace.fail('迁移暂不支持意向添加或特殊索引，请先正常暂存或取消意向添加', 409)
                after = index.read_bytes()
                operations.append({'side': side, 'path': 'index', 'kind': 'index',
                    'before': archive.digest(before), 'after': archive.digest(after)})
                z.writestr(f'{len(operations)-1}/before', before)
                z.writestr(f'{len(operations)-1}/after', after)
            z.writestr('operations.json', archive.encoded(operations))
        file.flush(); os.fsync(file.fileno())
    with (folder / 'backup.zip').open('rb') as file:
        sha = hashlib.file_digest(file, 'sha256').hexdigest()
    _save(db, row, folder_identity=workspace.identity(folder), backup_sha256=sha,
          operations=operations, cursor=0, total=len(operations), phase='backed_up', lock_token=uuid4().hex)


@contextmanager
def _reservations(row):
    """Reserve Git's own locks, including symbolic branch refs, across both sides.

    Crash-left locks are reclaimed only when their full random token matches the
    durable journal. Empty/foreign lock files require external investigation.
    """
    token = ('ScholarFlow transfer ' + row.id + ' ' + row.journal['lock_token']).encode()
    paths = set()
    for state in row.snapshot['states'].values():
        directory = Path(state['git_dir'])
        paths.add(directory / 'index.lock')
        if row.snapshot.get('mode') != 'git_state':
            paths.add(directory / 'HEAD.lock')
        if row.snapshot.get('mode') == 'git_state' and state['branch']:
            paths.add(Path(row.snapshot['repository']['path']) / 'refs' / 'heads' /
                (state['branch'].removeprefix('refs/heads/') + '.lock'))
    with ExitStack() as stack:
        acquired = []
        for path in sorted(paths):
            common = Path(row.snapshot['repository']['path'])
            directory = next((Path(s['git_dir']) for s in row.snapshot['states'].values() if path.parent == Path(s['git_dir'])), common)
            rel = path.relative_to(directory).as_posix()
            if path.exists():
                with ExitStack() as previous:
                    file = archive._locked(directory, rel, previous, delete=True)
                    if os.fstat(file.fileno()).st_nlink != 1 or file.read(len(token) + 1) != token:
                        workspace.fail('Git 锁文件已被其他操作占用：' + str(path), 409)
                    workspace_write.delete_content(file)
            file = archive._locked(directory, rel, stack, create=True, delete=True)
            workspace_write.write_content(file, token)
            acquired.append(file)
        try:
            yield
        finally:
            for file in reversed(acquired):
                workspace_write.delete_content(file)


def _validate_identity(row, stack):
    for side in ('source', 'target'):
        scope = row.snapshot[side]
        root = workspace.root_path(scope)
        state = row.snapshot['states'][side]
        directory = archive.git_dir(root)
        if str(directory) != state['git_dir'] or workspace.identity(directory) != state['git_identity']:
            workspace.fail('Git 元数据目录已变化', 409)
        for path in (root, directory, Path(row.snapshot['repository']['path'])):
            handle = stack.enter_context(workspace_write._handle(path, directory=True))
            if workspace.handle_path(handle, path) != path:
                workspace.fail('迁移目录身份已变化', 409)
        if (root / '.git').is_file():
            # Git's indirection file must keep pointing to the pinned metadata.
            marker = stack.enter_context(workspace_write._handle(root / '.git'))
            if workspace.handle_path(marker, root / '.git') != root / '.git':
                workspace.fail('工作树 Git 指向文件已变化', 409)
        entry = worktrees._matching(root, root)
        if not entry or any(k in entry for k in ('locked', 'prunable', 'bare')):
            workspace.fail('提交、分支或工作树登记已变化，未继续迁移', 409)
        if row.snapshot.get('mode') == 'git_state':
            source, target = row.snapshot['states']['source'], row.snapshot['states']['target']
            allowed = ({'source': {(source.get('branch'), source['head']), (None, target['head'])},
                        'target': {(target.get('branch'), target['head']), (source.get('branch'), source['head'])}})
            expected = allowed[side]
            if (entry.get('branch'), entry.get('HEAD')) not in expected:
                workspace.fail('提交、分支或工作树登记已变化，未继续迁移', 409)
        elif entry.get('HEAD') != state['head'] or entry.get('branch') != state['branch']:
            workspace.fail('提交、分支或工作树登记已变化，未继续迁移', 409)
        if archive.digest(git._git(root, 'config', '--null', '--list', '--show-origin')) != state['config']:
            workspace.fail('Git 配置已变化，未继续迁移', 409)
        if workspace.identity(Path(row.snapshot['repository']['path'])) != row.snapshot['repository']['identity']:
            workspace.fail('仓库目录已变化', 409)
        # Detect merge/rebase and special indexes even while our locks exist.
        archive.check_index(root, ignore_locks=('index.lock', 'HEAD.lock'))


def _current(row, op):
    if op['kind'] == 'index':
        target = Path(row.snapshot['states'][op['side']]['git_dir']) / 'index'
        if target.is_symlink() or target.is_junction():
            workspace.fail('索引路径已变化', 409)
        return archive.digest(target.read_bytes())
    data = _bytes(Path(row.snapshot[op['side']]['root_path']), op['path'])
    return archive.digest(data) if data is not None else None


def _check_refs(row):
    for side, state in row.snapshot['states'].items():
        root = Path(row.snapshot[side]['root_path'])
        entry = worktrees._matching(root, root)
        if not entry or entry.get('HEAD') != state['head'] or entry.get('branch') != state['branch']:
            workspace.fail('迁移期间提交或分支变化，请核对备份', 409)
        if archive.digest(git._git(root, 'config', '--null', '--list', '--show-origin')) != state['config']:
            workspace.fail('迁移期间 Git 配置变化，请核对备份', 409)


def _check_outcome(row, rollback):
    for side in ('source', 'target'):
        root = Path(row.snapshot[side]['root_path'])
        actual = git._entries(git._git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
        expected = row.snapshot['changes'] if side == ('source' if rollback else 'target') else []
        if actual != expected:
            workspace.fail('文件已写入，但 ' + ('来源' if side == 'source' else '目标') +
                ' Git 状态与审阅结果不同，请核对忽略规则、属性或后来修改后继续或撤回', 409)


def _validate_files(row, expected):
    for side in ('source', 'target'):
        files = {f['path']: f['sha256'] for f in row.snapshot['states'][side]['files']}
        for i, op in enumerate(row.journal['operations']):
            if op['side'] == side and op['kind'] == 'file':
                value = op[expected[i]]
                if value is None:
                    files.pop(op['path'], None)
                else:
                    files[op['path']] = value
        actual = {f['path']: f['sha256'] for f in archive.scan(Path(row.snapshot[side]['root_path']))}
        if files != actual:
            changed = sorted(p for p in files.keys() | actual.keys() if files.get(p) != actual.get(p))
            workspace.fail('迁移现场出现后来修改，已保留，请先核对备份：' + '、'.join(changed[:20]), 409)


def _write(row, op, content, before):
    root = Path(row.snapshot[op['side']]['root_path'])
    if op['kind'] == 'index':
        root = Path(row.snapshot['states'][op['side']]['git_dir'])
    with ExitStack() as stack:
        file = archive._locked(root, op['path'], stack, create=before is None, delete=content is None)
        if os.fstat(file.fileno()).st_nlink != 1:
            workspace.fail('拒绝修改多硬链接文件', 409)
        if before is not None and hashlib.file_digest(file, 'sha256').hexdigest() != before:
            workspace.fail('写入前文件已变化：' + op['path'], 409)
        if content is None:
            workspace_write.delete_content(file)
        else:
            workspace_write.write_content(file, content)


def _checkpoint(row, phase):
    """Fault-injection boundary: called after durable intent or mutation."""


def _execute(db, row, rollback):
    operations = row.journal['operations']
    desired = 'before' if rollback else 'after'
    states = []
    for op in operations:
        current = _current(row, op)
        if current == op[desired]:
            states.append(desired)
        elif current == op['after' if rollback else 'before']:
            states.append('after' if rollback else 'before')
        else:
            workspace.fail('文件或索引含无法自动辨认的内容，保留现场与备份：' + op['side'] + '/' + op['path'], 409)
    _validate_files(row, states)
    with workspace_write._handle(backup_path(row)) as saved, zipfile.ZipFile(saved) as z:
        saved.seek(0)
        if hashlib.file_digest(saved, 'sha256').hexdigest() != row.journal['backup_sha256']:
            workspace.fail('迁移备份已变化', 409)
        sequence = reversed(range(len(operations))) if rollback else range(len(operations))
        previous_side = None
        for i in sequence:
            op = operations[i]
            if states[i] == desired:
                continue
            # Scan each whole side at phase boundaries, not for every file (which
            # would rehash up to 512 MiB a thousand times). Individual writes and
            # the counterpart copy are pinned and checked below.
            if op['side'] != previous_side:
                _validate_files(row, states)
                previous_side = op['side']
            _save(db, row, cursor=i, phase=('rollback_' if rollback else '') + op['side'] + '_' + op['kind'])
            _checkpoint(row, 'before_write')
            content = z.read(f'{i}/{desired}') if op[desired] is not None else None
            if content is not None and archive.digest(content) != op[desired]:
                workspace.fail('备份条目校验失败', 409)
            with ExitStack() as protected:
                cleanup_side = 'target' if rollback else 'source'
                if op['side'] == cleanup_side:
                    other = 'source' if rollback else 'target'
                    peer = next((p for p in operations if p['side'] == other and p['kind'] == op['kind'] and p['path'] == op['path']), None)
                    if peer and peer[desired] is not None:
                        directory = Path(row.snapshot['states'][other]['git_dir'] if op['kind'] == 'index' else row.snapshot[other]['root_path'])
                        copy = archive._locked(directory, op['path'], protected)
                        if hashlib.file_digest(copy, 'sha256').hexdigest() != peer[desired]:
                            workspace.fail('另一侧文件已被修改，停止清理并保留原内容：' + op['path'], 409)
                _write(row, op, content, op[states[i]])
            _checkpoint(row, 'after_write')
            states[i] = desired
            _save(db, row, cursor=i + 1)
    _validate_files(row, states)
    if any(_current(row, op) != op[desired] for op in operations):
        workspace.fail('迁移后暂存区或文件校验不一致，请核对备份', 409)


def _entry(row, side):
    root = Path(row.snapshot[side]['root_path'])
    value = worktrees._matching(root, root)
    if not value:
        workspace.fail('Git 工作树登记已变化，请核对迁移现场', 409)
    return value


def _branch_name(value):
    if not value:
        return ''
    return value.removeprefix('refs/heads/')


def _switch(row, side, *, branch=None, head=None):
    root = Path(row.snapshot[side]['root_path'])
    if branch:
        # Change only the linked worktree's HEAD. Files and the index are
        # journaled separately; checkout/reset here would destroy the review
        # boundary before the durable file phase starts.
        archive.command(root, 'symbolic-ref', 'HEAD', 'refs/heads/' + branch)
    else:
        archive.command(root, 'update-ref', '--no-deref', 'HEAD', head)


def _git_state_execute(db, row, rollback):
    """Move a checked-out branch to a clean detached worktree without merging.

    The source is first detached at the target tip, freeing the source branch;
    the target then checks out that branch. The source/target working bytes and
    indexes are applied from the durable ZIP afterwards. A restart can observe
    either side of each switch and continue from that known state.
    """
    source, target = row.snapshot['states']['source'], row.snapshot['states']['target']
    source_branch, source_head = _branch_name(source['branch']), source['head']
    target_head = target['head']
    if not source_branch or target.get('branch'):
        workspace.fail('完整 Git 状态迁移要求来源有分支且目标为分离 HEAD', 409)
    operations = row.journal['operations']
    desired = 'before' if rollback else 'after'
    # Each operation may already be on either side of the durable boundary
    # after a process interruption. Accept only its exact before/after digest;
    # never infer a third state or overwrite a later edit.
    states = []
    for op in operations:
        current = _current(row, op)
        if current == op['before']:
            states.append('before')
        elif current == op['after']:
            states.append('after')
        else:
            workspace.fail('完整 Git 状态迁移现场无法辨认，保留备份：' + op['side'] + '/' + op['path'], 409)
    _validate_files(row, states)
    source_entry, target_entry = _entry(row, 'source'), _entry(row, 'target')
    source_detached = not source_entry.get('branch') and source_entry.get('HEAD') == target_head
    target_on_branch = _branch_name(target_entry.get('branch')) == source_branch and target_entry.get('HEAD') == source_head
    if rollback:
        # Return the branch to its original checkout and detach the receiver at
        # the original target tip before restoring both byte/index snapshots.
        if target_entry.get('branch'):
            if _branch_name(target_entry.get('branch')) != source_branch:
                workspace.fail('撤回时目标分支已被外部修改，请核对备份', 409)
            _switch(row, 'target', head=target_head)
            _save(db, row, phase='git_state_target_detached')
        if not _entry(row, 'source').get('branch'):
            _switch(row, 'source', branch=source_branch)
            _save(db, row, phase='git_state_source_branch')
    else:
        if not source_detached:
            if source_entry.get('branch') and _branch_name(source_entry.get('branch')) != source_branch:
                workspace.fail('来源分支已变化，请重新审阅', 409)
            _switch(row, 'source', head=target_head)
            _save(db, row, phase='git_state_source_detached')
        target_entry = _entry(row, 'target')
        if not target_on_branch:
            if target_entry.get('branch'):
                workspace.fail('目标已签出其他分支，请重新审阅', 409)
            _switch(row, 'target', branch=source_branch)
            _save(db, row, phase='git_state_target_branch')
    with workspace_write._handle(backup_path(row)) as saved, zipfile.ZipFile(saved) as z:
        saved.seek(0)
        if hashlib.file_digest(saved, 'sha256').hexdigest() != row.journal['backup_sha256']:
            workspace.fail('迁移备份已变化', 409)
        # Ref handoff changes the expected tree in both checkouts. The source
        # becomes the former target tip; the target receives the former source
        # branch. Restore both sides on rollback as well.
        sides = ('source', 'target')
        for i, op in enumerate(operations):
            if op['side'] not in sides or op['kind'] != 'file' and op['kind'] != 'index':
                continue
            current = _current(row, op)
            if current == op[desired]:
                continue
            if current != op['before' if not rollback else 'after'] and current != op[desired]:
                workspace.fail('完整 Git 状态迁移现场无法辨认，保留备份：' + op['side'] + '/' + op['path'], 409)
            _save(db, row, cursor=i, phase=('rollback_' if rollback else '') + 'git_state_' + op['side'] + '_' + op['kind'])
            _checkpoint(row, 'before_write')
            content = z.read(f'{i}/{desired}') if op[desired] is not None else None
            if content is not None and archive.digest(content) != op[desired]:
                workspace.fail('备份条目校验失败', 409)
            _write(row, op, content, current)
            _checkpoint(row, 'after_write')
            _save(db, row, cursor=i + 1)
    final_states = [desired] * len(operations)
    _validate_files(row, final_states)


def _check_git_state_outcome(row, rollback):
    source_entry, target_entry = _entry(row, 'source'), _entry(row, 'target')
    source, target = row.snapshot['states']['source'], row.snapshot['states']['target']
    if rollback:
        if _branch_name(source_entry.get('branch')) != _branch_name(source['branch']) or source_entry.get('HEAD') != source['head']:
            workspace.fail('撤回后的来源分支或提交不符合原审阅', 409)
        if target_entry.get('branch') or target_entry.get('HEAD') != target['head']:
            workspace.fail('撤回后的目标分离提交不符合原审阅', 409)
    else:
        if source_entry.get('branch') or source_entry.get('HEAD') != target['head']:
            workspace.fail('迁移后的来源工作树未分离到原目标提交', 409)
        if _branch_name(target_entry.get('branch')) != _branch_name(source['branch']) or target_entry.get('HEAD') != source['head']:
            workspace.fail('迁移后的目标未接收来源分支', 409)


def _finish(db, row, rollback):
    from .api.desktop_state import ConversationState
    now = datetime.utcnow()
    # Every affected chat loses permission to execute old plans, including chats
    # that stay in their original project. Text/editor drafts retain ownership.
    _, convs = _affected(db, [row.snapshot[k]['root_path'] for k in ('source', 'target')])
    for conv in convs:
        if conv.id == row.conversation_id and not rollback:
            conv.project_id = row.target_project_id
        conv.workspace_revision = (conv.workspace_revision or 0) + 1
        conv.updated_at = now
        draft = switches._draft(db, conv.id)
        value = ConversationState.model_validate(draft.value if draft else {}).model_dump()
        value.update(workspace_revision=conv.workspace_revision, file_paths=[])
        if draft:
            draft.value = value; draft.revision += 1; draft.updated_at = now
        else:
            db.add(DesktopState(key='conversation:' + conv.id, value=value))
        for old in db.query(WorkspaceSwitch).filter_by(conversation_id=conv.id, status='prepared').all():
            old.status = 'superseded'
    row.status = 'rolled_back' if rollback else 'applied'
    row.error = ''; row.completed_at = now
    _save(db, row, phase=row.status, cursor=row.journal['total'])
    _blocked.pop(row.id, None)


def decide(cid, id, action):
    if action not in ('apply', 'rollback', 'cancel'):
        workspace.fail('迁移操作无效')
    with _coordinated(), SessionLocal() as db, ExitStack() as stack:
        row = _get(db, cid, id)
        if row.status in ('applied', 'rolled_back', 'cancelled'):
            return describe(row)
        if action == 'cancel':
            if row.status != 'prepared':
                workspace.fail('迁移已经开始，请使用撤回迁移', 409)
            row.status = 'cancelled'; db.commit()
            return describe(row)
        rollback = action == 'rollback'
        if rollback and not row.journal.get('backup_sha256'):
            workspace.fail('迁移尚未写入，请取消审阅', 409)
        conv = switches._conversation(db, cid)
        workspace.check_revision(db, cid, row.workspace_revision)
        if conv.project_id != row.source_project_id:
            workspace.fail('会话工作目录已变化，请重新审阅', 409)
        if row.status == 'prepared':
            if row.snapshot['blockers']:
                workspace.fail('当前迁移存在冲突，请先处理：' + '；'.join(row.snapshot['blockers']), 409)
            draft = switches._draft(db, cid)
            if (draft.revision if draft else 0) != row.snapshot['draft_revision']:
                workspace.fail('草稿已变化，请重新审阅', 409)
            current = _inspect(db, conv, row.target_project_id, stack, row.snapshot.get('mode', 'changes'))
            if any(current[k] != row.snapshot[k] for k in current):
                workspace.fail('审阅后目录、文件内容、暂存区或会话范围已变化，请重新审阅', 409)
            # Failure here has not modified either checkout; retry requires a new
            # review, while retaining an incomplete backup for investigation.
            try:
                if not row.journal.get('backup_sha256'):
                    _make_backup(db, row)
                else:
                    backup_path(row)
            except Exception:
                row.status = 'cancelled'; db.commit()
                raise
        roots = [row.snapshot[k]['root_path'] for k in ('source', 'target')]
        _local.allowed = row.id
        try:
            _idle(db, roots)
            _blocked[row.id] = roots
            row.status = 'rolling_back' if rollback else 'applying'; row.error = ''; db.commit()
            _active.set()
            _validate_identity(row, stack)
            with _reservations(row):
                if row.snapshot.get('mode') != 'git_state':
                    _check_refs(row)
                _checkpoint(row, 'backed_up')
                if row.snapshot.get('mode') == 'git_state':
                    _git_state_execute(db, row, rollback)
                else:
                    _execute(db, row, rollback)
                _checkpoint(row, 'before_bind')
                if row.snapshot.get('mode') != 'git_state':
                    _check_refs(row)
                if row.snapshot.get('mode') == 'git_state':
                    _check_git_state_outcome(row, rollback)
                else:
                    _check_outcome(row, rollback)
                _finish(db, row, rollback)
        except Exception as exc:
            db.rollback()
            row = _get(db, cid, id)
            if row.status in PENDING:
                row.status = 'interrupted'
                row.error = str(exc.detail if isinstance(exc, HTTPException) else exc)[:4000]
                db.commit()
            else:
                raise
        finally:
            _local.allowed = None
            _active.clear()
        return describe(row)


def listing(cid):
    with SessionLocal() as db:
        conv = db.get(Conversation, cid)
        if not conv:
            workspace.fail('会话不存在', 404)
        # Include incomplete transfers affecting this chat's project, so another
        # window can locate the operation that blocks its workspace.
        rows = db.query(WorkspaceTransfer).filter((WorkspaceTransfer.conversation_id == cid) |
            ((WorkspaceTransfer.status.in_(PENDING)) & ((WorkspaceTransfer.source_project_id == conv.project_id) |
                (WorkspaceTransfer.target_project_id == conv.project_id)))).order_by(WorkspaceTransfer.created_at.desc()).limit(30).all()
        return {'transfers': [describe(row) for row in rows]}


def recover_interrupted():
    _blocked.clear()
    with SessionLocal() as db:
        for row in db.query(WorkspaceTransfer).filter(WorkspaceTransfer.status.in_(PENDING)).all():
            row.status = 'interrupted'
            row.error = '上次迁移期间应用退出。已保留备份，请继续核对或撤回；不会自动重放。'
            _blocked[row.id] = [row.snapshot[k]['root_path'] for k in ('source', 'target')]
        db.commit()


def active():
    return _active.is_set()
