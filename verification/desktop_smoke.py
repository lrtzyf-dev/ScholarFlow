"""Verify the packaged runtime with disposable offline data, without UI input."""
import json
import hashlib
import os
import socket
import sqlite3
import subprocess
import shutil
import stat
import tempfile
import time
import psutil
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request
from pathlib import Path
from websockets.sync.client import connect as websocket_connect

root = Path(__file__).resolve().parents[1]
executable = root / "verification" / "desktop" / "ScholarFlow" / "ScholarFlow.exe"
data = Path(tempfile.mkdtemp(prefix="scholarflow-desktop-smoke-"))

# Exercise migration of a database created before usage-origin IDs and tombstones.
db_path = data / "scholarflow.db"
if not db_path.exists():
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE qa_records (
            id VARCHAR(32) PRIMARY KEY, conversation_id VARCHAR(32), paper_id VARCHAR(32),
            question TEXT, answer TEXT, evidence JSON, claims JSON, trace JSON, skills JSON,
            refusal INTEGER, latency_ms INTEGER, in_memory INTEGER, usage JSON,
            created_at DATETIME
        )""")
        db.execute("CREATE TABLE desktop_state (key VARCHAR(128) PRIMARY KEY, revision INTEGER, value JSON)")
        db.execute("""CREATE TABLE conversations (
            id VARCHAR(32) PRIMARY KEY, title VARCHAR(512), created_at DATETIME, updated_at DATETIME
        )""")
        db.execute("""INSERT INTO qa_records (id, conversation_id, question, answer, created_at)
            VALUES ('qa_legacy_read', 'c_legacy_read', 'Legacy question', 'Legacy answer', CURRENT_TIMESTAMP)""")
    db.close()

environment = dict(os.environ)
environment.update({"DATA_DIR": str(data), "LLM_API_KEY": "", "EMBEDDING_API_KEY": "",
                    "REDIS_URL": "", "DESKTOP_NOTIFICATIONS": "false", "MOCK_STREAM_DELAY": "0.05"})
startupinfo = subprocess.STARTUPINFO()
startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startupinfo.wShowWindow = 0


def listening_port(pid):
    # The bundled application starts one local HTTP server on a selected port.
    output = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True)
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "TCP" and parts[-1] == str(pid):
            if parts[2] == "0.0.0.0:0" and parts[1].startswith("127.0.0.1:"):
                return int(parts[1].rsplit(":", 1)[1])
    return None


def request(port, path, body=None, method=None):
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=raw, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read()


def launch(target_environment=None):
    process = subprocess.Popen([str(executable)], cwd=executable.parent, env=target_environment or environment,
                               startupinfo=startupinfo)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"packaged process exited early: {process.returncode}")
        port = listening_port(process.pid)
        if port:
            try:
                health = json.loads(request(port, "/api/health"))
                assert health["llm_mode"] == "mock"
                assert b'/assets/' in request(port, "/chat")
                return process, port
            except (OSError, ValueError):
                pass
        time.sleep(0.2)
    process.terminate()
    process.wait(timeout=10)
    raise AssertionError("packaged HTTP server did not become ready")


def cleanup(process, port):
    process.terminate()
    process.wait(timeout=10)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0, "test process left its port open"


def wait_task(port, task_id, predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        task = json.loads(request(port, f"/api/tasks/{task_id}"))
        if predicate(task):
            return task
        time.sleep(0.01)
    raise AssertionError("packaged task did not reach expected state")


def verify_terminal(port, project):
    base = f"/api/projects/{project['id']}/terminals"
    body = {'request_id': 'packaged_terminal_001', 'shell': 'powershell'}
    terminal = json.loads(request(port, base, body))
    assert json.loads(request(port, base, body))['id'] == terminal['id']
    token = json.loads(request(port, base + f"/{terminal['id']}/connect", {}))['token']
    def read_until(ws, marker):
        text = ''; deadline = time.monotonic() + 20
        while marker not in text:
            message = json.loads(ws.recv(timeout=max(.1, deadline - time.monotonic())))
            assert message['type'] != 'error', message
            if message['type'] == 'output':
                text += message['data']
                response = '\x1b[1;1R' if '\x1b[6n' in message['data'] else ''
                ws.send(json.dumps({'type': 'ack', 'sequence': message['sequence'], 'response': response}))
        return text
    with websocket_connect(f"ws://127.0.0.1:{port}{base}/{terminal['id']}/stream",
                           origin=f'http://127.0.0.1:{port}', proxy=None) as ws:
        ws.send(json.dumps({'token': token}))
        read_until(ws, 'PS ')
        command = "[IO.File]::WriteAllText((Join-Path $PWD 'terminal-executed.txt'), '中文 😀'); [Console]::WriteLine('PACKAGED_'+'TERMINAL')\r"
        ws.send(json.dumps({'type': 'input', 'data': command}))
        read_until(ws, 'PACKAGED_TERMINAL')
        assert (Path(project['root_path']) / 'terminal-executed.txt').read_text(encoding='utf-8') == '中文 😀'
        ws.send(json.dumps({'type': 'resize', 'cols': 135, 'rows': 28}))
        command = "$p=Start-Process -FilePath ($PSHOME+'\\powershell.exe') -ArgumentList '-NoProfile','-Command','Start-Sleep 120' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText((Join-Path $PWD 'terminal-child.pid'), [string]$p.Id); [Console]::WriteLine('CHILD_'+'READY')\r"
        ws.send(json.dumps({'type': 'input', 'data': command}))
        read_until(ws, 'CHILD_READY')
    current = json.loads(request(port, base))['sessions'][0]
    assert current['cols'] == 135 and current['rows'] == 28 and current['pid'] == terminal['pid']
    child_pid = int((Path(project['root_path']) / 'terminal-child.pid').read_text())
    assert psutil.pid_exists(terminal['pid']) and psutil.pid_exists(child_pid)
    return terminal['pid'], child_pid


def verify_duplicate_launch(primary, port):
    descriptor = json.loads((data / '.runtime.json').read_text(encoding='utf-8'))
    assert descriptor['pid'] == primary.pid and descriptor['port'] == port and descriptor['kind'] == 'desktop'
    try:
        request(port, '/api/runtime/activate', {})
    except urllib.error.HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError('packaged activation accepted a missing credential')
    cid = json.loads(request(port, '/api/conversations', {'title': '[Verification] Ownership'}))['id']
    policy = data / 'mcp_tool_policy.json'
    policy.write_text(json.dumps({'tool_rules': {'retrieve_evidence': {'approval': 'always'}}}), encoding='utf-8')
    task = json.loads(request(port, '/api/tasks/start', {'question': 'Packaged ownership', 'conversation_id': cid}))
    waiting = wait_task(port, task['id'], lambda item: bool(item['approvals']) and
                        any(event['kind'] == 'approval_request' for event in item['events']))
    duplicate = subprocess.Popen([str(executable)], cwd=executable.parent, env=environment, startupinfo=startupinfo)
    try:
        duplicate.wait(timeout=40)
        assert duplicate.returncode == 0, 'second desktop did not activate the existing instance'
        assert primary.poll() is None and listening_port(primary.pid) == port
        assert listening_port(duplicate.pid) is None
        assert json.loads((data / '.runtime.json').read_text(encoding='utf-8')) == descriptor
        unchanged = json.loads(request(port, f'/api/tasks/{task["id"]}'))
        assert unchanged['status'] == 'running' and unchanged['executing']
        assert unchanged['events'] == waiting['events'], (waiting['events'], unchanged['events'])
        assert unchanged['approvals'] == waiting['approvals'], (waiting['approvals'], unchanged['approvals'])
        independent, independent_port = launch({**environment, 'DATA_DIR': str(data / 'independent')})
        try:
            assert independent_port != port and primary.poll() is None
            assert json.loads(request(independent_port, '/api/tasks'))['tasks'] == []
        finally:
            cleanup(independent, independent_port)
    finally:
        if duplicate.poll() is None:
            duplicate.terminate()
            duplicate.wait(timeout=10)
        policy.unlink(missing_ok=True)
        request(port, f'/api/tasks/{task["id"]}/cancel', {})
        wait_task(port, task['id'], lambda item: item['status'] == 'cancelled' and not item['executing'])
        request(port, f'/api/conversations/{cid}', method='DELETE')
    return descriptor['instance_id']


first, port = launch()
try:
    rg_dir = executable.parent / '_internal' / 'vendor' / 'ripgrep'
    rg_manifest = json.loads((rg_dir / 'manifest.json').read_text(encoding='utf-8'))
    with (rg_dir / 'rg.exe').open('rb') as binary:
        assert hashlib.file_digest(binary, 'sha256').hexdigest() == rg_manifest['executable_sha256']
    rg_version = subprocess.check_output([str(rg_dir / 'rg.exe'), '--version'], text=True, startupinfo=startupinfo)
    assert rg_version.startswith('ripgrep ' + rg_manifest['version'])
    assert (rg_dir / 'LICENSE-MIT').is_file()
    frontend = root / 'frontend' / 'dist'
    assert request(port, '/chat') == (frontend / 'index.html').read_bytes()
    static_count = 0
    for asset in (frontend / 'assets').iterdir():
        if asset.is_file():
            assert request(port, '/assets/' + asset.name) == asset.read_bytes(), f'Packaged asset differs: {asset.name}'
            static_count += 1
    assert static_count > 0 and any((frontend / 'assets').glob('KaTeX_Main-Regular-*.woff2'))
    project_root = data / 'project-files'
    project_root.mkdir()
    (project_root / 'notes.md').write_text('Packaged project context survives restart.', encoding='utf-8')
    search_dir = project_root / 'search-src'
    search_dir.mkdir()
    search_file = search_dir / 'main.txt'
    search_text = 'PackagedSearch first\nPackagedSearch second\n'
    search_file.write_text(search_text, encoding='utf-8')
    def git(*arguments):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *arguments],
                              cwd=project_root, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Verification')
    git('config', 'user.email', 'verification@example.invalid')
    git('config', 'commit.gpgsign', 'false')
    git('add', '--', 'notes.md')
    git('-c', 'user.name=Verification', '-c', 'user.email=verification@example.invalid', 'commit', '-m', 'Initial project')
    project = json.loads(request(port, '/api/projects', {'path': str(project_root)}))
    search_path = f"/api/projects/{project['id']}/search"
    search_options = {'query': 'PackagedSearch', 'path': 'search-src', 'include': '**/*.txt', 'case_sensitive': True}
    search_page = json.loads(request(port, search_path, search_options))
    assert search_page['total'] == 2 and search_page['files'] == 1 and not search_page['limited']
    assert [hit['line'] for hit in search_page['matches']] == [1, 2]
    search_hit = search_page['matches'][0]
    assert search_hit['path'] == 'search-src/main.txt'
    preview_path = f"/api/projects/{project['id']}/file?path={search_hit['path']}&sha256={search_hit['sha256']}"
    assert 'PackagedSearch first' in json.loads(request(port, preview_path))['text']
    search_draft_path = f"/api/desktop-state/projects/{project['id']}/search"
    saved_search = json.loads(request(port, search_draft_path, {'revision': 0, 'value': search_options}, method='PUT'))
    project_cid = json.loads(request(port, '/api/conversations', {'title': 'Packaged project', 'project_id': project['id']}))['id']
    request(port, f"/api/projects/{project['id']}/init", {})
    git_base = f"/api/projects/{project['id']}/git"
    git_prepared = json.loads(request(port, git_base + '/operations', {
        'request_id': 'packaged_git_001', 'action': 'stage', 'paths': ['AGENTS.md']}))
    git_draft_path = f"/api/desktop-state/projects/{project['id']}/git"
    request(port, git_draft_path, {'revision': 0, 'value': {'message': 'Packaged commit'}}, method='PUT')
    project_draft_path = f'/api/desktop-state/conversations/{project_cid}'
    request(port, project_draft_path, {'revision': 0, 'value': {'text': 'Next project question', 'file_paths': ['notes.md']}}, method='PUT')
    project_request = {'id': 'chat_packaged_project', 'question': 'Plan using project notes', 'conversation_id': project_cid,
                       'execution_mode': 'plan', 'project_id': project['id'], 'file_paths': ['notes.md']}
    request(port, '/api/foreground-runs', project_request)
    assert b'event: workspace' in request(port, '/api/chat/stream', {**project_request, 'run_id': project_request['id']})
    project_message = json.loads(request(port, f'/api/conversations/{project_cid}'))['messages'][0]
    assert project_message['file_paths'] == ['notes.md']
    assert any(event.get('kind') == 'workspace' and event['rules'] == ['AGENTS.md'] for event in project_message['trace'])
    terminal_pid, terminal_child_pid = verify_terminal(port, project)
    first_instance = verify_duplicate_launch(first, port)
    legacy_item = next(item for item in json.loads(request(port, "/api/conversations/page"))["conversations"] if item["id"] == "c_legacy_read")
    assert legacy_item["unread_count"] == 0
    searched = json.loads(request(port, "/api/conversations/page?q=Legacy%20answer&limit=1"))
    assert searched["total"] == 1 and searched["conversations"][0]["match_record_id"] == "qa_legacy_read"
    assert json.loads(request(port, "/api/conversations/c_legacy_read/activity"))["message_count"] == 1
    assert "conversations" not in json.loads(request(port, "/api/tasks?conversation_mode=signal"))
    request(port, "/api/conversations/c_legacy_read", {"title": "Legacy question"}, method="PUT")
    # More than one page verifies that the bundled code is the paged backend.
    for index in range(42):
        request(port, "/api/conversations", {"title": f"[Search verification] {index:02}"})
    first_page = json.loads(request(port, "/api/conversations/page?q=Search%20verification"))
    assert first_page["total"] == 42 and len(first_page["conversations"]) == 40
    more_page = json.loads(request(port, "/api/conversations/page?q=Search%20verification&cursor=" + first_page["next_cursor"]))
    assert len(more_page["conversations"]) == 2 and more_page["next_cursor"] is None
    with sqlite3.connect(db_path) as db:
        db.execute("""INSERT INTO qa_records (id, conversation_id, question, answer, created_at)
            VALUES ('qa_packaged_receipt', 'c_packaged_receipt', 'New question', 'New answer', CURRENT_TIMESTAMP)""")
    db.close()
    receipt_item = next(item for item in json.loads(request(port, "/api/conversations"))["conversations"] if item["id"] == "c_packaged_receipt")
    assert receipt_item["unread_count"] == 1
    assert json.loads(request(port, "/api/conversations/c_packaged_receipt/read", {"message_ids": ["qa_packaged_receipt"]}))["message_ids"] == ["qa_packaged_receipt"]
    created = json.loads(request(port, "/api/conversations", {"title": "[Verification] Packaged restart"}))
    cid = created["id"]
    draft_id = f"c_packaged_{time.time_ns():x}"
    draft_path = f"/api/desktop-state/conversations/{draft_id}"
    saved_draft = json.loads(request(port, draft_path, {"revision": 0, "value": {
        "text": "Packaged draft across origins", "paper_id": None, "skills": ["research"],
        "execution_mode": "plan",
        "images": [{"media_type": "image/png", "data": "YWJj"}],
        "reading": {"following": False, "top": 400, "anchor_id": "qa_old", "offset": 20},
    }}, method="PUT"))
    selected = json.loads(request(port, "/api/desktop-state/selection"))
    request(port, "/api/desktop-state/selection", {"revision": selected["revision"],
            "value": {"conversation_id": draft_id}}, method="PUT")
    pinned = json.loads(request(port, f"/api/conversations/{draft_id}/pin", {"pinned": True}, method="PUT"))
    assert pinned["pinned_at"]
    request(port, f"/api/conversations/{cid}/pin", {"pinned": True}, method="PUT")
    order_request = {"conversation_ids": [draft_id, cid], "expected_order": [cid, draft_id]}
    assert json.loads(request(port, "/api/conversations/pins/order", order_request, method="PUT"))["conversation_ids"] == [draft_id, cid]
    assert json.loads(request(port, "/api/conversations/pins/order", order_request, method="PUT"))["conversation_ids"] == [draft_id, cid]
    request(port, "/api/desktop-state/sidebar", {"revision": 0, "value": {"sort": "created_at"}}, method="PUT")
    task = json.loads(request(port, "/api/tasks/start", {
        "question": "Packaged checkpoint verification", "conversation_id": cid}))
    wait_task(port, task['id'], lambda item: item['status'] == 'running' and
              item.get('recovery', {}).get('completed_tools', 0) >= 1)
    request(port, f"/api/tasks/{task['id']}/cancel", {})
    stopped = wait_task(port, task['id'], lambda item: item['status'] == 'cancelled')
    assert stopped['recovery']['available']
    foreground_id = "chat_packaged_12345678"
    foreground = json.loads(request(port, "/api/foreground-runs", {
        "id": foreground_id, "conversation_id": cid,
        "question": "Packaged foreground recovery", "paper_id": None,
        "execution_mode": "plan",
        "skills": [], "images": [],
    }, method="POST"))
    assert foreground["status"] == "running"
    checkpoint = json.loads(request(port, f"/api/foreground-runs/{foreground_id}", {
        "revision": foreground["revision"], "phase": "answer",
        "partial_answer": "Saved packaged partial answer",
    }, method="PATCH"))
    assert checkpoint["partial_answer"] == "Saved packaged partial answer"
    plan_cid = json.loads(request(port, "/api/conversations", {"title": "Packaged reviewed plan"}))["id"]
    plan_stream = request(port, "/api/chat/stream", {
        "question": "Plan a verified report", "conversation_id": plan_cid, "execution_mode": "plan",
        "run_id": "chat_packaged_plan_generation",
    }).decode()
    assert 'event: answer' in plan_stream and '"execution_mode": "plan"' in plan_stream
    plan_record = json.loads(request(port, f"/api/conversations/{plan_cid}"))["messages"][0]
    assert plan_record["execution_mode"] == "plan" and not plan_record["plan_run_id"]
    assert all(step['status'] == 'pending' for step in plan_record['plan_steps'])
    plan_execution_id = "chat_packaged_reviewed_first"
    request(port, "/api/foreground-runs", {
        "id": plan_execution_id, "conversation_id": plan_cid, "question": "Execute reviewed plan",
        "reviewed_plan_id": plan_record["id"],
    })
    try:
        request(port, "/api/tasks/start", {"question": "Duplicate plan", "conversation_id": plan_cid, "reviewed_plan_id": plan_record["id"]})
    except urllib.error.HTTPError as error:
        assert error.code == 409
    else:
        raise AssertionError("packaged plan was claimed twice")
    try:
        request(port, f"/api/conversations/{cid}/archive", {"archived": True, "expected_revision": 0}, method="PUT")
    except urllib.error.HTTPError as error:
        assert error.code == 409
    else:
        raise AssertionError("packaged backend archived an active foreground run")
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(qa_records)")}
        assert "usage_origin_id" in columns
        assert "read_at" in columns
        assert {"execution_mode", "plan_run_id", "plan_steps", "reviewed_plan_id"} <= columns
        assert db.execute("SELECT name FROM sqlite_master WHERE name='deleted_conversations'").fetchone()
        assert 'checkpoint' in {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
        assert 'updated_at' in {row[1] for row in db.execute('PRAGMA table_info(desktop_state)')}
        assert 'pinned_at' in {row[1] for row in db.execute('PRAGMA table_info(conversations)')}
        assert 'pin_position' in {row[1] for row in db.execute('PRAGMA table_info(conversations)')}
        assert {'archived_at', 'archive_revision'} <= {row[1] for row in db.execute('PRAGMA table_info(conversations)')}
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=30)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
        db.execute("UPDATE foreground_runs SET heartbeat_at = ? WHERE id = ?", (stale_heartbeat, foreground_id))
        db.execute("UPDATE foreground_runs SET heartbeat_at = ? WHERE id = ?", (stale_heartbeat, plan_execution_id))
    db.close()
    archive_path = "/api/conversations/c_legacy_read/archive"
    archive_draft_path = "/api/desktop-state/conversations/c_legacy_read"
    request(port, archive_draft_path, {"revision": 0, "value": {"text": "Archived draft survives restart"}}, method="PUT")
    archive_request = {"archived": True, "expected_revision": 0}
    archived = json.loads(request(port, archive_path, archive_request, method="PUT"))
    assert archived["archived_at"] and archived["archive_revision"] == 1
    assert json.loads(request(port, archive_path, archive_request, method="PUT")) == archived
    memory_cid = json.loads(request(port, '/api/conversations', {'title': '[Verification] Persistent long context'}))['id']
    with sqlite3.connect(db_path) as db:
        for index in range(18):
            db.execute('INSERT INTO qa_records (id, conversation_id, question, answer, in_memory, created_at) VALUES (?, ?, ?, ?, 1, ?)',
                       (f'qa_packaged_memory_{index:02}', memory_cid, f'Constraint {index}',
                        f'Original value {index + 0.125} mg. Reference [^e1].',
                        (datetime(2026, 1, 1) + timedelta(minutes=index)).isoformat()))
    db.close()
    persisted_memory = json.loads(request(port, f'/api/conversations/{memory_cid}/memory/compact', {'expected_revision': 0}))
    assert persisted_memory['covered_turns'] == 14 and persisted_memory['recent_turns'] == 4
    assert '0.125 mg' in persisted_memory['summary'] and persisted_memory['mode'] == 'extractive'
    assert persisted_memory['usage']['calls'] == 0
    assert len(json.loads(request(port, f'/api/conversations/{memory_cid}'))['messages']) == 18
finally:
    cleanup(first, port)

second, port2 = launch()
try:
    assert not psutil.pid_exists(terminal_pid) and not psutil.pid_exists(terminal_child_pid)
    assert json.loads(request(port2, f"/api/projects/{project['id']}/terminals"))['sessions'] == []
    restored_search = json.loads(request(port2, search_draft_path))
    assert restored_search == saved_search
    assert json.loads(request(port2, search_path, search_options))['snapshot'] == search_page['snapshot']
    search_file.write_text(search_text + 'PackagedSearch external change\n', encoding='utf-8')
    for endpoint, body in [(preview_path, None), (search_path, {**search_options, 'offset': 1, 'snapshot': search_page['snapshot']})]:
        try:
            request(port2, endpoint, body)
        except urllib.error.HTTPError as error:
            assert error.code == 409
        else:
            raise AssertionError('packaged search accepted stale file content or results')
    assert json.loads(request(port2, search_path, search_options))['total'] == 3
    assert json.loads(request(port2, f'/api/conversations/{project_cid}/project'))['project']['id'] == project['id']
    assert json.loads(request(port2, project_draft_path))['value']['file_paths'] == ['notes.md']
    assert json.loads(request(port2, f"/api/projects/{project['id']}/file?path=notes.md"))['text'] == 'Packaged project context survives restart.'
    assert json.loads(request(port2, f'/api/conversations/{project_cid}'))['messages'][0]['file_paths'] == ['notes.md']
    try:
        request(port2, f"/api/projects/{project['id']}/file?path=../scholarflow.db")
    except urllib.error.HTTPError as error:
        assert error.code == 400
    else:
        raise AssertionError('packaged project reader allowed path traversal')
    project_task = json.loads(request(port2, '/api/tasks/start', {'question': 'Plan next project step', 'conversation_id': project_cid,
                             'project_id': project['id'], 'file_paths': ['notes.md'], 'execution_mode': 'plan'}))
    wait_task(port2, project_task['id'], lambda item: item['status'] == 'done' and not item['executing'])
    assert json.loads(request(port2, f'/api/conversations/{project_cid}'))['messages'][-1]['file_paths'] == ['notes.md']
    original = json.loads(request(port2, f"/api/projects/{project['id']}/editable-file?path=notes.md"))
    change = json.loads(request(port2, f"/api/projects/{project['id']}/changes", {
        'conversation_id': project_cid, 'request_id': 'packaged_edit_001', 'edit': {
            'path': 'notes.md', 'operation': 'update', 'expected_sha256': original['sha256'],
            'content': 'Packaged file edit works.', 'summary': 'Packaged edit verification'}}))
    detail = json.loads(request(port2, f"/api/project-changes/{change['id']}"))
    assert detail['diff']['added'] == 1 and detail['status'] == 'proposed'
    request(port2, f"/api/project-changes/{change['id']}/apply", {'revision': detail['revision']})
    assert (project_root / 'notes.md').read_text(encoding='utf-8') == 'Packaged file edit works.'
    reverse = json.loads(request(port2, f"/api/project-changes/{change['id']}/undo", {'conversation_id': project_cid}))
    request(port2, f"/api/project-changes/{reverse['id']}/apply", {'revision': reverse['revision']})
    assert (project_root / 'notes.md').read_text(encoding='utf-8') == original['content']
    created = json.loads(request(port2, f"/api/projects/{project['id']}/changes", {
        'conversation_id': project_cid, 'request_id': 'packaged_edit_002', 'edit': {
            'path': 'nested/new.txt', 'operation': 'create', 'content': 'new file'}}))
    request(port2, f"/api/project-changes/{created['id']}/apply", {'revision': created['revision']})
    assert (project_root / 'nested' / 'new.txt').read_text(encoding='utf-8') == 'new file'
    assert len(json.loads(request(port2, f"/api/projects/{project['id']}/changes"))['changes']) >= 3
    status = json.loads(request(port2, f"/api/projects/{project['id']}/git/status"))
    assert status['available'] and status['branch'] == 'main' and any(item['path'] == 'nested/new.txt' for item in status['entries'])
    (project_root / 'notes.md').write_text('Packaged Git diff.', encoding='utf-8')
    patch = json.loads(request(port2, f"/api/projects/{project['id']}/git/diff?path=notes.md&version=unstaged"))
    assert '+Packaged Git diff.' in patch['patch']
    assert json.loads(request(port2, f"/api/projects/{project['id']}/git/history"))['commits'][0]['subject'] == 'Initial project'
    recovered = json.loads(request(port2, git_base + '/operations/' + git_prepared['id']))
    assert recovered['status'] == 'prepared' and 'AGENTS.md' in recovered['patch']
    assert json.loads(request(port2, git_draft_path))['value']['message'] == 'Packaged commit'
    result = json.loads(request(port2, git_base + f"/operations/{recovered['id']}/apply", {'revision': recovered['revision']}))
    assert result['status'] == 'applied' and 'AGENTS.md' in git('diff', '--cached', '--name-only')
    unstage = json.loads(request(port2, git_base + '/operations', {'request_id': 'packaged_git_002', 'action': 'unstage'}))
    assert json.loads(request(port2, git_base + f"/operations/{unstage['id']}/apply", {'revision': unstage['revision']}))['status'] == 'applied'
    assert (project_root / 'AGENTS.md').exists() and git('diff', '--cached') == ''
    stage = json.loads(request(port2, git_base + '/operations', {'request_id': 'packaged_git_003', 'action': 'stage', 'paths': ['notes.md']}))
    assert json.loads(request(port2, git_base + f"/operations/{stage['id']}/apply", {'revision': stage['revision']}))['status'] == 'applied'
    commit = json.loads(request(port2, git_base + '/operations', {'request_id': 'packaged_git_004', 'action': 'commit', 'message': 'Packaged commit'}))
    (project_root / 'notes.md').write_text('Working copy remains.', encoding='utf-8')
    result = json.loads(request(port2, git_base + f"/operations/{commit['id']}/apply", {'revision': commit['revision']}))
    assert result['status'] == 'applied' and git('show', 'HEAD:notes.md') == 'Packaged Git diff.'
    assert (project_root / 'notes.md').read_text(encoding='utf-8') == 'Working copy remains.'
    assert json.loads(request(port2, git_base + f"/operations/{commit['id']}/apply", {'revision': commit['revision']}))['result_head'] == result['result_head']
    assert git('rev-list', '--count', 'HEAD') == '2'
    attribute_review = json.loads(request(port2, git_base + '/operations', {
        'request_id': 'packaged_git_005', 'action': 'stage', 'paths': ['notes.md']}))
    (project_root / '.gitattributes').write_text('*.md -text\n', encoding='utf-8')
    assert json.loads(request(port2, git_base + f"/operations/{attribute_review['id']}/apply", {'revision': attribute_review['revision']}))['status'] == 'conflict'
    assert git('diff', '--cached') == ''
    request(port2, f'/api/conversations/{project_cid}', method='DELETE')
    assert json.loads((data / '.runtime.json').read_text(encoding='utf-8'))['instance_id'] != first_instance
    cached_memory = json.loads(request(port2, f'/api/conversations/{memory_cid}/memory'))
    assert cached_memory['summary'] == persisted_memory['summary'] and cached_memory['revision'] == persisted_memory['revision']
    assert not cached_memory['busy'] and not cached_memory['stale']
    no_op = json.loads(request(port2, f'/api/conversations/{memory_cid}/memory/compact', {'expected_revision': cached_memory['revision']}))
    assert no_op['revision'] == cached_memory['revision']
    history_stream = request(port2, '/api/chat/stream', {'question': 'Plan the next experiment', 'conversation_id': memory_cid, 'execution_mode': 'plan'}).decode()
    assert 'event: memory' in history_stream and '14' in history_stream and 'event: answer' in history_stream
    updated_memory = json.loads(request(port2, f'/api/conversations/{memory_cid}/memory/compact', {'expected_revision': cached_memory['revision']}))
    assert updated_memory['covered_turns'] == 15
    request(port2, f'/api/conversations/{memory_cid}', method='DELETE')
    assert port != port2, "desktop launch did not exercise a new origin"
    assert "c_legacy_read" not in {item["id"] for item in json.loads(request(port2, "/api/conversations"))["conversations"]}
    archived_item = next(item for item in json.loads(request(port2, "/api/conversations?scope=archived"))["conversations"] if item["id"] == "c_legacy_read")
    assert archived_item["archived_at"] == archived["archived_at"]
    assert json.loads(request(port2, "/api/conversations/c_legacy_read"))["messages"][0]["answer"] == "Legacy answer"
    archived_search = json.loads(request(port2, "/api/conversations/page?scope=archived&q=Legacy%20answer"))
    assert archived_search["conversations"][0]["id"] == "c_legacy_read"
    assert json.loads(request(port2, "/api/conversations/page?q=Legacy%20answer"))["total"] == 0
    assert json.loads(request(port2, archive_draft_path))["value"]["text"] == "Archived draft survives restart"
    assert json.loads(request(port2, archive_path, {"archived": False, "expected_revision": 1}, method="PUT"))["archive_revision"] == 2
    try:
        request(port2, archive_path, archive_request, method="PUT")
    except urllib.error.HTTPError as error:
        assert error.code == 409
    else:
        raise AssertionError("old archive request overwrote a restored conversation")
    snapshot = json.loads(request(port2, "/api/tasks"))
    receipt_item = next(item for item in snapshot["conversations"] if item["id"] == "c_packaged_receipt")
    assert receipt_item["unread_count"] == 0
    assert json.loads(request(port2, "/api/conversations/c_packaged_receipt"))["messages"][0]["read_at"]
    assert json.loads(request(port2, draft_path)) == saved_draft
    assert json.loads(request(port2, draft_path))["value"]["execution_mode"] == "plan"
    assert json.loads(request(port2, "/api/desktop-state/selection"))["value"]["conversation_id"] == draft_id
    assert json.loads(request(port2, f"/api/conversations/{draft_id}"))["messages"] == []
    assert json.loads(request(port2, "/api/desktop-state/sidebar"))["value"]["sort"] == "created_at"
    draft_item = next(item for item in json.loads(request(port2, "/api/conversations"))["conversations"] if item["id"] == draft_id)
    assert draft_item["pinned_at"] == pinned["pinned_at"] and draft_item["draft_only"]
    assert draft_item["pin_position"] == 0
    pinned_items = [item for item in snapshot["conversations"] if item.get("pinned_at")]
    assert [item["id"] for item in sorted(pinned_items, key=lambda item: item["pin_position"])] == [draft_id, cid]
    request(port2, f"/api/conversations/{draft_id}", method="DELETE")
    assert json.loads(request(port2, "/api/desktop-state/selection"))["value"]["conversation_id"] is None
    recovered_runs = json.loads(request(port2, f"/api/foreground-runs?conversation_id={cid}"))["runs"]
    recovered_run = next(run for run in recovered_runs if run["id"] == foreground_id)
    assert recovered_run["status"] == "interrupted"
    assert recovered_run["partial_answer"] == "Saved packaged partial answer"
    assert recovered_run["execution_mode"] == "plan"
    reviewed_run = json.loads(request(port2, f"/api/foreground-runs?conversation_id={plan_cid}"))["runs"][0]
    assert reviewed_run["reviewed_plan_id"] == plan_record["id"] and reviewed_run["status"] == "interrupted"
    try:
        request(port2, "/api/chat/stream", {"run_id": plan_execution_id, "question": "Execute reviewed plan",
                "conversation_id": plan_cid, "reviewed_plan_id": plan_record["id"]})
    except urllib.error.HTTPError as error:
        assert error.code == 409
    else:
        raise AssertionError("packaged backend replayed an interrupted foreground run")
    retry_id = "chat_packaged_reviewed_retry"
    retry = {"id": retry_id, "question": "Execute reviewed plan", "conversation_id": plan_cid,
             "reviewed_plan_id": plan_record["id"], "retry_plan_run_id": plan_execution_id}
    request(port2, "/api/foreground-runs", retry)
    assert b'event: answer' in request(port2, "/api/chat/stream", {**retry, "run_id": retry_id})
    reviewed_messages = json.loads(request(port2, f"/api/conversations/{plan_cid}"))["messages"]
    assert len(reviewed_messages) == 2
    assert reviewed_messages[0]["plan_run_id"] == retry_id
    assert reviewed_messages[1]["execution_mode"] == "execute" and reviewed_messages[1]["reviewed_plan_id"] == plan_record["id"]
    request(port2, f"/api/foreground-runs/{foreground_id}", method="DELETE")
    restored = json.loads(request(port2, f"/api/conversations/{cid}"))
    assert restored["title"] == "[Verification] Packaged restart"
    history = json.loads(request(port2, "/api/tasks/history?limit=1"))
    assert history["history_total"] == 1 and history["tasks"][0]["id"] == task["id"]
    assert "history_epoch" in json.loads(request(port2, "/api/tasks"))
    request(port2, f"/api/tasks/{task['id']}/resume", {})
    finished = wait_task(port2, task['id'], lambda item: item['status'] == 'done')
    assert finished['recovery']['completed_tools'] == stopped['recovery']['completed_tools']
    restored = json.loads(request(port2, f"/api/conversations/{cid}"))
    assert len(restored['messages']) == 1
    unread_item = next(item for item in json.loads(request(port2, "/api/tasks"))["conversations"] if item["id"] == cid)
    assert unread_item["unread_count"] == 1
    request(port2, f"/api/conversations/{cid}/read", {"message_ids": [restored['messages'][0]['id']]})
    assert next(item for item in json.loads(request(port2, "/api/conversations"))["conversations"] if item["id"] == cid)["unread_count"] == 0
    request(port2, f"/api/conversations/{cid}", method="DELETE")
    try:
        request(port2, "/api/chat/stream", {"question": "stale", "conversation_id": cid})
    except urllib.error.HTTPError as error:
        assert error.code == 410
    else:
        raise AssertionError("packaged backend recreated a deleted conversation")
finally:
    cleanup(second, port2)

print("PASS: packaged EXE starts at different ports, serves current frontend, migrates legacy SQLite, searches/paginates conversations, restores archive/pins/unread/drafts/context/reading, recovers foreground/background execution, persists planning mode and reviewed-plan ownership, rejects duplicate claims and explicitly retries a reviewed execution across restart, rejects deleted conversations, releases ports")
print("PASS: second same-data EXE authenticates activation and exits without a second listener or task recovery; independent-data EXE runs concurrently; forced-exit stale discovery is replaced by a new owner")
print("PASS: packaged ripgrep hash/version/license, recursive project content search, persistent search options across ports and stale preview/page rejection")
print("PASS: packaged native ConPTY PowerShell, authenticated WebSocket input/output/resize, Unicode command execution, idempotent creation, child process containment on EXE crash, and no false terminal restoration after restart")
print("PASS: packaged long-context summary covers all 14 older turns, preserves 4 recent originals, survives restart, reuses cached revision, injects history into planning and incrementally updates; offline extraction makes no model call")
print("Scope: activation acknowledgement and desktop state were checked via HTTP/processes; native focus, window input and graceful close remain unverified.")
print("PASS: packaged project root registration, file read/scope rejection, project AGENTS init, file-context injection into foreground/background planning, project/draft/history recovery on a new port")
print("PASS: packaged file diff/update/undo/nested create and durable change history on real Windows files")
print("PASS: packaged installed-Git branch/status/unstaged-diff/history on a real repository after restart")
print("PASS: packaged durable Git stage review and commit draft survive restart; stage/unstage/commit alter real Git, preserve working files and do not duplicate a lost-response commit")
print(f"PASS: packaged frontend and all {static_count} assets match current dist byte-for-byte, including local math fonts")

# The process and port checks above must pass before touching this generated folder.
temp_root = Path(tempfile.gettempdir()).resolve()
data_resolved = data.resolve()
if data_resolved.parent != temp_root or not data_resolved.name.startswith("scholarflow-desktop-smoke-"):
    raise RuntimeError(f"Refusing to remove unexpected smoke-test path: {data_resolved}")


def remove_readonly_git_object(operation, target, error):
    candidate = Path(target)
    if (getattr(error, 'winerror', None) == 5 and operation is os.unlink
            and candidate.resolve().is_relative_to(data_resolved)
            and not candidate.is_symlink() and candidate.is_file()
            and '.git' in candidate.relative_to(data_resolved).parts
            and not candidate.stat().st_mode & stat.S_IWRITE):
        candidate.chmod(candidate.stat().st_mode | stat.S_IWRITE)
        operation(target)
    else:
        raise error


for attempt in range(60):
    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=rw", uri=True, timeout=0) as db:
            db.execute("BEGIN EXCLUSIVE")
            db.rollback()
        db.close()
        shutil.rmtree(data_resolved, onexc=remove_readonly_git_object)
        break
    except (OSError, sqlite3.OperationalError) as error:
        sharing_lock = getattr(error, "winerror", None) == 32
        sqlite_lock = isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower()
        if not (sharing_lock or sqlite_lock) or attempt == 59:
            raise RuntimeError(f"Could not safely clean temporary smoke-test data {data_resolved}; it was retained") from error
        time.sleep(0.5)
