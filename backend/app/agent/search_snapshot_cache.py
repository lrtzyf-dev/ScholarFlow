"""Safe search reuse for complete bounded inputs, with a producer receipt.

The fast path uses real ripgrep over immutable stdin bytes. Complex ignore/glob
selection and incomplete captures retain the ordinary scoped live search.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType

from fastapi import HTTPException

from .readonly_versions import Budget, VersionUnavailable, digest


_active = ContextVar('scholarflow_search_snapshot', default=None)
MAX_INPUT_FILES = 32
MAX_INPUT_BYTES = 8 * 1024 * 1024
IGNORE_FILES = {'.gitignore', '.ignore', '.rgignore'}


@dataclass(frozen=True)
class Snapshot:
    scope: dict
    request: dict
    files: object
    fingerprint: str


def _read_bytes(scope, relative, budget):
    from .. import workspace
    path=workspace.scoped_path(scope,relative)
    budget.consume(files=1)
    with path.open('rb') as file:
        workspace._check_handle(file,scope,path)
        before=os.fstat(file.fileno())
        if before.st_size>workspace.MAX_FILE_BYTES:
            raise VersionUnavailable('存在超过搜索快照限制的文件')
        budget.consume(before.st_size)
        data=file.read(workspace.MAX_FILE_BYTES+1)
        after=os.fstat(file.fileno())
        workspace._check_handle(file,scope,path)
    if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=
            (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
            or len(data)!=after.st_size or len(data)>workspace.MAX_FILE_BYTES):
        raise VersionUnavailable('搜索快照读取期间文件已变化')
    workspace.scoped_path(scope,relative)
    return data


def capture(scope,request):
    from .. import workspace
    if request.include or request.exclude:
        raise VersionUnavailable('文件范围表达式使用实际搜索')
    root=workspace.root_path(scope);target=workspace.scoped_path(scope,request.path)
    if not target.is_dir():raise VersionUnavailable('搜索快照需要目录')
    # stdin bypasses filesystem ignore processing. Prove that no relevant
    # ignore file exists; an explicit --no-ignore search needs no such proof.
    if not request.ignored:
        parent=target
        while True:
            if any((parent/name).exists() or (parent/name).is_symlink() for name in IGNORE_FILES):
                raise VersionUnavailable('忽略规则使用实际搜索')
            if parent==root:break
            parent=parent.parent
    budget=Budget(size=MAX_INPUT_BYTES,files=MAX_INPUT_FILES*4+256,seconds=2)
    files,manifest={},[]
    def children(directory):
        # Query canonical names through the filesystem too: Windows case
        # equivalence is not identical to Python Unicode casefold.
        if not request.ignored and any((directory/name).exists() or (directory/name).is_symlink()
                                      for name in IGNORE_FILES):
            raise VersionUnavailable('忽略规则使用实际搜索')
        entries=[]
        with os.scandir(directory) as iterator:
            for item in iterator:
                budget.consume(files=1)
                if item.name.casefold() in workspace.SKIP_DIRS:continue
                if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
                    raise VersionUnavailable('链接范围使用实际搜索')
                if not request.ignored and item.name.casefold() in IGNORE_FILES:
                    raise VersionUnavailable('忽略规则使用实际搜索')
                path=workspace.scoped_path(scope,Path(item.path).relative_to(root).as_posix())
                kind='directory' if path.is_dir() else 'file' if path.is_file() else 'other'
                if kind=='other':raise VersionUnavailable('搜索目录存在不可核对对象')
                entries.append((item.name,kind))
        return sorted(entries)
    def visit(directory,depth=0):
        if depth>64:raise VersionUnavailable('搜索目录深度使用实际搜索')
        before=children(directory)
        for name,kind in before:
            path=directory/name;relative=path.relative_to(root).as_posix()
            if kind=='directory':
                manifest.append([relative,'directory']);visit(path,depth+1)
            else:
                if len(files)>=MAX_INPUT_FILES:raise VersionUnavailable('搜索目录使用实际搜索')
                data=_read_bytes(scope,relative,budget)
                files[relative]=data;manifest.append([relative,hashlib.sha256(data).hexdigest(),len(data)])
        if children(directory)!=before:raise VersionUnavailable('搜索快照目录成员已变化')
    visit(target)
    workspace.root_path(scope)
    return Snapshot(dict(scope),request.model_dump(),MappingProxyType(files),digest(manifest))


@contextmanager
def bind(snapshot):
    context={'snapshot':snapshot,'receipt':None}
    token=_active.set(context)
    try:yield context
    finally:_active.reset(token)


def active(scope=None,request=None,root=None):
    context=_active.get()
    if context is None:return None
    snapshot=context['snapshot']
    if scope is not None and scope!=snapshot.scope:return None
    if root is not None and Path(root)!=Path(snapshot.scope['root_path']):return None
    if request is not None and request.model_dump()!=snapshot.request:return None
    return snapshot


def source_bytes(scope,path):
    snapshot=active(scope=scope)
    if snapshot is None:return None
    if path not in snapshot.files:raise VersionUnavailable('搜索器返回快照外文件')
    return snapshot.files[path]


def receipt(result):
    context=_active.get()
    context['receipt']={'input_fingerprint':context['snapshot'].fingerprint,'result_sha256':digest(result)}


def run(snapshot,request):
    from .. import project_search
    budget=Budget(size=project_search.MAX_OUTPUT,files=MAX_INPUT_FILES+1,seconds=project_search.SCAN_SECONDS)
    output=[];reasons=[];matched=False
    candidates=[]
    for path,data in snapshot.files.items():
        if not request.hidden and any(part.startswith('.') for part in Path(path).parts):continue
        if b'\x00' in data:continue
        try:data.decode('utf-8-sig')
        except UnicodeError:continue
        candidates.append((path,data))
    # Validate regex even for a complete empty directory.
    for path,data in sorted(candidates) or [('',b'')]:
        try:budget.consume(files=1)
        except VersionUnavailable:
            reasons.append('搜索达到时间上限');break
        raw,code,error,limited=project_search._run_stdin(data,request,path,budget)
        if code not in (0,1) and not limited:return raw,code,error,[]
        if code==0:matched=True
        output.append(raw);reasons.extend(limited)
        if limited:break
    return b'\n'.join(output),0 if matched else 1,'',reasons


def cached_search(arguments,*,scope,binding,conversation_id,cache,authorize,action):
    from ..project_search import SearchRequest
    from pydantic import ValidationError
    from .readonly_cache import _eligible,store_entry
    try:
        request=SearchRequest.model_validate(arguments)
        snapshot=capture(scope,request)
    except (VersionUnavailable,OSError,UnicodeError,HTTPException,ValidationError):
        return action()
    authorize()
    identity={'tool':'search_project_files','version':'stdin-snapshot/1','input_fingerprint':snapshot.fingerprint,
        'arguments':request.model_dump(),'workspace':dict(scope),'conversation_id':conversation_id,
        'run_id':binding.get('run_id'),'policy_revision':binding.get('policy_revision')}
    key='search-snapshot/'+digest(identity);item=cache.get(key)
    hit=False
    if isinstance(item,dict) and _eligible(item.get('result')):
        hit=(item.get('identity')==identity
            and hashlib.sha256(item['result'].encode()).hexdigest()==item.get('result_sha256')
            and item.get('receipt')=={'input_fingerprint':snapshot.fingerprint,
                'result_sha256':digest(json.loads(item['result']))})
    observation=None
    if hit:result=item['result']
    else:
        with bind(snapshot) as observation:result=action()
    authorize()
    try:current=capture(scope,request)
    except (VersionUnavailable,OSError,UnicodeError,HTTPException):
        cache.pop(key,None)
        return action() if hit else result
    authorize()
    if current.fingerprint!=snapshot.fingerprint:
        cache.pop(key,None)
        return action() if hit else result
    expected={'input_fingerprint':snapshot.fingerprint,'result_sha256':digest(json.loads(result))} if _eligible(result) else None
    actual=item.get('receipt') if hit else observation['receipt']
    if expected is not None and actual==expected:
        store_entry(cache,key,{'identity':identity,'result':result,'result_sha256':hashlib.sha256(result.encode()).hexdigest(),'receipt':expected})
    else:cache.pop(key,None)
    return result
