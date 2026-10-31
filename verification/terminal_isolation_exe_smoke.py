"""Exercise private terminal workers from the actual windowed EXE."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

import psutil
from websockets.sync.client import connect

root = Path(__file__).resolve().parents[1]
exe = root / 'verification/desktop/ScholarFlow/ScholarFlow.exe'
data = Path(tempfile.mkdtemp(prefix='terminal-isolation-exe-', dir=root / 'verification/runtime'))
folder = data / '项目 中文'; folder.mkdir()
environment = {**os.environ, 'DATA_DIR': str(data), 'LLM_API_KEY': '', 'EMBEDDING_API_KEY': '',
               'REDIS_URL': '', 'DESKTOP_NOTIFICATIONS': 'false'}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
process = subprocess.Popen([str(exe)], cwd=exe.parent, env=environment, startupinfo=startup)
port = None


def api(path, body=None, method=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api' + path,
        data=None if body is None else json.dumps(body).encode(), method=method,
        headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=15) as response:
        return json.loads(response.read())


def until(fn, label, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(label)


try:
    def ready():
        global port
        assert process.poll() is None, process.returncode
        try:
            info = json.loads((data / '.runtime.json').read_text(encoding='utf-8'))
            port = info.get('port')
            return info.get('pid') == process.pid and port and api('/health')
        except (OSError, ValueError):
            return False
    until(ready, 'EXE ready', 40)
    project = api('/projects', {'path': str(folder)})
    cid = api('/conversations', {'title': 'Terminal isolation acceptance', 'project_id': project['id']})['id']
    base = f"/projects/{project['id']}/terminals"
    for index in range(6):
        req = {'request_id': f'packaged_isolation_{index:03}', 'shell': 'powershell' if index == 0 else 'cmd'}
        item = api(base, req)
        assert api(base, req)['id'] == item['id']
        shell = psutil.Process(item['pid'])
        helper = shell.parent()
        assert helper.pid != process.pid and helper.ppid() == process.pid
        assert helper.exe().casefold() == str(exe).casefold()
        assert '--terminal-worker' in helper.cmdline()
        assert not any(c.status == psutil.CONN_LISTEN for c in helper.net_connections(kind='tcp'))
        assert json.loads((data / '.runtime.json').read_text(encoding='utf-8'))['pid'] == process.pid
        target = base + '/' + item['id']
        if index == 0:
            token = api(target + '/connect', {})['token']
            with connect(f'ws://127.0.0.1:{port}/api{target}/stream', proxy=None) as ws:
                ws.send(json.dumps({'token': token}))
                def read_until(marker):
                    text, deadline = '', time.monotonic() + 20
                    while marker not in text:
                        message = json.loads(ws.recv(timeout=max(.1, deadline - time.monotonic())))
                        assert message['type'] != 'error', message
                        if message['type'] == 'output':
                            text += message['data']
                            ws.send(json.dumps({'type': 'ack', 'sequence': message['sequence'],
                                'response': '\x1b[1;1R' if '\x1b[6n' in message['data'] else ''}))
                read_until('PS ')
                ws.send(json.dumps({'type': 'input', 'data': "[IO.File]::WriteAllText((Join-Path $PWD 'isolated.txt'), '中文 😀'); [Console]::WriteLine('ISOLATED_'+'DONE')\r"}))
                read_until('ISOLATED_DONE')
                assert (folder / 'isolated.txt').read_text(encoding='utf-8') == '中文 😀'
        if index == 3:
            helper.kill()
            until(lambda: api(base)['sessions'][0]['status'] == 'exited', 'worker crash reported')
            assert '意外退出' in api(base)['sessions'][0]['error']
            assert api('/health')['status'] == 'ok'
        api(target, method='DELETE')
        until(lambda: not psutil.pid_exists(helper.pid) and not psutil.pid_exists(item['pid']), 'worker and shell reaped')
        assert api(base)['sessions'] == []
        assert api('/conversations/' + cid)['id'] == cid
    assert not any('_winpty.' in m.path.casefold() for m in psutil.Process(process.pid).memory_maps())
    print('PASS: windowed EXE private stdio worker, six create/close cycles, one helper per shell, no extra backend/listener, Unicode I/O, idempotency, helper-crash containment, healthy API/history and process cleanup')
    print('PASS: primary desktop process has no native _winpty library loaded')
    print('Scope: EXE HTTP/WebSocket/process checks; native window interaction and Codex pixel parity are not verified')
    print('Retained isolated data:', data)
finally:
    if process.poll() is None:
        process.kill(); process.wait(timeout=10)
