"""Approved, noninteractive Windows commands with bounded, durable output.

Initial cwd is project-scoped. The shell runs as the user and is NOT a sandbox.
No HTTP route starts commands: only the agent's policy/checkpoint path may run.
"""
from contextlib import ExitStack
from datetime import datetime
import codecs
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import cancel_store, git_process, run_store, workspace, workspace_write
from .db import SessionLocal
from .models import ProjectCommand
from .project_changes import available
from .terminal_sessions import shell_command, shell_environment_dict

MAX_OUTPUT_BYTES = 512 * 1024
MODEL_OUTPUT_CHARS = 12000
MAX_RUNNING = 4
_guard = threading.RLock()
_running = {}
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'output_limit', 'interrupted', 'rejected', 'conflict'}


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    command: str = Field(min_length=1, max_length=16000)
    path: str = Field(default='', max_length=1024)
    summary: str = Field(min_length=1, max_length=500)
    timeout_seconds: int = Field(default=120, ge=1, le=600)

    @field_validator('command', 'summary')
    @classmethod
    def text(cls, value):
        value.encode('utf-8')
        if not value.strip() or '\x00' in value:
            raise ValueError('命令和说明必须是有效文本')
        return value

    @field_validator('path')
    @classmethod
    def scoped(cls, value):
        return workspace.relative_path(value)


def get(db, command_id):
    row = db.get(ProjectCommand, command_id)
    if not row:
        workspace.fail('命令记录不存在', 404)
    return row


def info(row):
    return {key: getattr(row, key) for key in ('id', 'project_id', 'conversation_id', 'run_id', 'command',
        'path', 'cwd', 'summary', 'timeout_seconds', 'status', 'exit_code', 'pid', 'error', 'revision',
        'attempt', 'previous_id')} | {'output_limited': bool(row.output_limited), 'total_chars': len(row.output),
        **{key: getattr(row, key).isoformat() if getattr(row, key) else None
           for key in ('created_at', 'updated_at', 'started_at', 'finished_at')}}


def result(row):
    return {**info(row), 'command_id': row.id, 'output': row.output[-MODEL_OUTPUT_CHARS:],
            'output_offset': max(0, len(row.output) - MODEL_OUTPUT_CHARS),
            'output_is_partial': bool(row.output_limited or len(row.output) > MODEL_OUTPUT_CHARS),
            'execution_boundary': '当前用户权限的本机 PowerShell；初始目录受项目校验，进程不受项目沙箱限制'}


def read_output(scope, cid, arguments):
    if set(arguments) - {'command_id', 'offset'}:
        workspace.fail('不允许覆盖会话的项目范围')
    command_id, offset = arguments.get('command_id'), arguments.get('offset', 0)
    if not isinstance(command_id, str) or type(offset) is not int or offset < 0:
        workspace.fail('需要有效命令编号和非负整数 offset')
    with SessionLocal() as db:
        workspace.validate_scope(db, cid, scope)
        row = get(db, command_id)
        if row.project_id != scope['project_id'] or row.conversation_id != cid:
            workspace.fail('只能回查本会话的项目命令', 403)
        text = row.output[offset:offset + 8000]
        end = offset + len(text)
        return {**info(row), 'command_id': row.id, 'output': text, 'output_offset': offset,
                'next_offset': end if end < len(row.output) else None}


def _set(db, row, **values):
    for key, value in values.items():
        setattr(row, key, value)
    row.revision += 1
    row.updated_at = datetime.utcnow()
    db.commit()


def prepare(scope, cid, arguments, *, run_id, operation_key, retry=False):
    try:
        request = CommandRequest.model_validate(arguments)
    except ValueError as exc:
        workspace.fail(str(exc))
    if os.name != 'nt':
        workspace.fail('项目命令目前仅支持 Windows 桌面', 501)
    if not run_id or not operation_key:
        workspace.fail('命令必须属于可追踪的执行步骤')
    fingerprint = hashlib.sha256(json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with _guard, run_store.coordinated(), SessionLocal() as db:
        available(db, cid)
        workspace.validate_scope(db, cid, scope)
        cwd = workspace.scoped_path(scope, request.path)
        if not cwd.is_dir():
            workspace.fail('命令启动位置必须是项目内文件夹')
        previous = db.query(ProjectCommand).filter_by(operation_key=operation_key).order_by(ProjectCommand.attempt.desc()).first()
        if previous:
            if (previous.request_hash, previous.project_id, previous.conversation_id, previous.run_id) != (fingerprint, scope['project_id'], cid, run_id):
                workspace.fail('同一执行步骤的命令或项目不一致，拒绝复用', 409)
            if previous.status != 'interrupted' or not retry:
                return info(previous)
            if previous.attempt >= 10:
                workspace.fail('同一步骤重试已达 10 次，请核对项目后开始新任务', 409)
        saved_scope = {key: scope[key] for key in ('project_id', 'name', 'root_path', 'root_identity')}
        saved_scope['workspace_revision'] = scope.get('workspace_revision', 0)
        saved_scope['rules'] = workspace.directory_rules(scope, [request.path + '/__command__' if request.path else '__command__'])
        row = ProjectCommand(project_id=scope['project_id'], conversation_id=cid, run_id=run_id,
            operation_key=operation_key, request_hash=fingerprint, scope=saved_scope,
            command=request.command, path=request.path, cwd=str(cwd), summary=request.summary,
            timeout_seconds=request.timeout_seconds, attempt=previous.attempt + 1 if previous else 1,
            previous_id=previous.id if previous else None)
        db.add(row); db.commit(); db.refresh(row)
        return info(row)


def reject(command_id):
    with _guard, SessionLocal() as db:
        row = get(db, command_id)
        if row.status == 'proposed':
            _set(db, row, status='rejected', finished_at=datetime.utcnow(), error='用户或策略拒绝了命令；未启动进程')
        return info(row)


def recover_interrupted():
    with _guard, SessionLocal() as db:
        for row in db.query(ProjectCommand).filter_by(status='running').all():
            _set(db, row, status='interrupted', finished_at=datetime.utcnow(),
                 error='应用在命令完成确认前退出。文件或外部操作可能已生效，请核对后明确选择重试或跳过；未自动重放。')


def stop(command_id):
    with _guard, SessionLocal() as db:
        row = get(db, command_id)
        if row.status == 'running':
            cancel_store.request_cancel(row.run_id)
            if command_id in _running:
                _running[command_id]['stop'].set()
        return info(row)


def _powershell(command):
    # Windows passes Unicode argv directly. EncodedCommand makes Windows
    # PowerShell serialize stderr as CLIXML even with OutputFormat Text.
    # Set encoding before invoking any cmdlet (including module autoload).
    script = """$ProgressPreference = 'SilentlyContinue'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$global:LASTEXITCODE = 0
try {
    & {
""" + command + """
    }
    $commandSucceeded = $?
    if ($global:LASTEXITCODE -ne 0) { exit $global:LASTEXITCODE }
    if (-not $commandSucceeded) { exit 1 }
} catch { Write-Error $_; exit 1 }
"""
    executable, _ = shell_command('powershell')
    argv = [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text', '-Command', script]
    if len(subprocess.list2cmdline(argv).encode('utf-16-le')) // 2 >= 32760:
        workspace.fail('命令超过 Windows 启动参数上限，请将脚本保存为项目文件后调用')
    return argv


def _reader(pipe, chunks):
    try:
        while data := pipe.read(4096):
            chunks.put(data)
    finally:
        chunks.put(None)


def run(command_id, *, run_id):
    with _guard, SessionLocal() as db:
        row = get(db, command_id)
        if row.run_id != run_id:
            workspace.fail('命令与当前执行轮次不匹配', 409)
        if row.status in TERMINAL:
            return result(row)
        if row.status != 'proposed':
            workspace.fail('此命令正在运行，不能重复启动', 409)
        if git_process.stopping():
            workspace.fail('服务正在退出，命令未启动', 503)
        if len(_running) >= MAX_RUNNING or any(s['project_id'] == row.project_id for s in _running.values()):
            workspace.fail('此项目已有命令运行或命令数量已达上限，请等待结束后再试', 409)
        event = threading.Event()
        _running[row.id] = {'project_id': row.project_id, 'stop': event}
        scope, path, command, timeout = row.scope, row.path, row.command, row.timeout_seconds

    process = None
    output = ''
    status, error, exit_code, limited = 'interrupted', '', None, False
    chunks = queue.Queue(maxsize=64)
    reader = None
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    size = 0

    def collect(data):
        nonlocal output, size, limited
        allowed = max(0, MAX_OUTPUT_BYTES - size)
        output += decoder.decode(data[:allowed])
        size += min(allowed, len(data))
        limited = limited or len(data) > allowed

    try:
        with ExitStack() as handles, SessionLocal() as db:
            row = get(db, command_id)
            available(db, row.conversation_id)
            workspace.validate_scope(db, row.conversation_id, scope)
            root = workspace.root_path(scope)
            for index in range(len(Path(path).parts) + 1):
                directory = root.joinpath(*Path(path).parts[:index])
                handle = handles.enter_context(workspace_write._handle(directory, directory=True))
                if workspace.handle_path(handle, directory) != directory:
                    workspace.fail('命令启动目录已变化，请重新审阅', 409)
            cwd = workspace.scoped_path(scope, path)
            rule_paths = [path + '/__command__' if path else '__command__']
            if workspace.directory_rules(scope, rule_paths) != scope['rules']:
                status = 'conflict'
                workspace.fail('目录规范已修改，命令未执行；请重新审阅任务', 409)
            if cancel_store.is_cancelled(run_id) or event.is_set():
                status = 'cancelled'
                return _finish(command_id, status, '', output, None, False)
            # Persist before spawn; a crash at any following point must never imply
            # that the command was not executed.
            _set(db, row, status='running', started_at=datetime.utcnow())
            env = shell_environment_dict()
            env.update({'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1', 'CI': 'true'})
            process = subprocess.Popen(_powershell(command), cwd=str(cwd), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, creationflags=0x08000004)
            with git_process.contain(process, purpose='项目命令'):
                workspace.root_path(scope)
                _set(db, row, pid=process.pid)
                handles.close()
                reader = threading.Thread(target=_reader, args=(process.stdout, chunks), daemon=True)
                reader.start()
                started = last_save = time.monotonic()
                ended = False
                while True:
                    try:
                        data = chunks.get(timeout=.05)
                        if data is None:
                            ended = True
                        else:
                            collect(data)
                    except queue.Empty:
                        pass
                    now = time.monotonic()
                    if limited:
                        status, error = 'output_limit', '输出超过 512 KiB，已结束命令及子进程；保留此前输出'
                    elif cancel_store.is_cancelled(run_id) or event.is_set():
                        status, error = 'cancelled', '用户已停止命令；已发生的文件或外部操作不会自动撤销'
                    elif git_process.stopping():
                        status, error = 'interrupted', '应用正在退出，命令已结束；请核对已产生的操作'
                    elif now - started >= timeout:
                        status, error = 'timed_out', f'命令超过 {timeout} 秒，已结束命令及子进程'
                    if status != 'interrupted' or error:
                        git_process.terminate(process)
                        break
                    if process.poll() is not None and (ended or chunks.empty()):
                        exit_code = process.returncode
                        status = 'succeeded' if exit_code == 0 else 'failed'
                        break
                    if now - last_save >= .25:
                        _set(db, row, output=output, output_limited=int(limited))
                        last_save = now
            # Closing the job also closes inherited pipes held by descendants.
            if reader:
                deadline = time.monotonic() + 5
                while reader.is_alive() or not chunks.empty():
                    try:
                        data = chunks.get(timeout=.05)
                        if data is not None:
                            collect(data)
                    except queue.Empty:
                        if time.monotonic() >= deadline:
                            break
                reader.join(timeout=.1)
            output += decoder.decode(b'', final=True)
            if limited and status in ('succeeded', 'failed'):
                status, error = 'output_limit', '输出超过 512 KiB，保留此前输出，不能据此确认完整结果'
            exit_code = process.poll()
    except Exception as exc:
        error = str(getattr(exc, 'detail', exc))[:1000]
        if process is None and status == 'interrupted':
            status = 'conflict'
    finally:
        if process:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
        with _guard:
            _running.pop(command_id, None)
    return _finish(command_id, status, error, output, exit_code, limited)


def _finish(command_id, status, error, output, exit_code, limited):
    with _guard, SessionLocal() as db:
        row = get(db, command_id)
        _set(db, row, status=status, error=error, output=output, exit_code=exit_code,
             output_limited=int(limited), finished_at=datetime.utcnow())
        return result(row)
