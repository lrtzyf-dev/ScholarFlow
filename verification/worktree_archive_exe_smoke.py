"""Packaged Windows archive: Git snapshot, original index, local ignored bytes and restore."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

root = Path(__file__).resolve().parents[1]
data = Path(tempfile.mkdtemp(prefix='worktree-archive-exe-', dir=root / 'verification/runtime'))
folder = data / '来源项目 中文'; folder.mkdir()


def git(cwd, *args):
    return subprocess.check_output(['git', '-C', str(cwd), *args], encoding='utf-8', creationflags=0x08000000).strip()


git(folder, 'init', '-b', 'main')
for key, value in [('user.name', 'Packaged Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
    git(folder, 'config', key, value)
(folder / 'source.txt').write_text('original\n', encoding='utf-8')
git(folder, 'add', '.'); git(folder, 'commit', '-m', 'Initial')
(folder / 'source.txt').write_text('parent dirty\n', encoding='utf-8')
environment = {**os.environ, 'DATA_DIR': str(data), 'LLM_API_KEY': '', 'EMBEDDING_API_KEY': '',
    'REDIS_URL': '', 'DESKTOP_NOTIFICATIONS': 'false'}
exe = root / 'verification/desktop/ScholarFlow/ScholarFlow.exe'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
process = None


def api(port, path, body=None, method=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api' + path,
        data=None if body is None else json.dumps(body).encode(), method=method,
        headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=150) as response:
        return json.loads(response.read())


def launch():
    global process
    process = subprocess.Popen([str(exe)], cwd=exe.parent, env=environment, creationflags=0x08000000)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert process.poll() is None, process.returncode
        try:
            info = json.loads((data / '.runtime.json').read_text(encoding='utf-8'))
            port = info.get('port')
            if info.get('pid') == process.pid and isinstance(port, int) and port > 0 and api(port, '/health'):
                return port
        except Exception:
            pass
        time.sleep(.1)
    raise AssertionError('EXE not ready')


def stop():
    if process and process.poll() is None:
        process.kill(); process.wait(timeout=10)


try:
    first = launch()
    project = api(first, '/projects', {'path': str(folder)})
    base = f"/projects/{project['id']}/worktrees"
    request = {'request_id': 'packaged_archive_worktree', 'name': '打包工作树 中文 😀', 'ref': 'HEAD', 'branch': 'feature/packaged'}
    row = api(first, base, request)
    assert row['status'] == 'ready', row
    checkout = Path(row['path']); target = base + '/' + row['id']
    assert (checkout / 'source.txt').read_text(encoding='utf-8') == 'original\n'
    assert (folder / 'source.txt').read_text(encoding='utf-8') == 'parent dirty\n'
    assert api(first, base, request)['id'] == row['id']
    assert len(api(first, base)['worktrees']) == 1
    cid = api(first, '/conversations', {'title': '工作树项目会话', 'project_id': row['project_id']})['id']
    assert api(first, '/conversations/' + cid + '/project')['project']['root_path'] == str(checkout)
    original_head = git(checkout, 'rev-parse', 'HEAD')
    (checkout / 'source.txt').write_text('original staged version\n', encoding='utf-8')
    git(checkout, 'add', '.')
    raw_content = b'raw working bytes\x00\xff\r\n'
    (checkout / 'source.txt').write_bytes(raw_content)
    (checkout / 'new.txt').write_text('untracked 中文 😀', encoding='utf-8')
    (checkout / '.gitignore').write_text('private/\n', encoding='utf-8')
    (checkout / 'private').mkdir()
    (checkout / 'private/settings.txt').write_bytes(b'ignored local settings')
    review = api(first, target + '/review-archive', {})
    assert review['removal_snapshot']['ignored'] == 1
    removed = api(first, target + '/archive', {'revision': review['revision']})
    assert removed['status'] == 'removed' and not checkout.exists(), removed
    info = removed['archive_info']; head = info['head']
    assert git(folder, 'rev-parse', info['ref']) == head
    assert git(folder, 'rev-parse', 'feature/packaged') == original_head
    assert git(folder, 'show', info['index_head'] + ':source.txt') == 'original staged version'
    assert 'private/settings.txt' not in git(folder, 'ls-tree', '-r', '--name-only', head)
    assert not api(first, '/conversations/' + cid + '/project')['project']['available']
    assert api(first, target + '/archive', {'revision': review['revision']})['saved_head'] == head
    draft = f"/desktop-state/projects/{project['id']}/worktrees"
    api(first, draft, {'revision': 0, 'value': {'name': 'Next worktree', 'ref': 'main', 'branch': 'feature/next', 'pending': None}}, 'PUT')
    stop(); second = launch()
    assert second != first
    assert api(second, draft)['value']['name'] == 'Next worktree'
    assert api(second, target)['status'] == 'removed'
    restored = api(second, target + '/restore', {'revision': removed['revision']})
    assert restored['status'] == 'ready' and restored['project_id'] == row['project_id'], restored
    assert (checkout / 'source.txt').read_bytes() == raw_content
    assert (checkout / 'new.txt').read_text(encoding='utf-8') == 'untracked 中文 😀'
    assert (checkout / 'private/settings.txt').read_bytes() == b'ignored local settings'
    assert git(checkout, 'rev-parse', 'HEAD') == head
    assert git(checkout, 'branch', '--show-current') == ''
    assert api(second, '/conversations/' + cid + '/project')['project']['available']
    assert (folder / 'source.txt').read_text(encoding='utf-8') == 'parent dirty\n'
    print('PASS: packaged archive snapshot retains working binary bytes, staged-only version, untracked Unicode and ignored local files; original branch unchanged, idempotent archive, new-port metadata/draft/history and detached byte-exact restore')
    print('Scope: EXE HTTP/process/filesystem verification; no native window input or Codex pixel comparison')
    print('Retained isolated verification data:', data)
finally:
    stop()
