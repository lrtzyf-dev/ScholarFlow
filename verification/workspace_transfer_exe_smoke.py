"""Exercise reviewed uncommitted Git transfers using the packaged EXE and disposable data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

root = Path(__file__).resolve().parents[1]
exe = root / 'verification/desktop/ScholarFlow/ScholarFlow.exe'
data = Path(tempfile.mkdtemp(prefix='workspace-transfer-exe-', dir=root / 'verification/runtime'))
folder = data / '来源项目 中文'; folder.mkdir()
env = {**os.environ, 'DATA_DIR': str(data), 'LLM_API_KEY': '', 'EMBEDDING_API_KEY': '', 'REDIS_URL': '', 'DESKTOP_NOTIFICATIONS': 'false'}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
process = None
port = None


def api(path, body=None, method=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api' + path,
        data=None if body is None else json.dumps(body).encode(), method=method,
        headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=60) as response:
        return json.loads(response.read())


def launch():
    global process, port
    process = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, startupinfo=startup)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert process.poll() is None, process.returncode
        try:
            info = json.loads((data/'.runtime.json').read_text(encoding='utf-8'))
            port = info.get('port')
            if info.get('pid') == process.pid and port and api('/health')['status'] == 'ok':
                return
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise AssertionError('EXE did not become ready')


def stop():
    if process and process.poll() is None:
        process.terminate(); process.wait(timeout=15)
    if port:
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, encoding='utf-8').strip()


def prepare(cid, target):
    draft = api('/desktop-state/conversations/' + cid)
    return api(f'/conversations/{cid}/workspace-transfers', {'request_id': f'review_{time.time_ns()}',
        'target_project_id': target, 'workspace_revision': draft['value'].get('workspace_revision', 0), 'draft_revision': draft['revision']})


try:
    git(folder, 'init', '-b', 'main')
    for key, value in [('user.name', 'EXE Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
        git(folder, 'config', key, value)
    (folder/'a.txt').write_text('initial\n'); git(folder, 'add', '.'); git(folder, 'commit', '-m', 'Initial')
    launch()
    project = api('/projects', {'path': str(folder)})
    cid = api('/conversations', {'title': 'EXE Git 改动迁移', 'project_id': project['id']})['id']
    tree = api(f"/projects/{project['id']}/worktrees", {'request_id':'exe_transfer_tree_001', 'name':'独立目录', 'ref':'HEAD'})
    assert tree['status'] == 'ready', tree
    target = Path(tree['path'])
    (folder/'a.txt').write_bytes(b'staged\r\n\x00\xff'); git(folder, 'add', '.')
    (folder/'a.txt').write_bytes(b'working\r\n\x00\xfe')
    (folder/'untracked.bin').write_bytes(b'\x00\x80\xff')
    original_index = git(folder, 'ls-files', '--stage')
    doc_path = '/desktop-state/conversations/' + cid
    draft = api(doc_path)
    value = {**draft['value'], 'text':'保留未发送文字', 'file_paths':['a.txt'], 'editor':{
        'project_id':project['id'], 'path':'a.txt', 'operation':'update', 'content':'保留编辑草稿', 'sha256':None}}
    api(doc_path, {'revision':draft['revision'], 'value':value}, 'PUT')
    review = prepare(cid, tree['project_id'])
    assert review['review']['blockers'] == [], review
    first_port = port; stop(); launch()
    assert port != first_port
    assert api(f'/conversations/{cid}/workspace-transfers')['transfers'][0]['id'] == review['id']
    endpoint = f"/conversations/{cid}/workspace-transfers/{review['id']}/apply"
    result = api(endpoint, {})
    assert result['status'] == 'applied', result
    stop(); launch()
    assert api(endpoint, {})['status'] == 'applied'
    current = api(f'/conversations/{cid}/project')
    assert current['project']['id'] == tree['project_id'] and current['workspace_revision'] == 1
    saved = api(doc_path)['value']
    assert saved['editor'] == value['editor'] and saved['text'] == value['text'] and saved['file_paths'] == []
    assert git(folder, 'status', '--porcelain') == ''
    assert (target/'a.txt').read_bytes() == b'working\r\n\x00\xfe'
    assert (target/'untracked.bin').read_bytes() == b'\x00\x80\xff'
    assert git(target, 'ls-files', '--stage') == original_index
    with opener.open(f'http://127.0.0.1:{port}/api/conversations/{cid}/workspace-transfers/{review["id"]}/backup') as response:
        assert response.read(4) == b'PK\x03\x04'
    back = prepare(cid, project['id'])
    returned = api(f"/conversations/{cid}/workspace-transfers/{back['id']}/apply", {})
    assert returned['status'] == 'applied', returned
    assert api(f'/conversations/{cid}/project')['workspace_revision'] == 2
    assert (folder/'a.txt').read_bytes() == b'working\r\n\x00\xfe'
    assert git(folder, 'ls-files', '--stage') == original_index
    assert git(target, 'status', '--porcelain') == ''
    assert len(api(f'/conversations/{cid}/workspace-transfers')['transfers']) == 2
    with opener.open(f'http://127.0.0.1:{port}/chat') as response:
        assert response.read() == (root/'frontend/dist/index.html').read_bytes()
    assets = [p for p in (root/'frontend/dist/assets').rglob('*') if p.is_file()]
    for asset in assets:
        rel = asset.relative_to(root/'frontend/dist').as_posix()
        with opener.open(f'http://127.0.0.1:{port}/{rel}') as response:
            assert response.read() == asset.read_bytes(), rel
    print('PASS: EXE prepare/restart/transfer/restart/retry/return; exact binary bytes, distinct staging, untracked files, cleaned source, preserved drafts, backup download')
    print(f'PASS: HTTP index and all {len(assets)} static assets match current dist byte for byte')
    print('Scope: HTTP/process verification; native clicks and Codex UI parity remain unverified')
    print('Retained isolated data:', data)
finally:
    stop()
