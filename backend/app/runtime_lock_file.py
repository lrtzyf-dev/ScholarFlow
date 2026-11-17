"""Opened-handle identity for byte-preserving local process locks.

Standard library only: safe before configuration, database or runtime startup.
Opening uses no truncation/append initialization; empty files stay empty.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat


class LockIdentityError(ValueError):
    pass


def _plain_path(value):
    path = Path(value).absolute()
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LockIdentityError("锁路径不能使用符号链接或目录联接")
    return path


def validate_open_lock(stream, path):
    """The actual opened handle, not a pre-open pathname claim, is checked."""
    opened = os.fstat(stream.fileno())
    named = _plain_path(path).stat()
    if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1 or named.st_nlink != 1
            or getattr(opened, "st_file_attributes", 0) & 0x400
            or not opened.st_ino or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
        raise LockIdentityError("锁必须是同一路径的独立普通文件，不能使用硬链接或重定向句柄")


def open_lock_file(path):
    path = _plain_path(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        stream = os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        validate_open_lock(stream, path)
        return stream
    except BaseException:
        stream.close()
        raise
