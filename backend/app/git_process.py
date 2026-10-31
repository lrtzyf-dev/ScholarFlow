"""Tracked local processes; Windows jobs contain Git hooks and agent commands."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
import threading
import time

_guard = threading.RLock()
_processes = {}
_stopping = threading.Event()


@contextmanager
def contain(process, purpose='Git'):
    if os.name != 'nt':
        try:
            yield
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    job = kernel.CreateJobObjectW(None, None)
    try:
        class Limits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t), ('max_working', ctypes.c_size_t),
                        ('active_processes', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', Limits), ('io_counters', ctypes.c_ulonglong * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]
        limits = ExtendedLimits(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(job, int(process._handle))):
            raise OSError(ctypes.get_last_error(), f'Cannot contain {purpose} process')
        # Popen closes the primary thread handle. Enumerate it while the process
        # is still suspended, then resume only after assigning the job.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
                        ('tid', wintypes.DWORD), ('pid', wintypes.DWORD),
                        ('priority', wintypes.LONG), ('base', wintypes.LONG), ('flags', wintypes.DWORD)]
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        listing = kernel.CreateToolhelp32Snapshot(4, 0)
        if listing == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), f'Cannot enumerate {purpose} thread')
        resumed = False
        try:
            entry = ThreadEntry(); entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(listing, ctypes.byref(entry))
            while found:
                if entry.pid == process.pid:
                    thread = kernel.OpenThread(2, False, entry.tid)
                    if thread:
                        try:
                            resumed = kernel.ResumeThread(thread) != 0xffffffff
                        finally:
                            kernel.CloseHandle(thread)
                    break
                found = kernel.Thread32Next(listing, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(listing)
        if not resumed:
            raise OSError(f'Cannot resume contained {purpose} process')
        with _guard:
            _processes[process] = lambda: kernel.TerminateJobObject(job, 1)
            if _stopping.is_set():
                kernel.TerminateJobObject(job, 1)
        yield
    finally:
        with _guard:
            _processes.pop(process, None)
            if job:
                kernel.TerminateJobObject(job, 1)
                kernel.CloseHandle(job)
        if process.poll() is None:
            process.kill()
        process.wait()


def run(command, *, input, stdout, stderr, env, timeout=60):
    with _guard:
        if _stopping.is_set():
            raise RuntimeError('服务正在退出，Git 操作未启动')
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, env=env,
                                   creationflags=0x08000004 if os.name == 'nt' else 0,
                                   start_new_session=os.name != 'nt')
        _processes[process] = process.kill
    try:
        with contain(process):
            process.communicate(input=input, timeout=timeout)
            return process.returncode
    finally:
        with _guard:
            _processes.pop(process, None)


def start():
    _stopping.clear()


def stopping():
    return _stopping.is_set()


def terminate(process):
    with _guard:
        stop = _processes.get(process)
        if stop:
            stop()


def active():
    with _guard:
        return bool(_processes)


def shutdown():
    with _guard:
        _stopping.set()
        for stop in list(_processes.values()):
            try:
                stop()
            except OSError:
                pass
    deadline = time.monotonic() + 5
    while active() and time.monotonic() < deadline:
        time.sleep(.05)
