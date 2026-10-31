"""Real Windows ConPTY I/O, HTTP admission and descendant lifecycle."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import terminal_sessions as terminals, workspace
from app.api.terminals import CreateTerminal
from app.main import app

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows ConPTY')


@pytest.fixture(autouse=True)
def sessions_cleanup():
    terminals.start()
    yield
    terminals.shutdown()
    assert not terminals.active()
    terminals._sessions.clear()


@pytest.fixture
def project(db, tmp_path):
    folder = tmp_path / 'terminal root 中文'
    folder.mkdir(); (folder / 'src').mkdir()
    item = workspace.register(db, str(folder)); db.commit()
    return folder, workspace.project_scope(item)


def spawn(project, **values):
    request = CreateTerminal(request_id='terminal_001', **values)
    item = terminals.create(project[1], request)
    return terminals.get(project[1]['project_id'], item['id'])


def output_until(session, expected):
    text, sequence = '', 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        _, _, chunks, _ = session.snapshot(sequence)
        for sequence, value in chunks:
            text += value
            if '\x1b[6n' in value:
                session.enqueue('input', '\x1b[1;1R')
        if expected in text:
            return text
        time.sleep(.02)
    raise AssertionError(f'No {expected!r} in terminal output: {text!r}; {session.info()}')


def wait_for(fn):
    deadline = time.monotonic() + 15
    while not fn():
        assert time.monotonic() < deadline
        time.sleep(.03)


@pytest.mark.parametrize('shell', ['powershell', 'cmd'])
def test_real_interactive_shell_and_scoped_start(project, shell):
    session = spawn(project, shell=shell, path='src')
    output_until(session, '>' if shell == 'cmd' else 'PS ')
    command = 'echo TERMINAL_READY_123' if shell == 'cmd' else "[Console]::WriteLine('TERMINAL_READY_123')"
    session.enqueue('input', command + '\r')
    text = output_until(session, 'TERMINAL_READY_123')
    assert str(project[0] / 'src') in session.info()['cwd']
    assert 'TERMINAL_READY_123' in text
    session.enqueue('resize', (132, 31))
    wait_for(lambda: session.info()['cols'] == 132)
    assert session.info()['rows'] == 31
    session.enqueue('input', 'exit 7\r')
    wait_for(lambda: session.status != 'running' and not session.worker.is_alive())
    assert session.exit_code == 7 and not session.worker.is_alive()


def test_input_reaches_program_and_unicode_output(project):
    session = spawn(project)
    output_until(session, 'PS ')
    # A file proves actual execution rather than merely seeing terminal echo.
    session.enqueue('input', "[IO.File]::WriteAllText((Join-Path $PWD '中文.txt'), '中文 😀'); [Console]::WriteLine('EXECUTED_' + 'DONE')\r")
    output_until(session, 'EXECUTED_DONE')
    assert (project[0] / '中文.txt').read_text(encoding='utf-8') == '中文 😀'
    session.enqueue('input', "Get-Content -Encoding UTF8 '中文.txt'\r")
    assert '中文 😀' in output_until(session, '中文 😀')


def test_environment_does_not_expose_application_secrets(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'secret-not-for-shell')
    monkeypatch.setenv('SCHOLARFLOW_INSTANCE_TOKEN', 'internal-token')
    monkeypatch.setenv('PYWINPTY_BACKEND', '1')
    monkeypatch.setenv('NORMAL_USER_VALUE', 'retained')
    env = terminals.shell_environment()
    assert 'secret-not-for-shell' not in env and 'internal-token' not in env and 'PYWINPTY_BACKEND' not in env
    assert 'NORMAL_USER_VALUE=retained' in env and 'TERM=xterm-256color' in env


def test_idempotent_creation_scope_and_limit(project):
    first = spawn(project)
    assert spawn(project).id == first.id
    with pytest.raises(HTTPException, match='409'):
        spawn(project, shell='cmd')
    with pytest.raises(HTTPException) as error:
        terminals.get('another-project', first.id)
    assert error.value.status_code == 404
    for index in range(3):
        terminals.create(project[1], CreateTerminal(request_id=f'terminal_{index + 2:03}'))
    with pytest.raises(HTTPException) as error:
        terminals.create(project[1], CreateTerminal(request_id='terminal_overflow'))
    assert error.value.status_code == 409


def test_replaced_or_escaping_directory_never_starts(project):
    for path in ('../escape', 'D:/absolute', '.git'):
        with pytest.raises((HTTPException, ValueError)):
            spawn(project, path=path)
    original = project[0]
    original.rename(original.with_name('moved'))
    original.mkdir()
    with pytest.raises(HTTPException) as error:
        spawn(project)
    assert error.value.status_code == 409 and not terminals.active()


def test_output_ring_and_attachment_are_bounded(project, monkeypatch):
    session = spawn(project)
    monkeypatch.setattr(terminals, 'OUTPUT_CHARS', 8192)
    session.append('a' * 20000)
    reset, base, chunks, _ = session.snapshot(0)
    assert reset and base > 0 and session.output_size <= 8192
    assert len(chunks) <= 4
    with pytest.raises(HTTPException):
        session.attach('incorrect')
    session.attach(session.token)
    with pytest.raises(HTTPException) as error:
        session.attach(session.token)
    assert error.value.status_code == 409
    session.detach(); session.attach(session.token); session.detach()


def test_disconnected_timeout_and_stopping_admission(project, monkeypatch):
    session = spawn(project)
    monkeypatch.setattr(terminals, 'DETACH_SECONDS', .1)
    wait_for(lambda: session.status != 'running')
    assert session.error and not session.worker.is_alive()
    terminals.shutdown()
    with pytest.raises(HTTPException) as error:
        terminals.create(project[1], CreateTerminal(request_id='after_shutdown'))
    assert error.value.status_code == 503


def test_http_origin_validation_and_lost_response_retry(project):
    client = TestClient(app, base_url='http://127.0.0.1')
    base = f"/api/projects/{project[1]['project_id']}/terminals"
    request = {'request_id': 'http_terminal_001', 'shell': 'cmd'}
    assert client.post(base, json=request, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert client.post(base, json=request, headers={'Host': 'evil.invalid'}).status_code == 403
    response = client.post(base, json=request, headers={'Origin': 'http://127.0.0.1'})
    assert response.status_code == 200, response.text
    item = response.json()
    assert client.post(base, json=request).json()['id'] == item['id']
    assert len(client.get(base).json()['sessions']) == 1
    assert 'token' not in client.get(base).text
    assert client.post(base, json={**request, 'rows': 0}).status_code == 422
    token = client.post(base + f"/{item['id']}/connect").json()['token']
    assert len(token) > 32
    assert client.post(base + f"/{item['id']}/stop").status_code == 200
    assert client.delete(base + f"/{item['id']}").status_code == 200
    assert client.get(base).json()['sessions'] == []


def test_websocket_auth_input_ack_resize_and_duplicate_connection(project):
    client = TestClient(app, base_url='http://127.0.0.1')
    session = spawn(project, shell='cmd')
    base = f"ws://127.0.0.1/api/projects/{session.project_id}/terminals/{session.id}/stream"
    with client.websocket_connect(base) as ws:
        ws.send_json({'token': 'bad'})
        assert ws.receive_json()['type'] == 'error'
    with client.websocket_connect(base, headers={'Origin': 'http://127.0.0.1'}) as ws:
        ws.send_json({'token': session.token})
        ws.send_json({'type': 'input', 'data': 'echo WEBSOCKET_' + 'DONE\r'})
        text = ''
        while 'WEBSOCKET_DONE' not in text:
            message = ws.receive_json()
            if message['type'] == 'output':
                text += message['data']
                ws.send_json({'type': 'ack', 'sequence': message['sequence']})
                if '\x1b[6n' in message['data']:
                    ws.send_json({'type': 'input', 'data': '\x1b[1;1R'})
        with client.websocket_connect(base) as second:
            second.send_json({'token': session.token})
            assert '另一窗口' in second.receive_json()['message']
        ws.send_json({'type': 'resize', 'cols': 125, 'rows': 22})
        wait_for(lambda: session.cols == 125)
        ws.send_json({'type': 'input', 'data': 'x' * 4097})
        while ws.receive_json()['type'] != 'error':
            pass
    wait_for(lambda: not session.attached)
    assert session.status == 'running'


def test_replay_boundary_commits_parser_reply_once_and_blocks_early_input(project, monkeypatch):
    session = spawn(project, shell='cmd')
    output_until(session, '>')
    replies = []
    monkeypatch.setattr(session, 'enqueue', lambda kind, value: replies.append((kind, value)))
    client = TestClient(app, base_url='http://127.0.0.1')
    url = f'ws://127.0.0.1/api/projects/{session.project_id}/terminals/{session.id}/stream'
    with client.websocket_connect(url) as ws:
        ws.send_json({'token': session.token})
        assert ws.receive_json() == {'type': 'replay', 'sequence': 0}
        while True:
            message = ws.receive_json()
            if message['type'] == 'output':
                sequence = message['sequence']
                ws.send_json({'type': 'ack', 'sequence': sequence, 'response': '\x1b[?1;2c'})
                wait_for(lambda: session.acknowledged == sequence)
                break
    wait_for(lambda: not session.attached)
    assert replies == [('input', '\x1b[?1;2c')]
    with client.websocket_connect(url) as ws:
        ws.send_json({'token': session.token})
        assert ws.receive_json() == {'type': 'replay', 'sequence': sequence}
        while True:
            message = ws.receive_json()
            if message['type'] == 'output':
                assert message['sequence'] <= sequence
                ws.send_json({'type': 'ack', 'sequence': message['sequence']})
            elif message['type'] == 'ready':
                break
        assert replies == [('input', '\x1b[?1;2c')]
        ws.send_json({'type': 'ack', 'sequence': sequence, 'response': 'duplicate'})
        while ws.receive_json()['type'] != 'error':
            pass
        assert replies == [('input', '\x1b[?1;2c')]
    wait_for(lambda: not session.attached)
    with client.websocket_connect(url) as ws:
        ws.send_json({'token': session.token})
        assert ws.receive_json()['type'] == 'replay'
        ws.send_json({'type': 'input', 'data': 'too early'})
        while ws.receive_json()['type'] != 'error':
            pass
        assert replies == [('input', '\x1b[?1;2c')]


def test_descendants_terminate_when_backend_crashes(tmp_path):
    root = tmp_path / 'crash'; root.mkdir()
    code = r'''
import sys, time
from pathlib import Path
from types import SimpleNamespace
from app import terminal_sessions as t, workspace
root = Path(sys.argv[1])
scope = {'project_id':'crash_project', 'root_path':str(root), 'root_identity':workspace.identity(root)}
item = t.create(scope, SimpleNamespace(request_id='crash_request',shell='powershell',path='',rows=20,cols=100))
s = t.get('crash_project', item['id'])
seen=0
while True:
    _,_,chunks,_=s.snapshot(seen)
    for seen,text in chunks:
        if '\x1b[6n' in text: s.enqueue('input','\x1b[1;1R')
        if 'PS ' in text:
            s.enqueue('input', "$p=Start-Process -FilePath ($PSHOME+'\powershell.exe') -ArgumentList '-NoProfile','-Command','Start-Sleep 120' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText((Join-Path $PWD 'descendant.pid'), [string]$p.Id)\r")
    if (root/'descendant.pid').exists():
        (root/'shell.pid').write_text(str(s.pid)); break
    time.sleep(.02)
while True: time.sleep(.1)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(root)], creationflags=0x08000000)
    try:
        wait_for(lambda: (root / 'shell.pid').exists())
        shell_pid = int((root / 'shell.pid').read_text())
        child_pid = int((root / 'descendant.pid').read_text())
        import psutil
        assert psutil.pid_exists(shell_pid) and psutil.pid_exists(child_pid)
        process.kill(); process.wait(timeout=10)
        wait_for(lambda: not psutil.pid_exists(shell_pid) and not psutil.pid_exists(child_pid))
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=10)


def test_native_worker_crash_cannot_take_down_backend_or_leave_descendants(project):
    import psutil
    session = spawn(project)
    output_until(session, 'PS ')
    session.enqueue('input', "$p=Start-Process -FilePath ($PSHOME+'\\powershell.exe') -ArgumentList '-NoProfile','-Command','Start-Sleep 120' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText((Join-Path $PWD 'worker-child.pid'), [string]$p.Id)\r")
    marker = project[0] / 'worker-child.pid'
    wait_for(marker.exists)
    descendant = int(marker.read_text())
    helper_pid, shell_pid = session.process.pid, session.pid
    assert helper_pid != shell_pid and psutil.pid_exists(descendant)
    assert 'winpty._winpty' not in sys.modules, 'The HTTP backend must not load native PTY code'
    session.process.kill()
    wait_for(lambda: not session.worker.is_alive())
    assert session.status == 'exited' and '意外退出' in session.error
    wait_for(lambda: not any(psutil.pid_exists(pid) for pid in (helper_pid, shell_pid, descendant)))
    assert TestClient(app).get('/api/health').status_code == 200
    terminals.remove(session.project_id, session.id)
    replacement = spawn(project, shell='cmd')
    output_until(replacement, '>')
    replacement.enqueue('input', 'echo AFTER_' + 'CRASH\r')
    output_until(replacement, 'AFTER_CRASH')


def test_unresponsive_native_worker_is_bounded_and_reaped(project, monkeypatch):
    import psutil
    session = spawn(project, shell='cmd')
    output_until(session, '>')
    psutil.Process(session.process.pid).suspend()
    monkeypatch.setattr(terminals, 'WORKER_HEARTBEAT_SECONDS', .3)
    wait_for(lambda: not session.worker.is_alive())
    assert session.status == 'exited' and '无响应' in session.error
    wait_for(lambda: not psutil.pid_exists(session.pid))
    assert not session.reader.is_alive() and not session.writer.is_alive()
    assert TestClient(app).get('/api/health').status_code == 200


def test_worker_startup_timeout_reaps_process_and_does_not_register_session(project, monkeypatch):
    import psutil
    marker = project[0] / 'startup.pid'
    code = "import os,time; from pathlib import Path; Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(60)"
    monkeypatch.setattr(terminals, '_worker_command', lambda: [sys.executable, '-c', code])
    monkeypatch.setattr(terminals, 'WORKER_START_SECONDS', .5)
    with pytest.raises(HTTPException) as error:
        spawn(project)
    assert error.value.status_code == 503 and '启动超时' in error.value.detail
    assert not terminals._sessions and not terminals.active()
    assert marker.exists()
    wait_for(lambda: not psutil.pid_exists(int(marker.read_text())))
