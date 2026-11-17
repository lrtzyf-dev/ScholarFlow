"""Opt-in fixed-name shortcut, only for a signed, committed installation."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.release_files import contained, no_link
from app.release_install import verify_candidate


_SCRIPT = """$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:SF_SHORTCUT_FILE)
$link.TargetPath = $env:SF_SHORTCUT_TARGET
$link.WorkingDirectory = $env:SF_SHORTCUT_WORKDIR
$link.Description = 'ScholarFlow'
$link.Save()
"""


def _native_writer(link, executable, working_directory):
    if os.name != 'nt':
        raise ValueError('Windows 快捷方式只能在 Windows 上创建')
    env = os.environ.copy()
    env.update(SF_SHORTCUT_FILE=str(link), SF_SHORTCUT_TARGET=str(executable),
               SF_SHORTCUT_WORKDIR=str(working_directory))
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', _SCRIPT],
        env=env, check=True, timeout=30, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def create_shortcut(target, directory, trusted_keys, receipt, *, forbidden_roots=(), native_writer=None):
    """Never guess Desktop, create directories, overwrite, or run the target EXE."""
    if receipt.get('state') != 'committed' or not receipt.get('candidate_id'):
        raise ValueError('只有已提交且身份明确的安装可以创建快捷方式')
    target = no_link(no_link(target).resolve())
    directory = no_link(no_link(directory).resolve())
    if not directory.is_dir():
        raise ValueError('请明确选择已经存在的快捷方式目录')
    for root in (target, *forbidden_roots):
        root = no_link(no_link(root).resolve())
        if directory.is_relative_to(root):
            raise ValueError('快捷方式目录必须独立于程序、候选、数据和事务目录')
    manifest = verify_candidate(target, trusted_keys)
    if manifest['candidate_id'] != receipt['candidate_id']:
        raise ValueError('当前安装身份与提交回执不符')
    executable = contained(target / 'payload', manifest['entrypoint'])
    if not executable.is_file():
        raise ValueError('已签名程序入口不存在')
    destination = no_link(directory / 'ScholarFlow.lnk')
    if destination.exists():
        raise ValueError('ScholarFlow.lnk 已存在，请先自行确认旧快捷方式')
    descriptor, temporary_name = tempfile.mkstemp(prefix='.scholarflow-shortcut-', suffix='.lnk', dir=directory)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        (native_writer or _native_writer)(temporary, executable, executable.parent)
        temporary = no_link(temporary)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ValueError('快捷方式创建没有产生有效文件')
        # Hard-link publication fails if another process wins; never overwrite.
        os.link(temporary, destination)
        return {'state': 'created', 'path': str(destination), 'target': str(executable)}
    finally:
        if temporary.exists():
            temporary.unlink()
