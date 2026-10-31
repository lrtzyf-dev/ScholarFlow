"""Windows file handles pin the directory chain and exclude concurrent writers.

Writes are journaled by project_changes before this module mutates any content.
In-place writes preserve file identity/ACLs but are not crash-atomic: an interrupted
write is reconciled from its durable before/after snapshots, never blindly retried.
"""
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from . import workspace


def target(scope, raw):
    rel = workspace.relative_path(raw)
    if not rel or any(nt_reserved(p) for p in rel.split("/")):
        workspace.fail("请指定普通文件的项目相对路径")
    root = workspace.root_path(scope)
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.exists() or current.is_symlink() or current.is_junction():
            workspace.scoped_path(scope, current.relative_to(root).as_posix())
    return root / rel


def nt_reserved(name):
    return (name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$',
            *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))} or
            any(c in name for c in '<>"|?*'))


@contextmanager
def _handle(path, *, directory=False, create=False, delete=False):
    if os.name != 'nt':
        workspace.fail("项目写入目前仅支持 Windows 桌面", 501)
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # No FILE_SHARE_WRITE or FILE_SHARE_DELETE, including directory handles.
    access = 0x80 if directory else (0x80000000 | 0x40000000 | (0x10000 if delete else 0))
    flags = 0x02000000 | 0x00200000 if directory else 0x00200000
    native = kernel.CreateFileW(str(path), access, 1, None, 1 if create else 3, flags, None)
    if native == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        if code in (32, 33):
            workspace.fail("文件或目录正被其他程序写入，请稍后重试", 409)
        raise OSError(code, f"无法打开文件（Windows 错误 {code}）")
    try:
        fd = msvcrt.open_osfhandle(native, os.O_RDONLY if directory else os.O_RDWR)
    except BaseException:
        kernel.CloseHandle(native)
        raise
    if directory:
        class DirectoryHandle:
            def fileno(self):
                return fd
        try:
            yield DirectoryHandle()
        finally:
            os.close(fd)
    else:
        try:
            file = os.fdopen(fd, 'r+b')
        except BaseException:
            os.close(fd)
            raise
        with file:
            yield file


@contextmanager
def locked_target(scope, raw, *, create=False, delete=False):
    path = target(scope, raw)
    root = workspace.root_path(scope)
    with ExitStack() as stack:
        current = root
        chain = [root, *(root.joinpath(*Path(raw).parts[:i]) for i in range(1, len(Path(raw).parts)))]
        for current in chain:
            if not current.exists() and create:
                current.mkdir()
            handle = stack.enter_context(_handle(current, directory=True))
            if (workspace.handle_path(handle, current) != current or not current.is_dir()
                    or current.is_symlink() or current.is_junction()):
                workspace.fail("文件目录发生变化，拒绝写入", 409)
            workspace.root_path(scope)
        file = stack.enter_context(_handle(path, create=create, delete=delete))
        workspace._check_handle(file, scope, path)
        if os.fstat(file.fileno()).st_nlink != 1:
            workspace.fail("暂不修改有多个硬链接的文件，请使用独立文件")
        yield file


def write_content(file, content):
    file.seek(0)
    file.write(content)
    file.truncate()
    file.flush()
    os.fsync(file.fileno())


def delete_content(file):
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    disposition = wintypes.BOOL(True)
    if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(file.fileno()), 4,
                                             ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise OSError(ctypes.get_last_error(), "无法删除文件")
