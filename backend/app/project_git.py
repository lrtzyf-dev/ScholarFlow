"""Bounded, read-only Git inspection for an explicitly registered project root."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import workspace
from . import workspace_write
from contextlib import contextmanager, ExitStack

MAX_OUTPUT = 2 * 1024 * 1024
PAGE_SIZE = 200


@contextmanager
def _pinned(scope, path=''):
    root = workspace.root_path(scope)
    with ExitStack() as stack:
        chain = [root]
        if path:
            current = root
            for part in Path(path).parts[:-1]:
                current /= part
                if current.exists() or current.is_symlink() or current.is_junction():
                    workspace.scoped_path(scope, current.relative_to(root).as_posix())
                    chain.append(current)
        for directory in chain:
            handle = stack.enter_context(workspace_write._handle(directory, directory=True))
            if workspace.handle_path(handle, directory) != directory:
                workspace.fail('Git 读取期间目录发生变化，请刷新', 409)
        workspace.root_path(scope)
        yield


def _snapshot(data, expected):
    actual = hashlib.sha256(data).hexdigest()
    if expected is not None and actual != expected:
        workspace.fail('Git 内容已变化，请刷新后重新查看', 409)
    return actual


def _git(root: Path, *args: str, allow_failure: bool = False) -> bytes | None:
    executable = shutil.which('git')
    if not executable:
        workspace.fail('本机未安装 Git，无法查看版本控制状态', 503)
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_PAGER='cat', GIT_OPTIONAL_LOCKS='0')
    command = [executable, '--no-pager', '--literal-pathspecs', '-c', 'core.fsmonitor=false',
               '-c', 'diff.external=', '-c', 'core.quotepath=false', '-C', str(root), *args]
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                    env=environment, timeout=20, check=False, creationflags=0x08000000 if os.name == 'nt' else 0)
            if result.returncode:
                if allow_failure:
                    return None
                errors.seek(0)
                message = errors.read(1000).decode('utf-8', errors='replace').strip()
                workspace.fail(f'Git 操作失败：{message or result.returncode}', 409)
            if output.tell() > MAX_OUTPUT:
                workspace.fail('Git 输出超过 2 MB，请缩小项目或清理工作区', 413)
            output.seek(0)
            return output.read()
    except subprocess.TimeoutExpired:
        workspace.fail('Git 操作超时，请稍后重试', 504)
    except OSError:
        workspace.fail('无法启动 Git，请检查本机安装和目录权限', 503)


def _repository(scope):
    root = workspace.root_path(scope)
    top = _git(root, 'rev-parse', '--show-toplevel', allow_failure=True)
    if top is None:
        if (root / '.git').exists():
            workspace.fail('无法读取此 Git 仓库，请检查权限、仓库配置和 Git 信任设置', 409)
        return root, False
    try:
        actual = Path(top.decode('utf-8').strip()).resolve(strict=True)
    except (UnicodeError, OSError):
        workspace.fail('无法确认 Git 仓库根目录', 409)
    if os.path.normcase(str(actual)) != os.path.normcase(str(root)):
        return root, False
    workspace.root_path(scope)
    return root, True


def _text(data: bytes):
    try:
        return data.decode('utf-8')
    except UnicodeError:
        workspace.fail('Git 输出包含无法解码的文件名或文本', 409)


def _entries(data: bytes):
    fields = data.split(b'\0')
    entries = []
    index = 0
    while index < len(fields) and fields[index]:
        field = fields[index]
        if len(field) < 4 or field[2:3] != b' ':
            workspace.fail('Git 状态格式不完整，请刷新', 409)
        code = _text(field[:2])
        # Git lists tracked dependency files too; keep the list complete while
        # retaining the workspace reader's stricter rules for opening them.
        path = workspace.relative_path(_text(field[3:]), allow_internal=True)
        if not path:
            workspace.fail('Git 返回了空文件路径', 409)
        entry = {'path': path, 'index': code[0], 'worktree': code[1],
                 'staged': code[0] not in (' ', '?'), 'unstaged': code[1] not in (' ', '?'),
                 'untracked': code == '??', 'conflict': code in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU')}
        if code[0] in ('R', 'C') or code[1] in ('R', 'C'):
            index += 1
            if index >= len(fields) or not fields[index]:
                workspace.fail('Git 重命名状态不完整，请刷新', 409)
            entry['original_path'] = workspace.relative_path(_text(fields[index]), allow_internal=True)
        entries.append(entry)
        index += 1
    return entries


def status(scope, offset=0, snapshot=None):
    with _pinned(scope):
        return _status(scope, offset, snapshot)


def _status(scope, offset, snapshot):
    if type(offset) is not int or offset < 0 or offset > 100_000:
        workspace.fail('Git 分页位置无效')
    root, available = _repository(scope)
    if not available:
        return {'available': False, 'reason': '当前项目文件夹不是独立的 Git 仓库根目录',
                'branch': None, 'head': None, 'linked_worktree': False,
                'entries': [], 'total': 0, 'next_offset': None}
    branch = _git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD', allow_failure=True)
    head = _git(root, 'rev-parse', '--verify', '--short', 'HEAD', allow_failure=True)
    git_dir = _git(root, 'rev-parse', '--absolute-git-dir')
    common_dir = _git(root, 'rev-parse', '--git-common-dir')
    common = Path(_text(common_dir).strip())
    if not common.is_absolute():
        common = (root / common).resolve()
    raw = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    revision = _snapshot(raw, snapshot)
    all_entries = _entries(raw)
    return {'available': True, 'reason': '',
            'branch': _text(branch).strip() if branch else None,
            'head': _text(head).strip() if head else None,
            'linked_worktree': os.path.normcase(str(common)) != os.path.normcase(_text(git_dir).strip()),
            'entries': all_entries[offset:offset + PAGE_SIZE], 'total': len(all_entries), 'snapshot': revision,
            'staged_total': sum(e['staged'] for e in all_entries),
            'unstaged_total': sum(e['unstaged'] or e['untracked'] for e in all_entries),
            'next_offset': offset + PAGE_SIZE if offset + PAGE_SIZE < len(all_entries) else None}


def diff(scope, raw_path: str, version: str, offset=0, snapshot=None):
    path = workspace.relative_path(raw_path)
    if not path or version not in ('staged', 'unstaged', 'untracked') or type(offset) is not int or offset < 0 or offset > 100_000:
        workspace.fail('请选择有效的 Git 文件和差异范围')
    with _pinned(scope, path):
        workspace_write.target(scope, path)
        return _diff(scope, path, version, offset, snapshot)


def _diff(scope, path, version, offset, snapshot):
    root, available = _repository(scope)
    if not available:
        workspace.fail('当前项目不是独立的 Git 仓库根目录', 409)
    if version == 'untracked':
        entries = _entries(_git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
        if not any(entry['path'] == path and entry['untracked'] for entry in entries):
            workspace.fail('此文件不再处于未跟踪状态，请刷新', 409)
        page = workspace.read_text(scope, path, offset=offset)
        revision = _snapshot(page['sha256'].encode(), snapshot)
        return {'path': path, 'version': version, 'patch': None, 'preview': page['text'], 'snapshot': revision,
                'next_offset': page['next_offset'], 'binary': False}
    args = ['diff', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames', '--unified=3']
    if version == 'staged':
        args.append('--cached')
    data = _git(root, *args, '--', path)
    revision = _snapshot(data, snapshot)
    lines = _text(data).splitlines(keepends=True)
    page, size = [], 0
    for line in lines[offset:offset + 400]:
        if len(line) > 32768:
            workspace.fail('Git 差异包含超过 32,768 字符的单行，请使用外部编辑器查看', 413)
        if size + len(line) > 32768:
            break
        page.append(line); size += len(line)
    patch = ''.join(page)
    next_offset = offset + len(page)
    return {'path': path, 'version': version, 'patch': patch, 'preview': None,
            'next_offset': next_offset if next_offset < len(lines) else None, 'snapshot': revision,
            'binary': 'Binary files ' in patch, 'total_lines': len(lines)}


def history(scope):
    with _pinned(scope):
        return _history(scope)


def _history(scope):
    root, available = _repository(scope)
    if not available:
        return []
    data = _git(root, 'log', '-n', '20', '--format=%H%x09%h%x09%aI%x09%s', allow_failure=True)
    if data is None:
        return []
    return [dict(zip(('id', 'short_id', 'date', 'subject'), line.split('\t', 3)))
            for line in _text(data).splitlines() if len(line.split('\t', 3)) == 4]
