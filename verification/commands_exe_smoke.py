"""Packaged command execution against a local deterministic HTTP model fixture.

No external model or credentials are used. The EXE's ordinary API client, agent,
approval, process containment, journal, output recall and restart paths run intact.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import psutil

root = Path(__file__).resolve().parents[1]
data = Path(tempfile.mkdtemp(prefix='command-exe-', dir=root / 'verification/runtime'))
folder = data / 'project 中文'; folder.mkdir()
model_calls = []


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not body.get('stream'):
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': {'content': '[]'}}]}).encode())
            return
        messages = body['messages']
        tools = [m for m in messages if m['role'] == 'tool']
        call = None
        if not tools:
            question = str(next(m['content'] for m in reversed(messages) if m['role'] == 'user'))
            command = "[IO.File]::AppendAllText((Join-Path $PWD 'actual.txt'), '中文 😀'); [Console]::Write('HEAD中文😀'+('中😀'*9000)+'TAIL中文😀')"
            if 'CRASH_COMMAND' in question:
                command = "$p=Start-Process -FilePath ($PSHOME+'\\powershell.exe') -ArgumentList '-NoProfile','-Command','Start-Sleep 120' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText((Join-Path $PWD 'child.pid'), [string]$p.Id); [Console]::WriteLine('CRASH_READY'); Start-Sleep 120"
            call = ('run_project_command', {'command': command, 'summary': 'Packaged command check', 'timeout_seconds': 600})
        elif len(tools) == 1:
            value = json.loads(tools[0]['content'])
            assert value.get('status') == 'succeeded' and value['exit_code'] == 0, value
            assert value['output_is_partial'] and value['output'].endswith('TAIL中文😀')
            call = ('read_project_command_output', {'command_id': value['command_id'], 'offset': 0})
        else:
            value = json.loads(tools[-1]['content'])
            assert value['output'].startswith('HEAD中文😀') and value['next_offset'] == 8000, value
            model_calls.append('recalled-original-output')
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        if call:
            name, args = call
            model_calls.append(name)
            delta = {'tool_calls': [{'index': 0, 'id': 'call-' + name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}
        else:
            delta = {'content': '命令与原始输出已核对。'}
        self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False) + '\n\ndata: [DONE]\n\n').encode())
        self.wfile.flush()


server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
environment = {**os.environ, 'DATA_DIR': str(data), 'LLM_API_KEY': '', 'EMBEDDING_API_KEY': '', 'REDIS_URL': '', 'DESKTOP_NOTIFICATIONS': 'false'}
exe = root / 'verification/desktop/ScholarFlow/ScholarFlow.exe'
process = None


def api(port, path, body=None, method=None, raw=False):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api' + path, data=None if body is None else json.dumps(body).encode(),
        method=method, headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=10) as response:
        content = response.read()
    return content.decode('utf-8') if raw else json.loads(content)


def until(fn, label):
    deadline = time.monotonic() + 35
    while True:
        value = fn()
        if value:
            return value
        assert time.monotonic() < deadline, label
        time.sleep(.08)


def launch():
    global process
    process = subprocess.Popen([str(exe)], cwd=exe.parent, env=environment, creationflags=0x08000000)
    def ready():
        assert process.poll() is None, process.returncode
        try:
            info = json.loads((data / '.runtime.json').read_text(encoding='utf-8'))
            port = info.get('port')
            if info.get('pid') == process.pid and isinstance(port, int) and port > 0 and api(port, '/health'):
                return port
        except Exception:
            return None
    return until(ready, 'EXE ready')


def stop():
    if process and process.poll() is None:
        process.kill(); process.wait(timeout=10)


try:
    first = launch()
    api(first, '/settings', {'llm_base_url': f'http://127.0.0.1:{server.server_port}/v1', 'llm_api_key': 'local-fixture-only',
        'llm_model': 'local-fixture', 'memory_recall': False, 'memory_capture': False, 'approval_mode': 'read_only'}, 'PUT')
    project = api(first, '/projects', {'path': str(folder)})
    def start(port, question):
        cid = api(port, '/conversations', {'title': question, 'project_id': project['id']})['id']
        task = api(port, '/tasks/start', {'question': question, 'conversation_id': cid, 'project_id': project['id']})
        item = until(lambda: (value if (value := api(port, '/tasks/' + task['id'])).get('approvals') else None), 'approval')
        approval = item['approvals'][0]
        command_id = approval['arguments']['command_id']
        assert api(port, '/project-commands/' + command_id)['status'] == 'proposed'
        api(port, '/approvals/decide', {'approval_id': approval['approval_id'], 'decision': 'approve', 'scope': 'once'})
        return task['id'], command_id
    task, saved_id = start(first, 'SHORT_COMMAND')
    until(lambda: api(first, '/tasks/' + task)['status'] == 'done', 'completed command and output recall')
    assert (folder / 'actual.txt').read_text(encoding='utf-8') == '中文 😀'
    assert 'recalled-original-output' in model_calls
    journal = api(first, '/project-commands/' + saved_id)
    assert journal['status'] == 'succeeded' and journal['exit_code'] == 0
    export = api(first, '/project-commands/' + saved_id + '/output', raw=True)
    assert 'HEAD中文😀' in export and 'TAIL中文😀' in export and 'CLIXML' not in export
    assert any(e.get('command_id') == saved_id for e in api(first, '/tasks/' + task)['events'])
    crash_task, crash_id = start(first, 'CRASH_COMMAND')
    crashed = until(lambda: (value if (value := api(first, '/project-commands/' + crash_id)).get('output', '').find('CRASH_READY') >= 0 else None), 'running output persisted')
    pids = [crashed['pid'], int((folder / 'child.pid').read_text())]
    assert all(psutil.pid_exists(pid) for pid in pids)
    stop()
    until(lambda: all(not psutil.pid_exists(pid) for pid in pids), 'command process tree ended with EXE')
    second = launch()
    interrupted = api(second, '/project-commands/' + crash_id)
    assert interrupted['status'] == 'interrupted' and 'CRASH_READY' in interrupted['output']
    assert api(second, '/project-commands/' + saved_id)['status'] == 'succeeded'
    assert api(second, f"/projects/{project['id']}/commands")['total'] == 2
    assert api(second, '/tasks/' + crash_task)['recovery']['pending_operation']['name'] == 'run_project_command'
    assert (folder / 'actual.txt').read_text(encoding='utf-8') == '中文 😀'
    print('PASS: packaged real API dispatch/approval/PowerShell execution, Unicode, exit status, output pagination/model recall/export, task event links, EXE-crash child containment and restart journal without replay')
    print('Scope: local deterministic HTTP model fixture; no external model quality or native-window interaction claim')
    print('Retained isolated verification data:', data)
finally:
    stop(); server.shutdown(); server.server_close()
