"""Packaged agent Git: deterministic local HTTP model, real EXE/approval/Git/restart."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request

root = Path(__file__).resolve().parents[1]
data = Path(tempfile.mkdtemp(prefix='agent-git-exe-', dir=root / 'verification/runtime'))
folder = data / '项目 中文'; folder.mkdir()


def git(*args):
    return subprocess.check_output(['git', '-C', str(folder), *args], encoding='utf-8', creationflags=0x08000000).strip()


git('init', '-b', 'main')
for key, value in [('user.name', 'Packaged Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
    git('config', key, value)
file = folder / '源文件.txt'; file.write_text('original\n', encoding='utf-8')
git('add', '.'); git('commit', '-m', 'Initial')
file.write_text('模型修改 中文 😀\n', encoding='utf-8')
results = []


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not body.get('stream'):
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': {'content': '[]'}}]}).encode())
            return
        messages = body['messages']; replies = [m for m in messages if m['role'] == 'tool']
        if not replies:
            question = str(next(m['content'] for m in reversed(messages) if m['role'] == 'user'))
            action = 'commit' if 'GIT_COMMIT' in question else 'unstage' if 'GIT_UNSTAGE' in question else 'stage'
            args = {'action': action, **({'message': '打包提交 中文 😀'} if action == 'commit' else {'paths': ['源文件.txt']})}
            delta = {'tool_calls': [{'index': 0, 'id': 'git-' + action, 'type': 'function',
                'function': {'name': 'git_project_write', 'arguments': json.dumps(args, ensure_ascii=False)}}]}
        else:
            results.append(json.loads(replies[-1]['content']))
            delta = {'content': 'Git 操作记录已保存。'}
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False) + '\n\ndata: [DONE]\n\n').encode())
        self.wfile.flush()


server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
environment = {**os.environ, 'DATA_DIR': str(data), 'LLM_API_KEY': '', 'EMBEDDING_API_KEY': '',
    'REDIS_URL': '', 'DESKTOP_NOTIFICATIONS': 'false'}
exe = root / 'verification/desktop/ScholarFlow/ScholarFlow.exe'
process = None


def api(port, path, body=None, method=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api' + path,
        data=None if body is None else json.dumps(body).encode(), method=method,
        headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=15) as response:
        return json.loads(response.read())


def until(fn, label):
    deadline = time.monotonic() + 40
    while True:
        result = fn()
        if result:
            return result
        assert time.monotonic() < deadline, label
        time.sleep(.1)


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
    port = launch()
    api(port, '/settings', {'llm_base_url': f'http://127.0.0.1:{server.server_port}/v1', 'llm_api_key': 'local-fixture-only',
        'llm_model': 'local-fixture', 'memory_recall': False, 'memory_capture': False, 'approval_mode': 'read_only'}, 'PUT')
    project = api(port, '/projects', {'path': str(folder)})
    operation_prefix = f"/projects/{project['id']}/git/operations/"
    def start(question, decision='approve'):
        cid = api(port, '/conversations', {'title': question, 'project_id': project['id']})['id']
        task = api(port, '/tasks/start', {'question': question, 'conversation_id': cid, 'project_id': project['id']})
        state = until(lambda: (value if (value := api(port, '/tasks/' + task['id'])).get('approvals') else None), 'approval')
        approval = state['approvals'][0]; operation_id = approval['arguments']['operation_id']
        operation = api(port, operation_prefix + operation_id)
        assert operation['status'] == 'prepared' and operation['agent_owned']
        assert '源文件.txt' in operation['patch'] and '中文 😀' in operation['patch']
        if decision:
            api(port, '/approvals/decide', {'approval_id': approval['approval_id'], 'decision': decision, 'scope': 'once'})
            until(lambda: api(port, '/tasks/' + task['id'])['status'] == 'done', 'task completed')
        return task['id'], operation_id, cid
    _, denied, _ = start('GIT_STAGE deny', 'deny')
    assert api(port, operation_prefix + denied)['status'] == 'cancelled' and not git('diff', '--cached')
    _, staged, _ = start('GIT_STAGE approve')
    assert api(port, operation_prefix + staged)['status'] == 'applied' and '+模型修改' in git('diff', '--cached')
    _, unstaged, _ = start('GIT_UNSTAGE')
    assert api(port, operation_prefix + unstaged)['status'] == 'applied' and not git('diff', '--cached')
    start('GIT_STAGE again')
    task, committed, cid = start('GIT_COMMIT')
    assert api(port, operation_prefix + committed)['status'] == 'applied'
    assert git('rev-list', '--count', 'HEAD') == '2' and git('log', '-1', '--format=%B') == '打包提交 中文 😀'
    assert any(event.get('operation_id') == committed for event in api(port, '/tasks/' + task)['events'])
    assert any(e.get('operation_id') == committed for e in api(port, '/conversations/' + cid)['messages'][0]['trace'])
    file.write_text('pending 中文 😀\n', encoding='utf-8')
    pending_task, pending, _ = start('GIT_STAGE restart', None)
    stop(); port = launch()
    assert api(port, operation_prefix + pending)['status'] == 'cancelled'
    assert api(port, operation_prefix + committed)['status'] == 'applied'
    assert api(port, '/tasks/' + pending_task)['status'] == 'interrupted'
    assert git('rev-list', '--count', 'HEAD') == '2' and not git('diff', '--cached')
    assert len(results) == 5 and all(value.get('operation_id') for value in results)
    print('PASS: packaged model Git stage/unstage/commit, deny, Unicode diff/result, real index/HEAD, task/chat links, pending-permission cancellation and restart without replay')
    print('Scope: local deterministic HTTP model; no native-window interaction or real-model quality claim')
    print('Retained isolated verification data:', data)
finally:
    stop(); server.shutdown(); server.server_close()
