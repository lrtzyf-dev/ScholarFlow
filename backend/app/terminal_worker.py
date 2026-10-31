"""Private stdio worker. Native ConPTY handles never enter the desktop backend.

Each worker owns one PTY and exits with it. In particular, native teardown cannot
race later Python thread/event creation in the long-lived HTTP server.
"""
import ctypes
from ctypes import wintypes
import json
import os
import queue
import threading
import time


def _stream(number, mode):
    # Windowed PyInstaller executables have sys.stdin/stdout=None, even when
    # their parent supplies pipes. GetStdHandle still exposes those exact pipes.
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel.GetStdHandle(number & 0xffffffff)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError('Terminal worker requires inherited pipes')
    fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if mode == 'rb' else os.O_WRONLY))
    return os.fdopen(fd, mode, buffering=0)


def main():
    source, sink = _stream(-10, 'rb'), _stream(-11, 'wb')
    commands = queue.Queue(maxsize=128)
    disconnected = threading.Event()
    pty = job = None
    exit_code, error = None, ''

    def send(kind, **data):
        payload = (json.dumps({'type': kind, **data}, ensure_ascii=True) + '\n').encode('ascii')
        while payload:
            count = sink.write(payload)
            if not count:
                raise OSError('Terminal output pipe closed')
            payload = payload[count:]

    def read_commands():
        try:
            while True:
                raw = source.readline(524289)
                if not raw or len(raw) > 524288 or not raw.endswith(b'\n'):
                    break
                commands.put(json.loads(raw))
        except (OSError, ValueError):
            pass
        finally:
            disconnected.set()

    try:
        from winpty import PTY
        from winpty.enums import Backend
        from .terminal_sessions import ProcessJob, shell_command, shell_environment
        config = json.loads(source.readline(32769))
        executable, arguments = shell_command(config['shell'])
        pty = PTY(config['cols'], config['rows'], backend=Backend.ConPTY)
        if not pty.spawn(str(executable), cmdline=arguments, cwd=config['cwd'], env=shell_environment()):
            raise OSError('Cannot start terminal shell')
        job = ProcessJob(pty.pid)
        # The backend assigned this worker to its job BEFORE sending config;
        # the shell and all descendants also inherit that outer lifetime.
        send('ready', pid=pty.pid)
        threading.Thread(target=read_commands, daemon=True).start()
        quiet, heartbeat = 0, time.monotonic()
        while not disconnected.is_set():
            for _ in range(8):
                try:
                    message = commands.get_nowait()
                except queue.Empty:
                    break
                if message['type'] == 'input':
                    pty.write(message['data'])
                elif message['type'] == 'resize':
                    pty.set_size(message['cols'], message['rows'])
                    send('size', cols=message['cols'], rows=message['rows'])
                else:
                    raise ValueError('Unknown terminal worker message')
            text = pty.read(blocking=False)
            if text:
                for offset in range(0, len(text), 4096):
                    send('output', data=text[offset:offset + 4096])
                quiet = 0
            elif not pty.isalive():
                quiet += 1
                if quiet >= 5:
                    break
            if time.monotonic() - heartbeat >= 1:
                send('heartbeat')
                heartbeat = time.monotonic()
            if not text:
                disconnected.wait(.02)
        exit_code = pty.get_exitstatus()
    except BaseException as exc:
        error = '终端进程或连接已结束：' + str(exc)[:200]
    finally:
        if job:
            job.close()
        try:
            send('exit', exit_code=exit_code, error=error)
        except OSError:
            pass
        # Reap native resources at the OS process boundary. Do not run PTY Drop
        # alongside another Python thread or reuse its native handle values.
        os._exit(0 if not error else 1)


if __name__ == '__main__':
    main()
