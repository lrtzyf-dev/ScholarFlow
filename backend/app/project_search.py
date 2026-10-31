"""Bounded ripgrep search, with scoped verification before returning snippets."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import workspace, workspace_write

PAGE_SIZE = 40
MAX_MATCHES = 2000
MAX_OUTPUT = 8 * 1024 * 1024
SCAN_SECONDS = 5
VERIFY_SECONDS = 8


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(default='', max_length=256)
    path: str = Field(default='', max_length=1024)
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False
    include: str = Field(default='', max_length=256)
    exclude: str = Field(default='', max_length=256)
    hidden: bool = False
    ignored: bool = False

    @field_validator('query', 'include', 'exclude')
    @classmethod
    def single_line(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError('搜索只支持单行内容和文件范围')
        return value

    @field_validator('include', 'exclude')
    @classmethod
    def positive_glob(cls, value):
        if value.startswith('!'):
            raise ValueError('请在排除字段填写排除规则，不需要 ! 前缀')
        return value

    @field_validator('path')
    @classmethod
    def local_path(cls, value):
        return workspace.relative_path(value)


class SearchRequest(SearchOptions):
    query: str = Field(min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0, le=MAX_MATCHES)
    snapshot: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


def executable():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2] / 'desktop'
    path = base / 'vendor' / 'ripgrep' / 'rg.exe'
    if not path.is_file():
        workspace.fail('本机搜索组件缺失，请重新安装完整桌面包', 503)
    return path


@contextmanager
def _pinned(scope, path):
    root = workspace.root_path(scope)
    with workspace_write._handle(root, directory=True) as handle:
        if workspace.handle_path(handle, root) != root:
            workspace.fail('项目目录已变化，请刷新', 409)
        directory = workspace.scoped_path(scope, path)
        if not directory.is_dir():
            workspace.fail('搜索范围必须是项目内文件夹')
        # Pin the complete selected directory chain for the external traversal.
        from contextlib import ExitStack
        with ExitStack() as stack:
            current = root
            for part in Path(path).parts:
                current /= part
                handle = stack.enter_context(workspace_write._handle(current, directory=True))
                if workspace.handle_path(handle, current) != current:
                    workspace.fail('搜索目录已变化，请刷新', 409)
            yield root, directory
        workspace.root_path(scope)


def _run(root, req):
    args = [str(executable()), '--no-config', '--json', '--line-number', '--sort=path',
            '--threads=1', '--no-follow', '--no-mmap', '--encoding=none', '--color=never', '--crlf',
            '--no-ignore-parent', '--no-ignore-global', '--no-ignore-exclude', '--no-require-git',
            '--max-filesize=2M', '--max-depth=64', f'--max-count={MAX_MATCHES + 1}',
            '--regex-size-limit=1M', '--dfa-size-limit=4M', '--engine=default']
    if not req.regex:
        args.append('--fixed-strings')
    args.append('--case-sensitive' if req.case_sensitive else '--ignore-case')
    if req.whole_word:
        args.append('--word-regexp')
    if req.hidden:
        args.append('--hidden')
    if req.ignored:
        args.append('--no-ignore')
    if req.include:
        args.append('--glob=' + req.include)
    if req.exclude:
        args.append('--glob=!' + req.exclude)
    for directory in sorted(workspace.SKIP_DIRS):
        args += ['--iglob=!**/' + directory + '/**', '--iglob=!**/' + directory]
    args += ['--regexp', req.query, '--', req.path or '.']
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('RIPGREP_', 'GIT_'))}
    reason = []
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=errors, creationflags=0x08000000 if os.name == 'nt' else 0)
            try:
                deadline = time.monotonic() + SCAN_SECONDS
                while process.poll() is None:
                    if output.tell() > MAX_OUTPUT or errors.tell() > 32768:
                        reason.append('搜索输出达到上限'); process.kill(); break
                    if time.monotonic() >= deadline:
                        reason.append('搜索达到时间上限'); process.kill(); break
                    time.sleep(.02)
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
            output.seek(0); errors.seek(0)
            raw = output.read(MAX_OUTPUT + 1)
            if len(raw) > MAX_OUTPUT:
                raw = raw[:MAX_OUTPUT]; reason.append('搜索输出达到上限')
            error = errors.read(2000).decode('utf-8', errors='replace').strip()
            return raw, process.returncode, error, reason
    except OSError:
        workspace.fail('无法启动本机搜索组件，请检查安装和权限', 503)


def _read(scope, path):
    target = workspace.scoped_path(scope, path)
    if not target.is_file():
        workspace.fail('搜索结果不再是普通文件', 409)
    with target.open('rb') as file:
        workspace._check_handle(file, scope, target)
        before = os.fstat(file.fileno())
        data = file.read(workspace.MAX_FILE_BYTES + 1)
        after = os.fstat(file.fileno())
        workspace._check_handle(file, scope, target)
        workspace.scoped_path(scope, path)
    if len(data) > workspace.MAX_FILE_BYTES or b'\x00' in data:
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        workspace.fail('搜索期间文件已改变', 409)
    data.decode('utf-8-sig')
    return data


def _snippet(data, event):
    line = event['lines'].get('text')
    if line is None:
        return None
    raw = line.encode('utf-8')
    start = event['absolute_offset']
    if (data[start:start + len(raw)] != raw or data[:start].count(b'\n') + 1 != event['line_number']):
        workspace.fail('搜索期间文件已改变', 409)
    spans = event['submatches'][:64]
    if not spans:
        return None
    # Use the engine's byte offsets, then return escaped Vue text segments.
    text = raw.decode('utf-8').rstrip('\r\n')
    bom = int(start == 0 and text.startswith('\ufeff'))
    text = text[bom:]
    positions = [(max(0, len(raw[:s['start']].decode('utf-8')) - bom), max(0, len(raw[:s['end']].decode('utf-8')) - bom)) for s in spans]
    first = positions[0][0]
    left = max(0, first - 80); right = min(len(text), left + 480)
    pieces, cursor = [], left
    if left:
        pieces.append({'text': '...', 'match': False})
    for a, b in positions:
        if a > right:
            break
        a, b = max(a, left), min(b, right)
        if a < cursor or b < a:
            continue
        if a > cursor:
            pieces.append({'text': text[cursor:a], 'match': False})
        pieces.append({'text': text[a:b], 'match': True}); cursor = b
    if cursor < right:
        pieces.append({'text': text[cursor:right], 'match': False})
    if right < len(text):
        pieces.append({'text': '...', 'match': False})
    return {'line': event['line_number'], 'column': first + 1,
            'length': positions[0][1] - positions[0][0],
            'offset': len(data[:start + spans[0]['start']].decode('utf-8-sig')),
            'parts': pieces, 'snippet_limited': left > 0 or right < len(text) or len(event['submatches']) > 64}


def search(scope, req: SearchRequest):
    with _pinned(scope, req.path) as (root, directory):
        raw, code, error, reasons = _run(root, req)
        events = []
        for record in raw.splitlines():
            try:
                events.append(json.loads(record))
            except (ValueError, UnicodeError):
                if not reasons:
                    workspace.fail('搜索组件返回的数据不完整，请重试', 503)
                break
        if code not in (0, 1) and not reasons:
            if not any(e.get('type') == 'summary' for e in events):
                workspace.fail(error or '搜索条件无效，请检查正则和文件范围')
            reasons.append('部分文件无法搜索：' + error[:500])
        matches, fingerprints = [], {}
        current_path, current_data = None, None
        deadline = time.monotonic() + VERIFY_SECONDS
        for event in events:
            if event.get('type') != 'match':
                continue
            if time.monotonic() >= deadline:
                reasons.append('结果复核达到时间上限'); break
            item = event['data']
            name = item.get('path', {}).get('text')
            if not name:
                continue
            name = name.replace('\\', '/')
            if name.startswith('./'):
                name = name[2:]
            try:
                path = workspace.relative_path(name)
                if not req.hidden and any(p.startswith('.') for p in Path(path).parts):
                    continue
                if not (root / path).is_relative_to(directory):
                    workspace.fail('搜索结果超出所选目录', 409)
                if current_path != path:
                    current_path = path; current_data = None
                    current_data = _read(scope, path)
                    if current_data is not None:
                        fingerprints[path] = hashlib.sha256(current_data).hexdigest()
                if current_data is None:
                    continue
                result = _snippet(current_data, item)
                if result:
                    matches.append({'path': path, 'sha256': fingerprints[path], **result})
            except (HTTPException, OSError, UnicodeError) as exc:
                if isinstance(exc, HTTPException) and exc.status_code == 409:
                    reasons.append('搜索期间部分文件已改变，请刷新')
                continue
            if len(matches) > MAX_MATCHES:
                matches = matches[:MAX_MATCHES]; reasons.append('匹配行数达到上限'); break
        options = req.model_dump(exclude={'offset', 'snapshot'})
        digest = hashlib.sha256(json.dumps([options, fingerprints, matches, sorted(set(reasons))], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if req.snapshot and req.snapshot != digest:
            workspace.fail('搜索结果已变化，请重新搜索后继续', 409)
        if req.offset and not req.snapshot:
            workspace.fail('翻页需要上一次搜索的快照', 409)
        return {'matches': matches[req.offset:req.offset + PAGE_SIZE], 'total': len(matches),
                'files': len({m['path'] for m in matches}), 'snapshot': digest,
                'next_offset': req.offset + PAGE_SIZE if req.offset + PAGE_SIZE < len(matches) else None,
                'limited': bool(reasons), 'warnings': sorted(set(reasons))}
