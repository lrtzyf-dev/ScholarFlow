"""Recoverable worktree snapshots, with byte-exact local backups.

Git stores tracked/nonignored working content and the earlier index as commits.
Ignored files stay in the local ZIP only. No force-removal of a dirty checkout.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from uuid import uuid4
import zipfile

from . import workspace, workspace_write, project_git as git, git_process

MAX_FILES = 10000
MAX_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 60


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')


def command(root, *args, index=None, input=b''):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_PAGER='cat', GIT_OPTIONAL_LOCKS='0',
        GIT_AUTHOR_NAME='ScholarFlow Snapshot', GIT_AUTHOR_EMAIL='snapshot@scholarflow.local',
        GIT_COMMITTER_NAME='ScholarFlow Snapshot', GIT_COMMITTER_EMAIL='snapshot@scholarflow.local')
    if index:
        env['GIT_INDEX_FILE'] = str(index)
    executable = shutil.which('git')
    if not executable:
        workspace.fail('本机未安装 Git', 503)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        code = git_process.run([executable, '--no-pager', '--literal-pathspecs', '-c', 'core.fsmonitor=false',
            '-c', 'commit.gpgsign=false', '-C', str(root), *args], input=input, stdout=out, stderr=err, env=env, timeout=120)
        if out.tell() > 2 * 1024 * 1024:
            workspace.fail('归档 Git 输出超过限制', 413)
        out.seek(0); err.seek(0)
        if code:
            workspace.fail('归档 Git 操作失败：' + err.read(4000).decode('utf-8', errors='replace'), 409)
        return out.read()


def git_dir(path):
    return Path(git._text(git._git(path, 'rev-parse', '--absolute-git-dir')).strip()).resolve(strict=True)


def check_index(path, *, ignore_locks=()):
    flags = git._git(path, 'ls-files', '-v', '-z')
    if any(v and not v.startswith(b'H ') for v in flags.split(b'\0')):
        workspace.fail('索引含稀疏、跳过检查或冲突标记，请先在外部 Git 工具处理', 409)
    index = git._git(path, 'ls-files', '--stage', '-z')
    if any(v and (not v.split(b'\t', 1)[0].endswith(b' 0') or v.startswith(b'160000 ')) for v in index.split(b'\0')):
        workspace.fail('冲突索引或子模块暂不支持归档', 409)
    directory = git_dir(path)
    for name in ('index.lock', 'HEAD.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG', 'SQUASH_MSG'):
        if name not in ignore_locks and (directory / name).exists():
            workspace.fail('Git 正在执行其他操作，请结束后再归档', 409)
    return digest(index + b'\0' + flags)


def _relative(value):
    # Archive files may include ignored dependency folders, but never Git metadata.
    value = workspace.relative_path(value, allow_internal=True)
    if not value or any(part.casefold() == '.git' or workspace_write.nt_reserved(part) for part in value.split('/')):
        workspace.fail('归档路径不是普通项目文件', 409)
    return value


def _files(path):
    result, pending, deadline, total = [], [(path, 0)], time.monotonic() + MAX_SECONDS, 0
    while pending:
        folder, depth = pending.pop()
        if (folder / 'HEAD').is_file() and (folder / 'objects').is_dir() and (folder / 'refs').is_dir():
            workspace.fail('嵌套裸 Git 仓库暂不支持归档', 409)
        if depth > 64 or time.monotonic() > deadline:
            workspace.fail('归档扫描超时或目录超过 64 层，未移除任何文件', 413)
        with os.scandir(folder) as entries:
            for entry in entries:
                item = Path(entry.path)
                if item == path / '.git':
                    continue
                rel = _relative(item.relative_to(path).as_posix())
                if entry.is_symlink() or item.is_junction():
                    workspace.fail('归档不跟随符号链接或目录联接：' + rel, 409)
                if entry.is_dir(follow_symlinks=False):
                    pending.append((item, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    result.append(rel)
                    if len(result) > MAX_FILES or total > MAX_BYTES:
                        workspace.fail('归档最多 10,000 个文件、512 MiB 原始内容，请先移出大型缓存', 413)
                else:
                    workspace.fail('归档只支持普通文件：' + rel, 409)
    return sorted(result)


def _locked(path, rel, stack, *, create=False, delete=False):
    rel = _relative(rel)
    target = path / rel
    current = path
    for part in [None, *Path(rel).parts[:-1]]:
        if part is not None:
            current /= part
        if not current.exists() and create:
            current.mkdir()
        if current.is_symlink() or current.is_junction() or current.resolve(strict=True) != current:
            workspace.fail('归档目录已变化', 409)
        handle = stack.enter_context(workspace_write._handle(current, directory=True))
        if workspace.handle_path(handle, current) != current:
            workspace.fail('归档目录身份已变化', 409)
    file = stack.enter_context(workspace_write._handle(target, create=create, delete=delete))
    if workspace.handle_path(file, target) != target or not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
        workspace.fail('归档文件已变化', 409)
    return file


def scan(path):
    identity = workspace.identity(path)
    names = _files(path)
    files, total = [], 0
    for rel in names:
        with ExitStack() as stack:
            file = _locked(path, rel, stack)
            before = os.fstat(file.fileno())
            total += before.st_size
            if total > MAX_BYTES:
                workspace.fail('归档内容已超过 512 MiB', 413)
            sha = hashlib.file_digest(file, 'sha256').hexdigest()
            files.append({'path': rel, 'bytes': before.st_size, 'sha256': sha})
    if names != _files(path) or workspace.identity(path) != identity:
        workspace.fail('扫描期间工作目录已变化，请重新审阅', 409)
    return files


def inspect(path, entry, *, ignore_locks=()):
    index = check_index(path, ignore_locks=ignore_locks)
    files = scan(path)
    ignored = [git._text(v) for v in git._git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').split(b'\0') if v]
    state = {'head': entry['HEAD'], 'branch': entry.get('branch'), 'identity': workspace.identity(path),
        'index': index, 'files': files, 'ignored': sorted(ignored),
        'config': digest(git._git(path, 'config', '--null', '--list', '--show-origin'))}
    return state


def review(state):
    ignored = set(state['ignored'])
    return {'kind': 'archive', 'fingerprint': digest(encoded(state)), 'head': state['head'],
        'branch': state['branch'], 'identity': state['identity'], 'files': len(state['files']),
        'bytes': sum(f['bytes'] for f in state['files']), 'ignored': len(ignored),
        'preview': [{'path': f['path'], 'ignored': f['path'] in ignored} for f in state['files'][:100]]}


def save(path, parent, state, worktree_id):
    snapshot_id = uuid4().hex
    backup = parent / ('archive-' + snapshot_id + '.zip')
    # fsync the ZIP before updating refs or modifying this checkout.
    with backup.open('xb') as output:
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for item in state['files']:
                with ExitStack() as stack:
                    file = _locked(path, item['path'], stack)
                    sha = hashlib.sha256(); size = 0
                    with archive.open('files/' + item['path'], 'w') as destination:
                        while chunk := file.read(1024 * 1024):
                            sha.update(chunk); size += len(chunk); destination.write(chunk)
                    if size != item['bytes'] or sha.hexdigest() != item['sha256']:
                        workspace.fail('备份期间文件已变化，原目录未移除，请重新审阅', 409)
            archive.writestr('manifest.json', encoded(state))
            archive.write(git_dir(path) / 'index', 'original-index')
        output.flush(); os.fsync(output.fileno())
    with backup.open('rb') as source:
        backup_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    index_tree = command(path, 'write-tree').decode().strip()
    index_head = command(path, 'commit-tree', index_tree, '-p', state['head'], input=b'ScholarFlow archived index\n').decode().strip()
    with tempfile.TemporaryDirectory(prefix='snapshot-index-', dir=parent) as directory:
        index = Path(directory) / 'index'
        command(path, 'read-tree', index_tree, index=index)
        command(path, 'add', '-A', '--', '.', index=index)
        tree = command(path, 'write-tree', index=index).decode().strip()
    head = command(path, 'commit-tree', tree, '-p', state['head'], '-p', index_head,
        input=b'ScholarFlow worktree archive\n').decode().strip()
    reference = 'refs/scholarflow/worktree-archives/' + worktree_id + '/' + snapshot_id
    command(path, 'update-ref', reference, head)
    return {'version': 1, 'id': snapshot_id, 'head': head, 'index_head': index_head,
        'original_head': state['head'], 'original_branch': state['branch'], 'ref': reference,
        'backup': backup.name, 'sha256': backup_hash, 'files': len(state['files']),
        'bytes': sum(f['bytes'] for f in state['files']), 'ignored': len(state['ignored']),
        'phase': 'saved', 'removed_ignored': []}


def open_backup(parent, info):
    name = 'archive-' + info['id'] + '.zip'
    if info['backup'] != name or len(info['id']) != 32 or any(c not in '0123456789abcdef' for c in info['id']):
        workspace.fail('归档备份记录无效', 409)
    path = parent / name
    if path.is_symlink() or path.is_junction() or path.resolve(strict=True) != path:
        workspace.fail('归档备份路径已变化', 409)
    with path.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != info['sha256']:
            workspace.fail('归档备份校验失败，未恢复或覆盖任何文件', 409)
    return path


def _snapshot_id(value):
    """Validate the opaque snapshot id before using it in a ref or filename."""
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        workspace.fail('归档快照编号无效', 400)
    return value


def history(root, parent, worktree_id, current_id=None):
    """Return durable archive refs and their local ZIP metadata.

    The archive ref is the source of truth; the ZIP is deliberately checked
    independently so a missing or damaged local backup is visible before a
    user tries to restore it. Older refs are retained across restore/archive
    cycles and can be explicitly cleaned up by the worktree UI.
    """
    if not isinstance(worktree_id, str) or len(worktree_id) != 32 or any(c not in '0123456789abcdef' for c in worktree_id):
        workspace.fail('工作树编号无效', 400)
    prefix = f'refs/scholarflow/worktree-archives/{worktree_id}/'
    raw = command(root, 'for-each-ref', '--format=%(refname) %(objectname)', prefix)
    result = []
    for line in raw.decode('utf-8', errors='strict').splitlines():
        ref, _, head = line.partition(' ')
        snapshot_id = ref.removeprefix(prefix)
        try:
            _snapshot_id(snapshot_id)
        except Exception:
            continue
        backup = parent / f'archive-{snapshot_id}.zip'
        item = {
            'id': snapshot_id, 'head': head, 'ref': ref,
            'backup': backup.name, 'current': snapshot_id == current_id,
            'backup_present': False, 'backup_valid': False,
            'sha256': None, 'files': None, 'bytes': None, 'ignored': None,
            'created_at': None,
        }
        try:
            created = command(root, 'show', '-s', '--format=%cI', head).decode('utf-8', errors='strict').strip()
            item['created_at'] = created or None
        except Exception:
            pass
        if backup.is_file() and not backup.is_symlink() and not backup.is_junction():
            item['backup_present'] = True
            try:
                with backup.open('rb') as source:
                    item['sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
                with zipfile.ZipFile(backup) as archive_file:
                    state = json.loads(archive_file.read('manifest.json'))
                item['files'] = len(state.get('files') or [])
                item['bytes'] = sum(int(f.get('bytes') or 0) for f in state.get('files') or [])
                item['ignored'] = len(state.get('ignored') or [])
                item['backup_valid'] = True
            except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
                item['backup_valid'] = False
        result.append(item)
    result.sort(key=lambda item: (item.get('created_at') or '', item['id']), reverse=True)
    return result


def delete_snapshot(root, parent, worktree_id, snapshot_id, current_id=None):
    """Delete one old archive ref and its matching local ZIP.

    The active archive cannot be deleted because it is the only restore
    source for the removed worktree. The expected object id is passed to
    update-ref so an externally changed ref is rejected instead of deleting
    an unrelated object.
    """
    _snapshot_id(snapshot_id)
    if snapshot_id == current_id:
        workspace.fail('当前归档快照用于恢复，不能删除', 409)
    entries = history(root, parent, worktree_id, current_id)
    item = next((row for row in entries if row['id'] == snapshot_id), None)
    if item is None:
        workspace.fail('归档快照不存在或已清理', 404)
    backup = parent / f'archive-{snapshot_id}.zip'
    command(root, 'update-ref', '-d', item['ref'], item['head'])
    if backup.exists() or backup.is_symlink() or backup.is_junction():
        if not backup.is_file() or backup.is_symlink() or backup.is_junction():
            workspace.fail('归档备份路径不是普通文件，Git 快照已清理但本机文件需人工处理', 409)
        backup.unlink()
    return {'id': snapshot_id, 'deleted': True}


def restore_files(path, parent, info, *, only=None, identity=None):
    with workspace_write._handle(path, directory=True) as handle:
        if workspace.handle_path(handle, path) != path or (identity and workspace.identity(path) != identity):
            workspace.fail('恢复目录已被替换，未写入文件', 409)
        return _restore_files(path, parent, info, only=only)


def _restore_files(path, parent, info, *, only=None):
    backup = open_backup(parent, info)
    with zipfile.ZipFile(backup) as archive:
        state = json.loads(archive.read('manifest.json'))
        for item in state['files']:
            rel = _relative(item['path'])
            if only is not None and rel not in only:
                continue
            target = path / rel
            if target.exists() or target.is_symlink() or target.is_junction():
                # Resume never overwrites a later user edit, even if byte-different.
                if only is not None:
                    continue
                with ExitStack() as stack:
                    file = _locked(path, rel, stack)
                    if hashlib.file_digest(file, 'sha256').hexdigest() != item['sha256']:
                        workspace.fail('恢复目标包含不同内容，保留现有文件，请先核对：' + rel, 409)
                continue
            with ExitStack() as stack:
                file = _locked(path, rel, stack, create=True)
                with archive.open('files/' + rel) as source:
                    shutil.copyfileobj(source, file, 1024 * 1024)
                file.flush(); os.fsync(file.fileno()); file.seek(0)
                if hashlib.file_digest(file, 'sha256').hexdigest() != item['sha256']:
                    workspace.fail('恢复文件校验失败，请保留备份并核对', 409)
    return state


def remove_ignored(path, state, record_before_delete):
    items = {f['path']: f for f in state['files']}
    for rel in state['ignored']:
        item = items.get(rel)
        if not item:
            workspace.fail('被忽略文件已变化，未移除工作树', 409)
        with ExitStack() as stack:
            file = _locked(path, rel, stack, delete=True)
            if hashlib.file_digest(file, 'sha256').hexdigest() != item['sha256']:
                workspace.fail('被忽略文件在审阅后已变化，未移除工作树', 409)
            record_before_delete(rel)
            workspace_write.delete_content(file)
