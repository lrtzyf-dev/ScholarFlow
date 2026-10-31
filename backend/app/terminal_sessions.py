"""Live Windows ConPTY sessions. Shells are local processes, not a sandbox."""
from collections import deque
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import os
import json
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
import uuid

from . import workspace, workspace_write

MAX_SESSIONS = 12
MAX_PROJECT_SESSIONS = 4
OUTPUT_CHARS = 512 * 1024
DETACH_SECONDS = 600
WORKER_START_SECONDS = 10
WORKER_HEARTBEAT_SECONDS = 20
_guard = threading.RLock()
_sessions = {}
_stopping = False


class ProcessJob:
    def __init__(self, pid):
        self.lock = threading.Lock()
        self.kernel = k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.handle = k.CreateJobObjectW(None, None)
        process = k.OpenProcess(0x100 | 1, False, pid)
        try:
            class Basic(ctypes.Structure):
                _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                            ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t), ('max_working', ctypes.c_size_t),
                            ('active_processes', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                            ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
            class Extended(ctypes.Structure):
                _fields_ = [('basic', Basic), ('io', ctypes.c_ulonglong * 6),
                            ('memory', ctypes.c_size_t * 4)]
            limits = Extended(); limits.basic.flags = 0x2000
            if (not self.handle or not process or
                    not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or
                    not k.AssignProcessToJobObject(self.handle, process)):
                if process:
                    k.TerminateProcess(process, 1)
                raise OSError(ctypes.get_last_error(), 'Cannot contain terminal process')
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                k.CloseHandle(process)

    def stop(self):
        with self.lock:
            if self.handle:
                self.kernel.TerminateJobObject(self.handle, 1)

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.TerminateJobObject(self.handle, 1)
                self.kernel.CloseHandle(self.handle)
                self.handle = None


def shell_command(shell):
    if os.name != 'nt':
        workspace.fail('交互式终端目前仅支持 Windows 桌面', 501)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    path = ctypes.create_unicode_buffer(32768)
    if not kernel.GetWindowsDirectoryW(path, len(path)):
        raise OSError('Cannot find Windows directory')
    system = Path(path.value) / 'System32'
    if shell == 'powershell':
        return system / 'WindowsPowerShell/v1.0/powershell.exe', ' -NoLogo -NoProfile -NoExit'
    if shell == 'cmd':
        return system / 'cmd.exe', ' /D /Q'
    workspace.fail('不支持的终端类型')


def shell_environment_dict():
    blocked = {'DATA_DIR', 'DATABASE_URL', 'REDIS_URL', 'LLM_API_KEY', 'EMBEDDING_API_KEY',
               'PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'}
    env = {k: v for k, v in os.environ.items() if k.upper() not in blocked and
           not k.upper().startswith(('SCHOLARFLOW_', '_PYI', 'PYINSTALLER_', 'PYWINPTY_'))}
    env.update({'TERM': 'xterm-256color', 'COLORTERM': 'truecolor'})
    return env


def shell_environment():
    return '\0'.join(f'{k}={v}' for k, v in sorted(shell_environment_dict().items())) + '\0'


def _worker_command():
    return ([sys.executable, '--terminal-worker'] if getattr(sys, 'frozen', False)
            else [sys.executable, '-u', '-m', 'app.terminal_worker'])


class Session:
    def __init__(self, scope, request):
        self.id = uuid.uuid4().hex
        self.project_id, self.request_id = scope['project_id'], request.request_id
        self.shell, self.path = request.shell, request.path
        self.request_options = (request.shell, request.path, request.cols, request.rows)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.output, self.output_size, self.sequence = deque(), 0, 0
        self.acknowledged = 0
        self.rows, self.cols = request.rows, request.cols
        self.created = time.time()
        self.detached = time.monotonic()
        self.attached = False
        self.status, self.exit_code, self.error = 'running', None, ''
        self.stopped = threading.Event()
        self.commands = queue.Queue(maxsize=128)
        self.events = queue.Queue(maxsize=128)
        self.ready = threading.Event()
        self.job = None
        self.process = None
        self.pid = None
        self.worker = None
        self.reader = self.writer = None
        root = workspace.root_path(scope)
        shell_command(self.shell)
        try:
            with ExitStack() as handles:
                for i in range(len(Path(self.path).parts) + 1):
                    directory = root.joinpath(*Path(self.path).parts[:i])
                    handle = handles.enter_context(workspace_write._handle(directory, directory=True))
                    if workspace.handle_path(handle, directory) != directory:
                        workspace.fail('终端启动目录已变化，请刷新', 409)
                cwd = workspace.scoped_path(scope, self.path)
                if not cwd.is_dir():
                    workspace.fail('终端启动位置必须是项目内文件夹')
                self.cwd = str(cwd)
                self.process = subprocess.Popen(_worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0, creationflags=0x08000000,
                    cwd=None if getattr(sys, 'frozen', False) else str(Path(__file__).resolve().parent.parent))
                # No shell can start until config is sent after job assignment.
                self.job = ProcessJob(self.process.pid)
                self.worker = threading.Thread(target=self._run, name=f'terminal-{self.id[:8]}', daemon=True)
                self.worker.start()
                if not self.ready.wait(WORKER_START_SECONDS) or not self.pid:
                    raise OSError(self.error or '终端辅助进程启动超时')
                workspace.root_path(scope)
        except BaseException:
            self.stopped.set()
            if self.job:
                self.job.stop()
            if self.process and self.process.poll() is None:
                self.process.kill()
            if self.worker:
                self.worker.join(timeout=3)
            else:
                self._close_process()
            raise

    def info(self):
        with self.lock:
            return {'id': self.id, 'project_id': self.project_id, 'shell': self.shell, 'path': self.path,
                    'cwd': self.cwd, 'pid': self.pid, 'status': self.status, 'exit_code': self.exit_code,
                    'error': self.error, 'rows': self.rows, 'cols': self.cols,
                    'created_at': self.created, 'connected': self.attached}

    def append(self, text):
        with self.lock:
            for offset in range(0, len(text), 4096):
                chunk = text[offset:offset + 4096]
                self.sequence += 1; self.output.append((self.sequence, chunk)); self.output_size += len(chunk)
            while self.output_size > OUTPUT_CHARS and self.output:
                self.output_size -= len(self.output.popleft()[1])

    def snapshot(self, after, through=None):
        with self.lock:
            first = self.output[0][0] if self.output else self.sequence + 1
            reset = after < first - 1 or after > self.sequence
            after = first - 1 if reset else after
            chunks = [(seq, text) for seq, text in self.output
                      if seq > after and (through is None or seq <= through)][:4]
            return reset, first - 1, chunks, self.info()

    def acknowledge(self, sequence, response):
        with self.lock:
            # Commit parser replies and the replay boundary together. A lost WebSocket
            # cannot cause a historical terminal query to reach the shell twice.
            if response and self.status == 'running' and not self.stopped.is_set():
                self.enqueue('input', response)
            self.acknowledged = max(self.acknowledged, sequence)

    def attach(self, token):
        if not secrets.compare_digest(self.token, token):
            workspace.fail('终端连接凭据无效', 403)
        with self.lock:
            if self.attached:
                workspace.fail('此终端已在另一窗口连接', 409)
            self.attached = True

    def detach(self):
        with self.lock:
            self.attached = False; self.detached = time.monotonic()

    def enqueue(self, kind, value):
        with self.lock:
            if self.status != 'running' or self.stopped.is_set():
                workspace.fail('终端已结束，请新建终端', 409)
        try:
            self.commands.put_nowait((kind, value))
        except queue.Full:
            workspace.fail('终端输入过快，请稍后重试', 429)

    def stop(self):
        self.stopped.set()
        if self.job:
            self.job.stop()

    def _event(self, message):
        while not self.stopped.is_set():
            try:
                self.events.put(message, timeout=.1)
                return
            except queue.Full:
                pass

    def _read_worker(self):
        try:
            while not self.stopped.is_set():
                raw = self.process.stdout.readline(65537)
                if not raw:
                    break
                if len(raw) > 65536 or not raw.endswith(b'\n'):
                    raise ValueError('终端辅助进程返回的数据过长')
                self._event(json.loads(raw))
        except (OSError, ValueError) as exc:
            self._event({'type': 'exit', 'error': str(exc)[:200]})
        finally:
            self._event({'type': 'closed'})

    def _write_worker(self):
        def write(value):
            payload = (json.dumps(value, ensure_ascii=True) + '\n').encode('ascii')
            # Unbuffered FileIO may return a short write.
            while payload:
                count = self.process.stdin.write(payload)
                if not count:
                    raise OSError('终端输入连接已结束')
                payload = payload[count:]
        try:
            write({'shell': self.shell, 'cwd': self.cwd, 'cols': self.cols, 'rows': self.rows})
            while not self.stopped.is_set():
                try:
                    kind, value = self.commands.get(timeout=.1)
                except queue.Empty:
                    continue
                write({'type': 'input', 'data': value} if kind == 'input'
                      else {'type': 'resize', 'cols': value[0], 'rows': value[1]})
        except (OSError, ValueError):
            # EOF/exit or the heartbeat deadline is authoritative; a native
            # crash must not be reported as a successfully completed shell.
            pass

    def _close_process(self):
        if self.job:
            self.job.close()
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=3)
            for thread in (self.reader, self.writer):
                if thread and thread.ident is not None:
                    thread.join(timeout=1)
            self.process.stdin.close()
            self.process.stdout.close()

    def _run(self):
        last_message = time.monotonic()
        self.reader = threading.Thread(target=self._read_worker, daemon=True)
        self.writer = threading.Thread(target=self._write_worker, daemon=True)
        try:
            self.reader.start(); self.writer.start()
            while not self.stopped.is_set():
                try:
                    message = self.events.get(timeout=.05)
                except queue.Empty:
                    message = None
                if message:
                    last_message = time.monotonic()
                    kind = message['type']
                    if kind == 'ready':
                        self.pid = message['pid']; self.ready.set()
                    elif kind == 'output':
                        self.append(message['data'])
                    elif kind == 'size':
                        with self.lock:
                            self.cols, self.rows = message['cols'], message['rows']
                    elif kind == 'exit':
                        self.exit_code, self.error = message.get('exit_code'), message.get('error', '')
                        break
                    elif kind == 'closed':
                        self.error = '终端辅助进程意外退出，请新建终端'
                        break
                if time.monotonic() - last_message > WORKER_HEARTBEAT_SECONDS:
                    self.error = '终端辅助进程无响应，已结束此终端，请新建终端'
                    break
                with self.lock:
                    abandoned = not self.attached and time.monotonic() - self.detached > DETACH_SECONDS
                if abandoned:
                    self.error = '连接已断开超过 10 分钟，终端已结束'
                    break
        except Exception as exc:
            if not self.stopped.is_set():
                self.error = '终端进程或连接已结束：' + str(exc)[:200]
        finally:
            requested_stop = self.stopped.is_set()
            self.stopped.set()
            try:
                self._close_process()
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.error = '终端辅助进程清理失败：' + str(exc)[:200]
            finally:
                with self.lock:
                    self.status = 'stopped' if requested_stop else 'exited'
                self.ready.set()


def create(scope, request):
    with _guard:
        if _stopping:
            workspace.fail('服务正在退出，终端未启动', 503)
        for session in _sessions.values():
            if (session.project_id, session.request_id) == (scope['project_id'], request.request_id):
                if session.request_options != (request.shell, request.path, request.cols, request.rows):
                    workspace.fail('终端请求编号已用于其他启动参数', 409)
                return session.info()
        if (len(_sessions) >= MAX_SESSIONS or
                sum(s.project_id == scope['project_id'] for s in _sessions.values()) >= MAX_PROJECT_SESSIONS):
            workspace.fail('终端数量达到上限，请先关闭不再使用的终端', 409)
        try:
            session = Session(scope, request)
        except workspace.HTTPException:
            raise
        except Exception as exc:
            workspace.fail('无法启动 Windows 终端，请检查桌面组件：' + str(exc)[:200], 503)
        _sessions[session.id] = session
        return session.info()


def get(project_id, session_id):
    with _guard:
        session = _sessions.get(session_id)
        if not session or session.project_id != project_id:
            workspace.fail('终端会话不存在，应用重启后需要新建', 404)
        return session


def listing(project_id):
    with _guard:
        return [s.info() for s in _sessions.values() if s.project_id == project_id]


def remove(project_id, session_id):
    session = get(project_id, session_id)
    session.stop()
    session.worker.join(timeout=3)
    if session.worker.is_alive():
        workspace.fail('终端仍在结束，请稍后重试', 409)
    with _guard:
        _sessions.pop(session_id, None)


def active():
    with _guard:
        return any(s.worker.is_alive() for s in _sessions.values())


def start():
    global _stopping
    with _guard:
        _stopping = False


def shutdown():
    global _stopping
    with _guard:
        _stopping = True
        sessions = list(_sessions.values())
        for session in sessions:
            session.stop()
    deadline = time.monotonic() + 5
    for session in sessions:
        session.worker.join(timeout=max(0, deadline - time.monotonic()))
