"""Exercise reviewed workspace switches using the packaged EXE and disposable data."""
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
data = Path(tempfile.mkdtemp(prefix='workspace-switch-exe-', dir=root / 'verification/runtime'))
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
    with opener.open(req, timeout=25) as response:
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
    return api(f'/conversations/{cid}/workspace-switches', {'request_id': f'review_{time.time_ns()}',
        'target_project_id': target, 'workspace_revision': draft['value'].get('workspace_revision', 0), 'draft_revision': draft['revision']})


try:
    git(folder, 'init', '-b', 'main')
    for key, value in [('user.name', 'EXE Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
        git(folder, 'config', key, value)
    (folder/'a.txt').write_text('initial\n'); git(folder, 'add', '.'); git(folder, 'commit', '-m', 'Initial')
    launch()
    project = api('/projects', {'path': str(folder)})
    cid = api('/conversations', {'title': 'EXE 目录切换', 'project_id': project['id']})['id']
    tree = api(f"/projects/{project['id']}/worktrees", {'request_id':'exe_switch_tree_001', 'name':'独立目录', 'ref':'HEAD'})
    assert tree['status'] == 'ready', tree
    (folder/'a.txt').write_text('source staged\n'); git(folder, 'add', '.')
    (Path(tree['path'])/'a.txt').write_text('target edited\n')
    doc_path = '/desktop-state/conversations/' + cid
    draft = api(doc_path)
    value = {**draft['value'], 'text':'保留未发送文字', 'file_paths':['a.txt'], 'editor':{
        'project_id':project['id'], 'path':'a.txt', 'operation':'update', 'content':'保留编辑草稿', 'sha256':None}}
    api(doc_path, {'revision':draft['revision'], 'value':value}, 'PUT')
    review = prepare(cid, tree['project_id'])
    first_port = port; stop(); launch()
    assert port != first_port
    assert api(f'/conversations/{cid}/workspace-switches')['switches'][0]['id'] == review['id']
    endpoint = f"/conversations/{cid}/workspace-switches/{review['id']}/apply"
    assert api(endpoint, {})['status'] == 'applied'
    stop(); launch()
    assert api(endpoint, {})['status'] == 'applied'
    current = api(f'/conversations/{cid}/project')
    assert current['project']['id'] == tree['project_id'] and current['workspace_revision'] == 1
    saved = api(doc_path)['value']
    assert saved['editor'] == value['editor'] and saved['text'] == value['text'] and saved['file_paths'] == []
    try:
        api('/foreground-runs', {'id':'chat_stale_exe_scope','conversation_id':cid,'question':'old request','project_id':tree['project_id']})
        raise AssertionError('stale request admitted')
    except urllib.error.HTTPError as exc:
        assert exc.code == 409
    fresh = api('/foreground-runs', {'id':'chat_fresh_exe_scope','conversation_id':cid,'question':'fresh request',
        'project_id':tree['project_id'], 'workspace_revision':1})
    api('/foreground-runs/chat_fresh_exe_scope', {'revision':fresh['revision'], 'status':'cancelled'}, 'PATCH')
    back = prepare(cid, project['id'])
    api(f"/conversations/{cid}/workspace-switches/{back['id']}/apply", {})
    assert api(f'/conversations/{cid}/project')['workspace_revision'] == 2
    assert (folder/'a.txt').read_text() == 'source staged\n'
    assert (Path(tree['path'])/'a.txt').read_text() == 'target edited\n'
    assert git(folder, 'show', ':a.txt') == 'source staged' and git(Path(tree['path']), 'diff', '--cached') == ''
    assert len(api(f'/conversations/{cid}/workspace-switches')['switches']) == 2
    with opener.open(f'http://127.0.0.1:{port}/chat') as response:
        assert response.read() == (root/'frontend/dist/index.html').read_bytes()
    assets = [p for p in (root/'frontend/dist/assets').rglob('*') if p.is_file()]
    for asset in assets:
        rel = asset.relative_to(root/'frontend/dist').as_posix()
        with opener.open(f'http://127.0.0.1:{port}/{rel}') as response:
            assert response.read() == asset.read_bytes(), rel
    print('PASS: EXE prepare/restart/apply/restart/retry/return; editor/text/history preserved, stale execution refused, new execution admitted, Git files/index unchanged')
    print(f'PASS: HTTP index and all {len(assets)} static assets match current dist byte for byte')
    print('Scope: HTTP/process verification; native clicks and Codex UI parity remain unverified')
    print('Retained isolated data:', data)
finally:
    stop()
