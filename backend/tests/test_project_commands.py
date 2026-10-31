"""Real Windows project commands, durable results, admission and process lifetime."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import cancel_store, git_process, project_commands as commands, workspace
from app.db import SessionLocal
from app.main import app
from app.models import Conversation, ProjectCommand

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows project commands')


@pytest.fixture(autouse=True)
def lifetime():
    git_process.start(); cancel_store.clear('command_run')
    yield
    git_process.shutdown()
    assert not git_process.active() and not commands._running


@pytest.fixture
def project(db, tmp_path):
    root = tmp_path / 'command 中文'; root.mkdir(); (root / 'src').mkdir()
    item = workspace.register(db, str(root))
    db.add(Conversation(id='command_chat', project_id=item.id)); db.commit()
    return workspace.project_scope(item)


def prepare(project, command, **kwargs):
    return commands.prepare(project, 'command_chat', {'command': command, 'summary': 'Verify command', **kwargs},
                            run_id='command_run', operation_key='agent:command_run:tool/0/0')


def until(check):
    end = time.monotonic() + 15
    while not check():
        assert time.monotonic() < end
        time.sleep(.025)


def test_actual_unicode_execution_and_lost_response_never_repeats(project):
    item = prepare(project, "[IO.File]::AppendAllText((Join-Path $PWD 'executed.txt'), '中文 😀'); Write-Output 'ACTUAL_OUTPUT 中文 😀'", path='src')
    file = Path(project['root_path']) / 'src' / 'executed.txt'
    assert not file.exists()
    value = commands.run(item['id'], run_id='command_run')
    assert value['status'] == 'succeeded' and value['exit_code'] == 0, value
    assert file.read_text(encoding='utf-8') == '中文 😀' and 'ACTUAL_OUTPUT' in value['output']
    assert '中文 😀' in value['output'] and 'CLIXML' not in value['output'] and '\ufffd' not in value['output']
    assert commands.run(item['id'], run_id='command_run') == value
    assert file.read_text(encoding='utf-8') == '中文 😀'
    assert prepare(project, item['command'], path='src')['id'] == item['id']
    with pytest.raises(HTTPException):
        prepare(project, 'Write-Output changed')


@pytest.mark.parametrize('command,code', [('Write-Output ok; exit 7', 7), ("throw 'test failure'", 1),
                                        ("& $env:ComSpec /d /c 'exit 9'", 9)])
def test_exit_code_and_errors_are_real(project, command, code):
    value = commands.run(prepare(project, command)['id'], run_id='command_run')
    assert value['status'] == 'failed' and value['exit_code'] == code, value
    if 'throw' in command:
        assert 'test failure' in value['output'] and 'CLIXML' not in value['output']


def test_stop_timeout_and_child_pipe_cleanup(project):
    import psutil
    root = Path(project['root_path'])
    script = r"$p=Start-Process -FilePath ($PSHOME+'\powershell.exe') -ArgumentList '-NoProfile','-Command','Start-Sleep 120' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText((Join-Path $PWD 'child.pid'), [string]$p.Id); Write-Output 'BEFORE_SLEEP'; Start-Sleep 60"
    item = prepare(project, script)
    with ThreadPoolExecutor() as pool:
        job = pool.submit(commands.run, item['id'], run_id='command_run')
        until(lambda: (root / 'child.pid').exists())
        pid = int((root / 'child.pid').read_text())
        assert psutil.pid_exists(pid)
        def output_saved():
            with SessionLocal() as db:
                return 'BEFORE_SLEEP' in db.get(ProjectCommand, item['id']).output
        until(output_saved)
        commands.stop(item['id']); commands.stop(item['id'])
        value = job.result(timeout=10)
    assert value['status'] == 'cancelled' and 'BEFORE_SLEEP' in value['output']
    until(lambda: not psutil.pid_exists(pid))
    cancel_store.clear('command_run')
    timed = commands.prepare(project, 'command_chat', {'command': 'Start-Sleep 60', 'summary': 'Timeout', 'timeout_seconds': 1},
                             run_id='command_run', operation_key='timeout')
    assert commands.run(timed['id'], run_id='command_run')['status'] == 'timed_out'


def test_output_limit_journal_and_paging(project, monkeypatch):
    monkeypatch.setattr(commands, 'MAX_OUTPUT_BYTES', 40000)
    item = prepare(project, "[Console]::Write('START'+('中'*30000)); Start-Sleep 60")
    value = commands.run(item['id'], run_id='command_run')
    assert value['status'] == 'output_limit' and value['output_limited']
    assert value['output_is_partial'] and len(value['output']) <= commands.MODEL_OUTPUT_CHARS
    client = TestClient(app)
    first = client.get(f"/api/project-commands/{item['id']}?tail=false").json()
    assert first['output'].startswith('START') and first['next_offset'] == 8000
    assert first['output'][5:] == '中' * 7995
    next_page = client.get(f"/api/project-commands/{item['id']}?tail=false&offset=8000").json()
    assert next_page['output_offset'] == 8000
    assert client.get(f"/api/projects/{project['project_id']}/commands").json()['total'] == 1
    exported = client.get(f"/api/project-commands/{item['id']}/output")
    assert exported.status_code == 200 and 'attachment;' in exported.headers['content-disposition']
    assert 'Output limited: True' in exported.text and '中' * 12000 in exported.text


def test_multiline_quoting_metacharacters_and_unicode_errors(project):
    command = """$value = '中文 😀 "quoted" `ticks` $literal & | < >'
[IO.File]::WriteAllText((Join-Path $PWD 'quotes.txt'), $value)
Write-Output $value
Write-Error '错误 😀'
exit 17
"""
    value = commands.run(prepare(project, command)['id'], run_id='command_run')
    assert value['status'] == 'failed' and value['exit_code'] == 17
    assert (Path(project['root_path']) / 'quotes.txt').read_text(encoding='utf-8') in value['output']
    assert '错误 😀' in value['output'] and 'CLIXML' not in value['output'] and '\ufffd' not in value['output']


@pytest.mark.parametrize('mode,decision,expected', [('plan', 'approve', None), ('execute', 'deny', 'rejected'),
    ('execute', 'approve', 'succeeded'), ('full', 'approve', 'succeeded'), ('hook', 'approve', None)])
def test_agent_command_policy_real_execution_and_result_trace(project, db, monkeypatch, mode, decision, expected):
    from app.agent import loop
    from app.llm import LLMResponse
    target = Path(project['root_path']) / 'agent.txt'
    class Model:
        def stream_chat(self, messages, tools=None):
            if mode == 'plan':
                assert 'run_project_command' not in {tool['name'] for tool in tools}
            if not any(message['role'] == 'tool' for message in messages):
                yield 'final', LLMResponse('', [{'id': 'cmd1', 'name': 'run_project_command', 'arguments': {
                    'command': "[IO.File]::AppendAllText((Join-Path $PWD 'agent.txt'), 'once'); Write-Output 'RESULT 中文 😀'",
                    'summary': 'Run a real model-requested command'}}])
            else:
                result = json.loads(next(message['content'] for message in reversed(messages) if message['role'] == 'tool'))
                if expected == 'succeeded':
                    assert result['exit_code'] == 0 and 'RESULT 中文 😀' in result['output']
                yield 'final', LLMResponse('Command processed.')
    monkeypatch.setattr(loop, 'get_llm', Model)
    monkeypatch.setattr(loop, 'make_plan', lambda *a: [])
    monkeypatch.setattr(loop.settings, 'memory_recall', False)
    monkeypatch.setattr(loop.settings, 'memory_capture', False)
    monkeypatch.setattr(loop.settings, 'approval_mode', 'full_access' if mode == 'full' else 'read_only')
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda name, *a, **kw: {'ran': int(mode == 'hook'), 'blocked': mode == 'hook' and name == 'PreToolUse', 'feedback': ''})
    def approve(id):
        pending = loop.approval_store.get(id)
        assert pending.arguments['command_id'] and pending.arguments['cwd'] == project['root_path']
        assert not target.exists()
        loop.approval_store.resolve(id, decision)
    monkeypatch.setattr(loop.approval_store, 'wait', approve)
    events = list(loop.run_agent(db, 'Run project checks', conversation_id='command_chat', run_id='command_run',
        execution_mode='plan' if mode == 'plan' else 'execute', workspace={**project, 'files': [], 'rules': []}))
    db.expire_all()
    assert [row.status for row in db.query(ProjectCommand).all()] == ([expected] if expected else [])
    assert target.exists() == (expected == 'succeeded')
    if target.exists():
        assert target.read_text() == 'once'
        assert any(kind == 'project_command' and event.get('status') == 'succeeded' and event.get('exit_code') == 0 for kind, event in events)
    assert bool([event for kind, event in events if kind == 'approval_request']) == (mode == 'execute')
    assert any(kind == 'answer' for kind, _ in events)


def test_scope_validation_rules_conflict_and_no_http_execution(project):
    for extra in ({'path': '../outside'}, {'path': 'D:/absolute'}, {'project_id': 'override'}, {'cwd': 'D:/'}, {'command': '\0'}):
        with pytest.raises(HTTPException):
            prepare(project, **{'command': 'Write-Output no', **extra})
    item = prepare(project, "[IO.File]::WriteAllText((Join-Path $PWD 'must-not-exist'), 'bad')")
    (Path(project['root_path']) / 'AGENTS.md').write_text('New rules', encoding='utf-8')
    value = commands.run(item['id'], run_id='command_run')
    assert value['status'] == 'conflict' and value['pid'] is None
    assert not (Path(project['root_path']) / 'must-not-exist').exists()
    client = TestClient(app, base_url='http://127.0.0.1')
    assert client.post(f"/api/project-commands/{item['id']}/run").status_code in (404, 405)
    assert client.post(f"/api/project-commands/{item['id']}/stop", headers={'Origin': 'https://evil.invalid'}).status_code == 403


def test_interrupted_journal_requires_explicit_retry_and_preserves_attempt(project):
    item = prepare(project, 'Write-Output recovered')
    with SessionLocal() as db:
        row = db.get(ProjectCommand, item['id']); row.status = 'running'; row.output = 'partial before crash'; db.commit()
    commands.recover_interrupted()
    assert prepare(project, item['command'])['status'] == 'interrupted'
    assert commands.run(item['id'], run_id='command_run')['status'] == 'interrupted'
    retry = commands.prepare(project, 'command_chat', {'command': item['command'], 'summary': 'Verify command'},
                             run_id='command_run', operation_key='agent:command_run:tool/0/0', retry=True)
    assert retry['id'] != item['id'] and retry['attempt'] == 2 and retry['previous_id'] == item['id']
    assert commands.run(retry['id'], run_id='command_run')['status'] == 'succeeded'
    with SessionLocal() as db:
        assert db.get(ProjectCommand, item['id']).output == 'partial before crash'


def test_environment_no_application_credentials_and_current_user_boundary(project, monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'not-in-child')
    monkeypatch.setenv('SCHOLARFLOW_INSTANCE_TOKEN', 'private-runtime-token')
    item = prepare(project, "Write-Output ($env:LLM_API_KEY+'|'+$env:SCHOLARFLOW_INSTANCE_TOKEN); Write-Output $PWD")
    value = commands.run(item['id'], run_id='command_run')
    assert 'not-in-child' not in value['output'] and 'private-runtime-token' not in value['output']
    assert project['root_path'] in value['output'] and '不受项目沙箱限制' in value['execution_boundary']


def test_output_recall_scope_pagination_and_plan_dispatch(project, db):
    from app.agent.tools import execute_tool, tool_risk
    from app.execution_mode import PLAN_READ_TOOLS
    item = prepare(project, "[Console]::Write('HEAD'+('😀中'*9000)+'TAIL')")
    commands.run(item['id'], run_id='command_run')
    page = json.loads(execute_tool(db, 'read_project_command_output', {'command_id': item['id']},
        conversation_id='command_chat', project_scope=project))
    assert page['output'].startswith('HEAD😀中') and len(page['output']) == 8000 and page['next_offset'] == 8000
    tail = commands.read_output(project, 'command_chat', {'command_id': item['id'], 'offset': 16000})
    assert tail['output'].endswith('TAIL') and tail['next_offset'] is None
    db.add(Conversation(id='other_chat', project_id=project['project_id'])); db.commit()
    with pytest.raises(HTTPException):
        commands.read_output(project, 'other_chat', {'command_id': item['id']})
    for extra in ({'project_id': 'override'}, {'offset': True}, {'offset': -1}):
        with pytest.raises(HTTPException):
            commands.read_output(project, 'command_chat', {'command_id': item['id'], **extra})
    assert tool_risk('read_project_command_output') == 'safe' and 'read_project_command_output' in PLAN_READ_TOOLS


def test_real_backend_crash_kills_process_tree_and_recovers_journal(tmp_path):
    import psutil
    script = r'''
import os,sys,time,threading
from pathlib import Path
from app import git_process,project_commands as c,workspace
from app.db import init_db,SessionLocal
from app.models import Conversation,ProjectCommand
init_db()
root=Path(sys.argv[1])/'project'; root.mkdir()
with SessionLocal() as db:
    p=workspace.register(db,str(root)); db.add(Conversation(id='crash',project_id=p.id)); db.commit()
    scope=workspace.project_scope(p)
item=c.prepare(scope,'crash',{'command':"$p=Start-Process -FilePath ($PSHOME+'\\powershell.exe') -ArgumentList '-NoProfile','-Command','Start-Sleep 120' -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText((Join-Path $PWD 'child.pid'), [string]$p.Id); [Console]::WriteLine('CRASH_MARKER'); Start-Sleep 120",'summary':'Crash check','timeout_seconds':600},run_id='crash_run',operation_key='crash_op')
git_process.start()
threading.Thread(target=lambda:c.run(item['id'],run_id='crash_run'),daemon=True).start()
while True:
    with SessionLocal() as db:
        row=db.get(ProjectCommand,item['id'])
        if row.pid and 'CRASH_MARKER' in row.output:
            # Publish readiness only after the PID bytes are complete. The
            # parent must never observe the empty file created by write_text.
            (root/'shell.pid.tmp').write_text(str(row.pid))
            os.replace(root/'shell.pid.tmp', root/'shell.pid')
            print(item['id'],flush=True); break
    time.sleep(.03)
while True: time.sleep(.1)
'''
    env = {**os.environ, 'DATA_DIR': str(tmp_path), 'PYTHONIOENCODING': 'utf-8'}
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
    try:
        until(lambda: (tmp_path / 'project/shell.pid').exists() or process.poll() is not None)
        assert process.poll() is None, process.communicate(timeout=3)
        pids = [int((tmp_path / 'project' / name).read_text()) for name in ('shell.pid', 'child.pid')]
        assert all(psutil.pid_exists(pid) for pid in pids)
        process.kill(); out, err = process.communicate(timeout=10)
        until(lambda: all(not psutil.pid_exists(pid) for pid in pids))
        restored = subprocess.run([sys.executable, '-c',
            "from app.db import init_db,SessionLocal; from app import project_commands as c; from app.models import ProjectCommand; init_db(); c.recover_interrupted(); db=SessionLocal(); row=db.query(ProjectCommand).one(); assert row.status=='interrupted' and 'CRASH_MARKER' in row.output; assert c.run(row.id,run_id=row.run_id)['status']=='interrupted'; print('RESTORED_WITHOUT_REPLAY')"],
            env=env, capture_output=True, timeout=20)
        assert restored.returncode == 0 and b'RESTORED_WITHOUT_REPLAY' in restored.stdout, restored.stderr.decode('utf-8')
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=10)
