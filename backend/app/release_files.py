"""Strict paths and hashes for local release evidence. Never executes a manifest."""
from __future__ import annotations

import hashlib
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .release_info import ReleaseError, canonical_json


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def relative_path(value):
    if (not isinstance(value, str) or not value or len(value) > 2048 or '\\' in value
            or ntpath.splitdrive(value)[0] or value.startswith('/') or ':' in value
            or any(ord(c) < 32 for c in value)):
        raise ReleaseError('清单包含不安全路径')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) or
           re.fullmatch(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', p) for p in parts):
        raise ReleaseError('清单包含不安全路径')
    return PurePosixPath(value)


def no_link(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ReleaseError('发行输入不能通过符号链接或目录联接读取')
    return path


def contained(root, name):
    return no_link(no_link(root).joinpath(*relative_path(name).parts))


def file_record(root, path):
    root, path = no_link(root), no_link(path)
    name = path.relative_to(root).as_posix()
    relative_path(name)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ReleaseError('发行清单仅允许普通文件')
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ReleaseError('文件在校验时发生变化，请重新准备构建输入')
    return {'path': name, 'size': after.st_size, 'sha256': hasher.hexdigest()}


def validate_records(records):
    if not isinstance(records, list) or len(records) > 200_000:
        raise ReleaseError('文件清单结构无效或过大')
    seen = set()
    for item in records:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}:
            raise ReleaseError('文件清单字段无效')
        name = relative_path(item['path']).as_posix().casefold()
        if name in seen or type(item['size']) is not int or item['size'] < 0 or not isinstance(item['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['sha256']):
            raise ReleaseError('文件清单重复或校验值无效')
        seen.add(name)
    return records


def verify_records(root, records):
    for item in validate_records(records):
        if file_record(root, contained(root, item['path'])) != item:
            raise ReleaseError('文件完整性不匹配：' + item['path'])


# Explicit program roots only: never enumerate .env, user data, project files,
# diagnostics, credentials, node_modules or venv while creating source evidence.
SOURCE_ROOTS = ('backend/app', 'frontend/src', 'frontend/public', 'desktop/assets')
SOURCE_FILES = ('release.json', 'backend/requirements.txt', 'frontend/package.json',
                'frontend/package-lock.json', 'frontend/vite.config.ts', 'frontend/releaseBuild.ts',
                'frontend/tsconfig.json', 'frontend/tsconfig.node.json',
                'frontend/index.html', 'desktop/source-wheel-provenance.json', 'LICENSE', 'LICENSES.md')
DESKTOP_SUFFIXES = {'.py', '.spec', '.ps1', '.nsi', '.iss', '.in', '.lock'}


def walk_regular(root):
    root = no_link(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules', 'data'))
        for dirname in dirs:
            no_link(Path(directory) / dirname)
        for name in sorted(files):
            if name.startswith('.') or name.endswith(('.pyc', '.pyo')):
                continue
            yield no_link(Path(directory) / name)


def source_inventory(root):
    root = no_link(root)
    paths = {contained(root, name) for name in SOURCE_FILES if contained(root, name).is_file()}
    for name in SOURCE_ROOTS:
        directory = contained(root, name)
        if directory.is_dir():
            paths.update(walk_regular(directory))
    desktop = contained(root, 'desktop')
    if desktop.is_dir():
        paths.update(p for p in desktop.iterdir() if p.suffix in DESKTOP_SUFFIXES and p.is_file())
    for name in ('rg.exe', 'LICENSE-MIT', 'UNLICENSE', 'COPYING', 'manifest.json'):
        path = contained(root, 'desktop/vendor/ripgrep/' + name)
        if path.is_file():
            paths.add(path)
    result = sorted([file_record(root, p) for p in paths], key=lambda row: row['path'])
    return validate_records(result)
