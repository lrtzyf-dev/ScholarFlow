"""Durable, explicitly reviewed Git index and commit operations."""
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
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import git_process, project_git as git, workspace, workspace_write
from .db import SessionLocal
from .models import GitOperation

_guard = threading.RLock()
_active = threading.Event()
MAX_PREVIEW = 256 * 1024


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,64}$')
    action: Literal['stage', 'unstage', 'commit']
    paths: list[str] | None = Field(default=None, max_length=1000)
    message: str = Field(default='', max_length=10000)

    @model_validator(mode='after')
    def valid(self):
        if self.action == 'commit':
            if self.paths is not None or not self.message.strip() or '\x00' in self.message:
                raise ValueError('提交需要说明，且只能提交全部已暂存内容')
        elif self.message or self.paths == []:
            raise ValueError('暂存操作需要有效的文件范围')
        if self.paths is not None:
            self.paths = sorted(set(workspace.relative_path(p) for p in self.paths))
            if '' in self.paths:
                raise ValueError('不能使用空文件路径')
        return self


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def get(db, project_id, operation_id):
    row = db.get(GitOperation, operation_id)
    if not row or row.project_id != project_id:
        workspace.fail('Git 操作不存在', 404)
    return row


def summary(row, *, detail=False):
    result = {key: getattr(row, key) for key in (
        'id', 'project_id', 'action', 'paths', 'message', 'status', 'revision', 'error',
        'output', 'result_head', 'created_at', 'updated_at')}
    result['agent_owned'] = bool(row.agent_owner)
    if detail:
        result.update(patch=row.patch, branch=row.snapshot['branch'],
                      head=row.snapshot['head'], author=row.snapshot.get('author'))
    return result


def _repository(scope):
    root, available = git._repository(scope)
    if not available:
        workspace.fail('当前项目不是独立的 Git 仓库根目录', 409)
    return root


def _metadata(root):
    paths = []
    for option in ('--absolute-git-dir', '--git-common-dir'):
        path = Path(git._text(git._git(root, 'rev-parse', option)).strip())
        if not path.is_absolute():
            path = root / path
        paths.append(path.resolve(strict=True))
    return list(dict.fromkeys(paths))


@contextmanager
def _pinned(scope, paths=()):
    with ExitStack() as stack:
        stack.enter_context(git._pinned(scope))
        root = _repository(scope)
        for directory in _metadata(root):
            handle = stack.enter_context(workspace_write._handle(directory, directory=True))
            if workspace.handle_path(handle, directory) != directory:
                workspace.fail('Git 元数据目录已变化，请刷新', 409)
        for path in paths:
            stack.enter_context(git._pinned(scope, path))
            workspace_write.target(scope, path)
        yield root


def _file(scope, path):
    target = workspace_write.target(scope, path)
    if not target.exists():
        return None, '[deleted]\n'
    try:
        with target.open('rb') as source:
            workspace._check_handle(source, scope, target)
            before = os.fstat(source.fileno())
            data = source.read(git.MAX_OUTPUT + 1)
            after = os.fstat(source.fileno())
            workspace._check_handle(source, scope, target)
        if len(data) > git.MAX_OUTPUT:
            workspace.fail('暂存审阅仅支持单文件不超过 2 MB', 413)
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            workspace.fail('读取期间文件已改变，请重试', 409)
        try:
            text = data.decode('utf-8') if b'\x00' not in data else '[binary]\n'
        except UnicodeError:
            text = '[binary or non-UTF-8]\n'
        return _hash(data), text
    except OSError:
        workspace.fail('无法读取待暂存文件，请检查权限', 409)


def _snapshot(scope, root, action, paths):
    head = git._git(root, 'rev-parse', '--verify', 'HEAD', allow_failure=True)
    branch = git._git(root, 'symbolic-ref', '--quiet', 'HEAD', allow_failure=True)
    index = git._git(root, 'ls-files', '--stage', '-z')
    flags = git._git(root, 'ls-files', '-v', '-z')
    if any(field and not field.split(b'\t', 1)[0].endswith(b' 0') for field in index.split(b'\0')):
        workspace.fail('仓库存在未解决的合并冲突，请先在外部 Git 工具中处理', 409)
    metadata = _metadata(root)
    for directory in metadata:
        if any((directory / name).exists() for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG', 'SQUASH_MSG')):
            workspace.fail('仓库正在合并、变基或其他特殊流程中，请先在外部 Git 工具中完成', 409)
    result = {'head': git._text(head).strip() if head else None,
              'branch': git._text(branch).strip() if branch else None,
              'metadata': [(str(p), workspace.identity(p)) for p in metadata],
              'config': _hash(git._git(root, 'config', '--null', '--list', '--show-origin')),
              'index': _hash(index + b'\0' + flags)}
    if action == 'stage':
        result['files'] = {p: _file(scope, p)[0] for p in paths}
        # Attributes from working-tree ancestors, info/attributes and global
        # configuration can change clean filters and line-ending conversion.
        attributes = hashlib.sha256()
        batch, length = [], 0
        for path in paths:
            if length + len(path) > 12000 and batch:
                attributes.update(git._git(root, 'check-attr', '-z', '--all', '--', *batch))
                batch, length = [], 0
            batch.append(path); length += len(path) + 1
        if batch:
            attributes.update(git._git(root, 'check-attr', '-z', '--all', '--', *batch))
        result['attributes'] = attributes.hexdigest()
    if action == 'commit':
        author = git._git(root, 'var', 'GIT_AUTHOR_IDENT')
        # Git appends a changing timestamp; identity is the stable prefix.
        result['author'] = git._text(author).strip().rsplit(' ', 2)[0]
    # JSON persistence must preserve the same list/tuple shape for comparison.
    return json.loads(json.dumps(result))


def _paths(scope, root, request):
    entries = git._entries(git._git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
    if request.action == 'commit':
        selected = [e for e in entries if e['staged']]
    else:
        selected = [e for e in entries if (e['unstaged'] or e['untracked'])] if request.action == 'stage' else [e for e in entries if e['staged']]
        if request.paths is not None:
            selected = [e for e in selected if e['path'] in request.paths]
            if set(request.paths) != {e['path'] for e in selected}:
                workspace.fail('所选文件状态已变化，请刷新', 409)
    if not selected:
        workspace.fail('没有可操作的文件，请刷新', 409)
    paths = set()
    for entry in selected:
        paths.add(entry['path'])
        if entry.get('original_path'):
            paths.add(entry['original_path'])
    if len(paths) > 1000:
        workspace.fail('单次最多操作 1,000 个文件，请缩小范围', 413)
    result = sorted(paths)
    for path in result:
        workspace_write.target(scope, path)
    # Directories in status are submodules; do not mutate them implicitly.
    if any((Path(scope['root_path']) / p).is_dir() for p in result):
        workspace.fail('暂不支持子模块或目录级 Git 写入，请使用外部 Git 工具', 409)
    return result, entries


def _patch(scope, root, action, paths, entries):
    chunks = []
    for path in paths:
        entry = next((e for e in entries if e['path'] == path), None)
        if action == 'stage' and entry and entry['untracked']:
            digest, text = _file(scope, path)
            value = f'--- /dev/null\n+++ b/{path}\nSHA256 {digest}\n' + ''.join('+' + line + '\n' for line in text.splitlines())
        else:
            args = ['diff', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames', '--unified=3']
            if action != 'stage':
                args.append('--cached')
            value = git._text(git._git(root, *args, '--', path))
        chunks.append(value)
        if sum(len(c.encode('utf-8')) for c in chunks) > MAX_PREVIEW:
            workspace.fail('待审阅差异超过 256 KiB，请分批操作或使用外部 Git 工具', 413)
    return ''.join(chunks)


def prepare(scope, request: Request, *, agent_owner=None):
    fingerprint = _hash(json.dumps(request.model_dump(exclude={'request_id'}), sort_keys=True).encode())
    with _guard, SessionLocal() as db:
        existing = db.query(GitOperation).filter_by(request_id=request.request_id).first()
        if existing:
            if (existing.project_id != scope['project_id'] or existing.request_hash != fingerprint
                    or existing.agent_owner != agent_owner):
                workspace.fail('请求编号已用于不同的 Git 操作', 409)
            return summary(existing, detail=True)
        with _pinned(scope) as root:
            paths, entries = _paths(scope, root, request)
            with _pinned(scope, paths):
                snapshot = _snapshot(scope, root, request.action, paths)
                patch = _patch(scope, root, request.action, paths, entries)
                if snapshot != _snapshot(scope, root, request.action, paths):
                    workspace.fail('生成审阅期间 Git 内容发生变化，请重试', 409)
                row = GitOperation(project_id=scope['project_id'], scope=scope, agent_owner=agent_owner, request_id=request.request_id,
                    request_hash=fingerprint, action=request.action, paths=paths, message=request.message,
                    snapshot=snapshot, patch=patch)
                db.add(row); db.commit(); db.refresh(row)
                return summary(row, detail=True)


def prepare_agent(scope, conversation_id, arguments, *, owner, operation_key):
    """Bind model writes to the saved conversation scope and a durable tool identity."""
    if not scope or not conversation_id:
        workspace.fail('Git 写入需要绑定项目的会话')
    if not isinstance(arguments, dict) or set(arguments) - {'action', 'paths', 'message'}:
        workspace.fail('不允许覆盖 Git 操作编号或会话项目范围')
    with SessionLocal() as db:
        workspace.validate_scope(db, conversation_id, scope)
    request_id = _hash(json.dumps([owner, operation_key], sort_keys=True).encode())
    try:
        request = Request(request_id=request_id, **arguments)
    except ValidationError as exc:
        workspace.fail(str(exc))
    return prepare(scope, request, agent_owner=owner)


def agent_result(operation):
    """Bounded result for model/checkpoint traces; full patch stays in the review."""
    result = {key: operation[key] for key in ('project_id', 'action', 'paths', 'message', 'status',
        'revision', 'error', 'output', 'result_head')}
    result['operation_id'] = operation['id']
    if result['status'] != 'applied' and not result['error']:
        result['error'] = 'Git 操作尚未完成或已取消，请核对操作记录；不得假设成功。'
    return result


def cancel_agent_pending(owner):
    """Leaving a run invalidates its outstanding permission, never its completed result."""
    with _guard, SessionLocal() as db:
        cancelled = []
        for row in db.query(GitOperation).filter_by(status='prepared').all():
            if row.agent_owner == owner:
                row.status = 'cancelled'; row.revision += 1; row.updated_at = datetime.utcnow()
                row.error = '发起此操作的任务已停止，未执行 Git 写入；如仍需操作，请重新生成审阅。'
                cancelled.append(summary(row))
        db.commit()
        return cancelled


def _execute(root, row):
    executable = shutil.which('git')
    if not executable:
        workspace.fail('本机未安装 Git', 503)
    if row.action == 'stage':
        args = ['add', '-A', '--pathspec-from-file=-', '--pathspec-file-nul']
    elif row.action == 'unstage':
        args = (['restore', '--staged'] if row.snapshot['head'] else ['rm', '--cached', '--force', '--ignore-unmatch'])
        args += ['--pathspec-from-file=-', '--pathspec-file-nul']
    else:
        args = ['-c', 'i18n.commitEncoding=UTF-8', 'commit', '--file=-']
    payload = row.message.encode('utf-8') if row.action == 'commit' else b''.join(p.encode('utf-8') + b'\0' for p in row.paths)
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_PAGER='cat')
    command = [executable, '--no-pager', '--literal-pathspecs', '-c', 'core.fsmonitor=false', '-C', str(root), *args]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        code = git_process.run(command, input=payload, stdout=output, stderr=errors, env=environment)
        output.seek(0); errors.seek(0)
        text = (output.read(8000) + b'\n' + errors.read(8000)).decode('utf-8', errors='replace').strip()
    return code, text


def decide(project_id, operation_id, revision, *, cancel=False, agent_owner=None):
    with _guard, SessionLocal() as db:
        row = get(db, project_id, operation_id)
        if row.agent_owner != agent_owner:
            workspace.fail('模型 Git 操作请通过所属聊天或后台任务审批', 403)
        if row.status != 'prepared':
            return summary(row, detail=True)
        if row.revision != revision:
            workspace.fail('Git 操作已变化，请重新打开审阅', 409)
        if cancel:
            row.status = 'cancelled'; row.revision += 1; row.updated_at = datetime.utcnow()
            db.commit()
            return summary(row, detail=True)
        _active.set()
        try:
            try:
                if row.agent_owner:
                    workspace.validate_scope(db, row.agent_owner['conversation_id'], row.scope)
                with _pinned(row.scope, row.paths) as root:
                    if _snapshot(row.scope, root, row.action, row.paths) != row.snapshot:
                        workspace.fail('Git 内容或分支已变化，请刷新并重新生成审阅', 409)
                    row.status = 'running'; row.revision += 1; row.updated_at = datetime.utcnow(); db.commit()
                    code, row.output = _execute(root, row)
                    row.status = 'applied' if code == 0 else 'failed'
                    if code:
                        row.error = 'Git 返回失败，请检查下方输出和当前仓库状态后重新审阅'
                    head = git._git(root, 'rev-parse', '--verify', 'HEAD', allow_failure=True)
                    row.result_head = git._text(head).strip() if head else None
                    if code and row.action == 'commit' and row.result_head != row.snapshot['head']:
                        row.status = 'interrupted'
                        row.error = 'Git 返回失败但 HEAD 已变化，提交可能已完成。请核对仓库，系统不会自动重放。'
            except HTTPException as exc:
                row.status = 'conflict' if row.status == 'prepared' else 'interrupted'
                row.error = str(exc.detail)
            except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
                row.status = 'interrupted'
                row.error = 'Git 操作中断或超时，结果可能已写入；请刷新核对，系统不会自动重放。' + str(exc)[:500]
            row.revision += 1; row.updated_at = datetime.utcnow(); db.commit()
            return summary(row, detail=True)
        finally:
            _active.clear()


def recover_interrupted():
    with _guard, SessionLocal() as db:
        for row in db.query(GitOperation).filter_by(status='running').all():
            row.status = 'interrupted'; row.revision += 1; row.updated_at = datetime.utcnow()
            row.error = '上次进程在 Git 执行期间退出，结果未确认。请核对仓库，系统不会自动重放。'
        for row in db.query(GitOperation).filter_by(status='prepared').all():
            if row.agent_owner:
                row.status = 'cancelled'; row.revision += 1; row.updated_at = datetime.utcnow()
                row.error = '上次任务等待审批时退出，原审批已失效；请重新生成审阅。'
        db.commit()


def active():
    return _active.is_set() or git_process.active()
