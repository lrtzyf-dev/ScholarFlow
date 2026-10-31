"""Managed worktree lifecycle with durable identities, review and recovery.

Only app-created directories can be removed. A clean checkout is required,
including ignored files, and a private Git ref keeps its HEAD reachable.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi import HTTPException

from . import git_operations, git_process, project_git as git, workspace, workspace_write, run_store, worktree_archive as archive
from .config import DATA_DIR
from .db import SessionLocal
from .models import ManagedWorktree, Project, Conversation, TaskRecord

_guard = threading.RLock()
_active = threading.Event()
_unavailable: set[str] = set()


class Create(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,64}$')
    name: str = Field(min_length=1, max_length=120)
    ref: str = Field(default='HEAD', min_length=1, max_length=256)
    branch: str = Field(default='', max_length=256)

    @field_validator('name', 'ref', 'branch')
    @classmethod
    def text(cls, value):
        if any(ord(c) < 32 for c in value) or value != value.strip():
            raise ValueError('名称不能包含控制字符或首尾空格')
        return value


def require_available(path):
    if any(str(parent) in _unavailable for parent in (path, *path.parents)):
        workspace.fail('工作树正在处理、已移除或需要恢复，请在“工作树”页核对状态', 409)


def _mark(row):
    if row.status == 'ready':
        _unavailable.discard(row.path)
    else:
        _unavailable.add(row.path)


def summary(row):
    return {key: getattr(row, key) for key in ('id', 'source_project_id', 'project_id', 'name', 'base_ref',
        'base_head', 'branch', 'path', 'status', 'revision', 'saved_head', 'error', 'output',
        'removal_snapshot', 'archive_info', 'created_at', 'updated_at')}


def _save(db, row, status=None):
    if status:
        row.status = status
    row.revision += 1; row.updated_at = datetime.utcnow()
    _mark(row); db.commit()


def _get(db, source_id, id):
    row = db.get(ManagedWorktree, id)
    if not row or source_id not in (row.source_project_id, row.project_id):
        workspace.fail('工作树记录不存在', 404)
    return row


def _anchor(db, project_id):
    managed = db.query(ManagedWorktree).filter_by(project_id=project_id).first()
    return managed.source_project_id if managed else project_id


def _common(root):
    value = Path(git._text(git._git(root, 'rev-parse', '--git-common-dir')).strip())
    return (value if value.is_absolute() else root / value).resolve(strict=True)


def _entries(root):
    entries, current = [], {}
    for value in git._git(root, 'worktree', 'list', '--porcelain', '-z').split(b'\0'):
        if not value:
            if current:
                entries.append(current); current = {}
            continue
        key, _, content = git._text(value).partition(' ')
        current[key] = content
    if current:
        entries.append(current)
    return entries


def _matching(root, path):
    for entry in _entries(root):
        if os.path.normcase(os.path.abspath(entry.get('worktree', ''))) == os.path.normcase(str(path)):
            return entry
    return None


def _repo(db, row):
    project = workspace.get_project(db, row.source_project_id)
    scope = workspace.project_scope(project)
    if scope != row.source_scope:
        workspace.fail('来源项目目录或身份已变化，请恢复原项目后重试', 409)
    root = git_operations._repository(scope)
    common = _common(root)
    if {'path': str(common), 'identity': workspace.identity(common)} != row.repository:
        workspace.fail('Git 仓库元数据已变化，拒绝操作工作树', 409)
    return root


def _parent(row):
    base = (Path(DATA_DIR) / 'worktrees').resolve(strict=True)
    parent = base / row.id
    path = parent / 'checkout'
    if (str(path) != row.path or parent.resolve(strict=True) != parent or parent.is_symlink() or parent.is_junction()
            or workspace.identity(parent) != row.parent_identity):
        workspace.fail('工作树管理目录已移动或替换，拒绝文件操作', 409)
    return parent, path


@contextmanager
def _pinned(row, root):
    parent, path = _parent(row)
    with ExitStack() as stack:
        for directory in (parent.parent, parent, root, Path(row.repository['path'])):
            handle = stack.enter_context(workspace_write._handle(directory, directory=True))
            if workspace.handle_path(handle, directory) != directory:
                workspace.fail('操作期间目录已变化', 409)
        _parent(row)
        workspace.root_path(row.source_scope)
        if workspace.identity(Path(row.repository['path'])) != row.repository['identity']:
            workspace.fail('仓库元数据身份已变化', 409)
        yield path


def _run(root, *args):
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_PAGER='cat')
    executable = shutil.which('git')
    if not executable:
        workspace.fail('本机未安装 Git', 503)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        code = git_process.run([executable, '--no-pager', '--literal-pathspecs', '-c', 'core.fsmonitor=false',
            '-C', str(root), *args], input=b'', stdout=output, stderr=errors, env=environment, timeout=120)
        output.seek(0); errors.seek(0)
        result = (output.read(16000) + b'\n' + errors.read(16000)).decode('utf-8', errors='replace').strip()
    if code:
        workspace.fail('Git 操作未成功，请核对工作树后恢复记录：' + result, 409)
    return result


def _validate_checkout(row, root, *, clean=False):
    _, path = _parent(row)
    if (not path.is_dir() or path.resolve(strict=True) != path or path.is_symlink() or path.is_junction()
            or (row.root_identity and workspace.identity(path) != row.root_identity)):
        workspace.fail('工作树目录已丢失、替换或不是普通目录', 409)
    entry = _matching(root, path)
    if not entry or 'locked' in entry or 'prunable' in entry or 'bare' in entry:
        workspace.fail('工作树记录不匹配、被 Git 锁定或需要外部修复', 409)
    if _common(path) != Path(row.repository['path']):
        workspace.fail('目录关联了不同的 Git 仓库', 409)
    # Embedded repositories and submodules need separate preservation rules.
    if any(field.startswith(b'160000 ') for field in git._git(path, 'ls-files', '--stage', '-z').split(b'\0')):
        workspace.fail('含子模块的工作树暂不支持此管理操作', 409)
    if clean:
        if any(field and not field.startswith(b'H ') for field in git._git(path, 'ls-files', '-v', '-z').split(b'\0')):
            workspace.fail('索引含跳过检查或稀疏检出标记，无法确认文件已保存，请先在外部 Git 工具处理', 409)
        if git._git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored'):
            workspace.fail('工作树有未提交、未跟踪或被忽略的文件，请先保存或移出后再移除；不会丢弃这些文件', 409)
    return path, entry


def _ready(db, row, root):
    path, entry = _validate_checkout(row, root)
    if not row.project_id:
        project = workspace.register(db, str(path), row.name)
        row.project_id = project.id
    else:
        project = workspace.get_project(db, row.project_id)
        if project.root_path != str(path):
            workspace.fail('已登记项目与工作树路径不一致', 409)
        # Explicit restoration replaces only this managed checkout's identity.
        project.root_identity = workspace.identity(path)
    row.root_identity = project.root_identity
    row.branch = entry.get('branch', '').removeprefix('refs/heads/')
    row.removal_snapshot = None; row.error = ''
    _save(db, row, 'ready')


def create(project_id, request):
    fingerprint = hashlib.sha256(json.dumps([project_id, request.model_dump(exclude={'request_id'})], sort_keys=True).encode()).hexdigest()
    with _guard, git_operations._guard, SessionLocal() as db:
        anchor_id = _anchor(db, project_id)
        old = db.query(ManagedWorktree).filter_by(request_id=request.request_id).first()
        if old:
            if old.request_hash != fingerprint or old.source_project_id != anchor_id:
                workspace.fail('请求编号已用于其他工作树', 409)
            return summary(old)
        source_scope = workspace.project_scope(workspace.get_project(db, anchor_id))
        root = git_operations._repository(source_scope)
        selected = git_operations._repository(workspace.project_scope(workspace.get_project(db, project_id)))
        if _common(root) != _common(selected):
            workspace.fail('来源项目不属于同一 Git 仓库', 409)
        head = git._text(git._git(selected, 'rev-parse', '--verify', '--end-of-options', request.ref + '^{commit}')).strip()
        if any(field.startswith(b'160000 ') for field in git._git(selected, 'ls-tree', '-r', '-z', head).split(b'\0')):
            workspace.fail('起始提交含子模块，暂不支持托管创建，请使用外部 Git 工具', 409)
        if request.branch:
            git._git(root, 'check-ref-format', '--branch', request.branch)
            if request.branch.startswith('-') or request.branch.startswith('@'):
                workspace.fail('请输入新的分支名')
            if git._git(root, 'show-ref', '--verify', 'refs/heads/' + request.branch, allow_failure=True):
                workspace.fail('分支已存在，请使用新的分支名，或从该分支创建分离工作树', 409)
        id = uuid4().hex
        base = Path(DATA_DIR).resolve() / 'worktrees'
        base.mkdir(exist_ok=True)
        if base.is_symlink() or base.is_junction() or base.resolve() != base:
            workspace.fail('工作树保存目录不能是链接或目录联接', 409)
        parent = base / id; parent.mkdir()
        common = _common(root)
        row = ManagedWorktree(id=id, source_project_id=anchor_id, request_id=request.request_id,
            request_hash=fingerprint, source_scope=source_scope, name=request.name, base_ref=request.ref,
            base_head=head, branch=request.branch, path=str(parent / 'checkout'), parent_identity=workspace.identity(parent),
            repository={'path': str(common), 'identity': workspace.identity(common)})
        db.add(row); db.commit(); _mark(row)
        _active.set()
        try:
            with _pinned(row, root) as path:
                if path.exists() or path.is_symlink() or path.is_junction():
                    workspace.fail('目标目录已存在，拒绝覆盖', 409)
                options = ['-b', request.branch] if request.branch else ['--detach']
                row.output = _run(root, 'worktree', 'add', *options, '--', str(path), head)
                _ready(db, row, root)
        except (HTTPException, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            row.error = str(exc.detail if isinstance(exc, HTTPException) else exc)
            _save(db, row, 'interrupted')
        finally:
            _active.clear()
        return summary(row)


def listing(project_id):
    with SessionLocal() as db:
        workspace.get_project(db, project_id)
        anchor_id = _anchor(db, project_id)
        rows = db.query(ManagedWorktree).filter_by(source_project_id=anchor_id).order_by(ManagedWorktree.created_at.desc()).all()
        source = workspace.get_project(db, anchor_id)
        result = {'source_project_id': anchor_id, 'source_name': source.name, 'source_path': source.root_path,
            'worktrees': [summary(row) for row in rows], 'external': [], 'error': ''}
        try:
            root = git_operations._repository(workspace.project_scope(source))
            entries = _entries(root)
            by_path = {os.path.normcase(os.path.abspath(e['worktree'])): e for e in entries}
            for row, item in zip(rows, result['worktrees']):
                entry = by_path.get(os.path.normcase(row.path))
                path = Path(row.path)
                try:
                    item['available'] = (row.status == 'ready' and bool(entry) and path.is_dir()
                        and path.resolve(strict=True) == path and workspace.identity(path) == row.root_identity)
                except OSError:
                    item['available'] = False
                item['current_branch'] = entry.get('branch', '').removeprefix('refs/heads/') if entry else None
                item['current_head'] = entry.get('HEAD') if entry else None
            known = {os.path.normcase(row.path) for row in rows}
            result['external'] = [e for e in entries if os.path.normcase(os.path.abspath(e['worktree'])) not in known]
        except (HTTPException, OSError) as exc:
            result['error'] = str(exc.detail if isinstance(exc, HTTPException) else exc)
        return result


def detail(project_id, id):
    with SessionLocal() as db:
        return summary(_get(db, _anchor(db, project_id), id))


def _idle(db, row):
    from . import task_runner, terminal_sessions
    root = Path(row.path)
    project_ids = {p.id for p in db.query(Project).all() if Path(p.root_path).is_relative_to(root)}
    ids = {c.id for c in db.query(Conversation).filter(Conversation.project_id.in_(project_ids)).all()}
    if any(cid in ids for cid in run_store.active_runs().values()):
        workspace.fail('此工作树仍有前台执行，请先停止并等待结束', 409)
    if db.query(TaskRecord).filter(TaskRecord.conversation_id.in_(ids),
            (TaskRecord.status.in_(['pending', 'running'])) | (TaskRecord.id.in_(task_runner.running_task_ids()))).first():
        workspace.fail('此工作树仍有后台任务，请先停止并等待结束', 409)
    if any(s.project_id in project_ids and s.worker.is_alive() for s in terminal_sessions._sessions.values()):
        workspace.fail('此工作树仍有终端进程，请先结束终端', 409)


def preview_remove(project_id, id):
    with _guard, git_operations._guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        if row.status != 'ready':
            workspace.fail('工作树当前不可移除，请先恢复或刷新', 409)
        root = _repo(db, row)
        with _pinned(row, root):
            path, entry = _validate_checkout(row, root, clean=True)
            row.removal_snapshot = {'head': entry['HEAD'], 'branch': entry.get('branch'), 'identity': workspace.identity(path)}
            _save(db, row)
        return summary(row)


def remove(project_id, id, revision):
    from . import terminal_sessions
    with _guard, git_operations._guard, run_store.coordinated(), terminal_sessions._guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        if row.status == 'removed':
            return summary(row)
        if row.status != 'ready' or row.revision != revision or not row.removal_snapshot:
            workspace.fail('请重新审阅移除操作，工作树状态已变化', 409)
        root = _repo(db, row)
        _idle(db, row)
        with _pinned(row, root) as path:
            _, entry = _validate_checkout(row, root, clean=True)
            snapshot = {'head': entry['HEAD'], 'branch': entry.get('branch'), 'identity': workspace.identity(path)}
            if snapshot != row.removal_snapshot:
                workspace.fail('审阅后分支或提交发生变化，请重新审阅', 409)
            # Make detached/local commits durable before deleting their checkout.
            row.saved_head = entry['HEAD']; row.archive_info = None
            row.output = _run(root, 'update-ref', 'refs/scholarflow/worktrees/' + row.id, row.saved_head)
            _save(db, row, 'removing'); _active.set()
            try:
                row.output += _run(root, 'worktree', 'remove', '--', str(path))
                if path.exists() or _matching(root, path):
                    workspace.fail('Git 移除结果未确认，请恢复记录后核对', 409)
                _save(db, row, 'removed')
            except (HTTPException, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                row.error = str(exc.detail if isinstance(exc, HTTPException) else exc)
                _save(db, row, 'interrupted')
            finally:
                _active.clear()
        return summary(row)


def preview_archive(project_id, id):
    with _guard, git_operations._guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        if row.status != 'ready':
            workspace.fail('工作树当前不可归档，请先核对恢复记录', 409)
        root = _repo(db, row)
        with _pinned(row, root):
            path, entry = _validate_checkout(row, root)
            row.removal_snapshot = archive.review(archive.inspect(path, entry))
            _save(db, row)
        return summary(row)


def archive_worktree(project_id, id, revision):
    from . import terminal_sessions
    with _guard, git_operations._guard, run_store.coordinated(), terminal_sessions._guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        if row.status == 'removed' and row.archive_info:
            return summary(row)
        if row.status != 'ready' or row.revision != revision or not row.removal_snapshot or row.removal_snapshot.get('kind') != 'archive':
            workspace.fail('请重新审阅归档，工作树状态已变化', 409)
        root = _repo(db, row); _idle(db, row)
        with _pinned(row, root) as path:
            _, entry = _validate_checkout(row, root)
            state = archive.inspect(path, entry)
            if archive.review(state) != row.removal_snapshot:
                workspace.fail('文件、暂存区或分支在审阅后已变化，请重新审阅归档', 409)
            row.archive_info = None; row.saved_head = state['head']
            _save(db, row, 'archiving'); _active.set()
            try:
                with workspace_write._handle(path, directory=True) as checkout:
                    if workspace.handle_path(checkout, path) != path or workspace.identity(path) != row.root_identity:
                        workspace.fail("归档目录身份已变化", 409)
                    info = archive.save(path, path.parent, state, row.id)
                    row.archive_info = info; row.saved_head = info['head']; _save(db, row)
                    _, current = _validate_checkout(row, root)
                    if archive.inspect(path, current) != state:
                        workspace.fail('保存快照期间目录内容已变化，快照已保留，原目录未移除', 409)
                    # Detach only this worktree's HEAD; the original branch is untouched.
                    archive.command(path, 'update-ref', '--no-deref', 'HEAD', info['head'], state['head'])
                    archive.command(path, 'read-tree', info['head'])
                    row.archive_info = {**info, 'phase': 'prepared'}; _save(db, row)
                    if archive.scan(path) != state['files']:
                        workspace.fail('准备移除时文件已变化，快照和原目录已保留', 409)
                    def recorded(rel):
                        row.archive_info = {**row.archive_info, 'removed_ignored': [*row.archive_info['removed_ignored'], rel]}
                        _save(db, row)
                    archive.remove_ignored(path, state, recorded)
                    _validate_checkout(row, root, clean=True)
                # No --force: Git performs another dirty/untracked check itself.
                row.output = _run(root, 'worktree', 'remove', '--', str(path))
                if path.exists() or _matching(root, path):
                    workspace.fail('归档移除结果未确认，请核对记录', 409)
                row.archive_info = {**row.archive_info, 'phase': 'removed'}
                _save(db, row, 'removed')
            except (HTTPException, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                row.error = str(exc.detail if isinstance(exc, HTTPException) else exc)
                _save(db, row, 'interrupted')
            finally:
                _active.clear()
        return summary(row)


def archive_download(project_id, id):
    with _guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        if not row.archive_info:
            workspace.fail('尚无已保存的归档备份', 404)
        parent, _ = _parent(row)
        return archive.open_backup(parent, row.archive_info)


def archive_history(project_id, id):
    """List all durable archive snapshots for a managed worktree."""
    with _guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        root = _repo(db, row)
        parent, _ = _parent(row)
        current_id = (row.archive_info or {}).get('id')
        return {
            'worktree_id': row.id,
            'current_id': current_id,
            'snapshots': archive.history(root, parent, row.id, current_id),
        }


def delete_archive_snapshot(project_id, id, snapshot_id):
    """Remove one non-active archive snapshot after a user review."""
    with _guard, git_operations._guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        root = _repo(db, row)
        parent, _ = _parent(row)
        current_id = (row.archive_info or {}).get('id')
        with workspace_write._handle(parent, directory=True) as handle:
            if workspace.handle_path(handle, parent) != parent or workspace.identity(parent) != row.parent_identity:
                workspace.fail('工作树归档目录身份已变化，拒绝清理', 409)
            result = archive.delete_snapshot(root, parent, row.id, snapshot_id, current_id)
        return result


def restore(project_id, id, revision):
    with _guard, git_operations._guard, SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        if row.status == 'ready':
            return summary(row)
        if row.status != 'removed' or row.revision != revision or not row.saved_head:
            workspace.fail('请刷新工作树记录后恢复', 409)
        root = _repo(db, row)
        with _pinned(row, root) as path:
            if path.exists() or path.is_symlink() or path.is_junction() or _matching(root, path):
                workspace.fail('目标目录或 Git 工作树记录已存在，拒绝覆盖', 409)
            if row.archive_info:
                archive.open_backup(path.parent, row.archive_info)
            row.root_identity = None; _save(db, row, 'restoring'); _active.set()
            try:
                if row.archive_info:
                    row.archive_info = {**row.archive_info, 'phase': 'restoring'}; _save(db, row)
                    row.output = _run(root, 'worktree', 'add', '--detach', '--no-checkout', '--', str(path), row.saved_head)
                    row.root_identity = workspace.identity(path); _save(db, row)
                    archive.command(path, 'read-tree', row.saved_head)
                    archive.restore_files(path, path.parent, row.archive_info, identity=row.root_identity)
                    row.archive_info = {**row.archive_info, 'phase': 'restored'}
                else:
                    row.output = _run(root, 'worktree', 'add', '--detach', '--', str(path), row.saved_head)
                _ready(db, row, root)
            except (HTTPException, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                row.error = str(exc.detail if isinstance(exc, HTTPException) else exc)
                _save(db, row, 'interrupted')
            finally:
                _active.clear()
        return summary(row)


def reconcile(project_id, id, revision):
    with _guard, git_operations._guard, _recovering(), SessionLocal() as db:
        row = _get(db, _anchor(db, project_id), id)
        if row.status != 'interrupted' or row.revision != revision:
            workspace.fail('请刷新后核对中断的工作树', 409)
        root = _repo(db, row)
        with _pinned(row, root) as path:
            if not path.exists() and not path.is_symlink() and not path.is_junction() and not _matching(root, path):
                if row.saved_head:
                    row.error = ''; _save(db, row, 'removed')
                else:
                    row.error = '已确认未创建工作目录，可使用新请求重新创建。'; _save(db, row, 'failed')
            else:
                # A failed removal may leave the original directory with later
                # user edits. Reattaching that exact identity preserves them;
                # an interrupted add/restore needs a clean checkout to prove it
                # finished materializing the requested commit.
                info = row.archive_info
                _, entry = _validate_checkout(row, root, clean=not info and not (row.saved_head and row.root_identity))
                permitted_heads = (row.base_head, row.saved_head, info.get('original_head') if info else None)
                if entry['HEAD'] not in permitted_heads:
                    workspace.fail('工作树提交已变化，请在外部 Git 工具核对，未自动接管', 409)
                if info:
                    if info['phase'] == 'restoring':
                        # The interrupted no-checkout add may have no index yet.
                        if entry['HEAD'] != row.saved_head:
                            workspace.fail('恢复中的提交已变化，请核对备份', 409)
                        index_path = archive.git_dir(path) / 'index'
                        if index_path.exists():
                            archive.check_index(path)
                            actual = archive.command(path, 'write-tree').strip()
                            expected = git._git(path, 'rev-parse', row.saved_head + '^{tree}').strip()
                            if actual != expected:
                                workspace.fail('恢复期间暂存区已变化，未覆盖，请先核对', 409)
                        archive.restore_files(path, path.parent, info, identity=row.root_identity)
                        if not index_path.exists():
                            archive.command(path, 'read-tree', row.saved_head)
                    else:
                        archive.restore_files(path, path.parent, info, only=set(info['removed_ignored']), identity=row.root_identity)
                    row.archive_info = {**info, 'phase': 'restored' if info['phase'] == 'restoring' else 'retained'}
                if row.error:
                    row.output = (row.output + '\n恢复前记录：' + row.error)[-32000:]
                _ready(db, row, root)
        return summary(row)


@contextmanager
def _recovering():
    # Reconciliation can now restore bytes, so shutdown must wait for it too.
    _active.set()
    try:
        yield
    finally:
        _active.clear()


def recover_interrupted():
    with _guard, SessionLocal() as db:
        _unavailable.clear()
        for row in db.query(ManagedWorktree).all():
            if row.status in ('creating', 'removing', 'archiving', 'restoring'):
                row.status = 'interrupted'; row.revision += 1
                row.error = '上次处理工作树时进程退出，请核对磁盘和 Git 状态后恢复记录；不会自动重放。'
            _mark(row)
        db.commit()


def active():
    return _active.is_set()
