"""Persisted, reviewable file edits with optimistic checks and crash reconciliation."""
from __future__ import annotations

from datetime import datetime
import difflib
import hashlib
import json
import os
import threading
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import workspace, workspace_write, run_store, task_events
from .conversation_store import check_conversation_available, ConversationArchived, ConversationDeleted
from .db import SessionLocal
from .models import ProjectChange, TaskRecord, ForegroundRun

MAX_EDIT_BYTES = 256 * 1024
_guard = threading.RLock()


class EditRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=1024)
    operation: Literal['update', 'create', 'delete'] = 'update'
    expected_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    content: str | None = Field(default=None, max_length=MAX_EDIT_BYTES)
    summary: str = Field(default='', max_length=500)

    @model_validator(mode='after')
    def valid(self):
        if self.operation != 'create' and not self.expected_sha256:
            raise ValueError('修改或删除前必须读取文件并提供原始 SHA256')
        if self.operation == 'create' and self.expected_sha256 is not None:
            raise ValueError('新建文件不能提供旧文件 SHA256')
        if self.operation != 'delete' and self.content is None:
            raise ValueError('必须提供完整的新文件内容')
        if self.operation == 'delete' and self.content is not None:
            raise ValueError('删除操作不能提供新内容')
        if self.content is not None:
            validate_text(self.content)
        return self


def validate_text(value):
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        workspace.fail('文件包含无效 Unicode 字符')
    if '\x00' in value or size > MAX_EDIT_BYTES or value.count('\n') + bool(value and not value.endswith('\n')) > 20000:
        workspace.fail('编辑仅支持最多 256 KiB、20,000 行的 UTF-8 文本')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_original(scope, path):
    target = workspace_write.target(scope, path)
    if not target.exists():
        return None, None
    try:
        with target.open('rb') as file:
            workspace._check_handle(file, scope, target)
            if os.fstat(file.fileno()).st_nlink != 1:
                workspace.fail('暂不修改有多个硬链接的文件，请使用独立文件')
            data = file.read(MAX_EDIT_BYTES + 1)
            workspace._check_handle(file, scope, target)
        value = data.decode('utf-8')
        validate_text(value)
        return value, digest(data)
    except UnicodeError:
        workspace.fail('文件不是可编辑的 UTF-8 文本')
    except OSError:
        workspace.fail('无法读取文件，请检查权限或占用情况', 409)


def available(db, cid):
    try:
        check_conversation_available(db, cid)
    except ConversationDeleted as exc:
        workspace.fail(str(exc), 410)
    except ConversationArchived as exc:
        workspace.fail(str(exc), 409)


def _request(args):
    try:
        return EditRequest.model_validate(args)
    except ValueError as exc:
        workspace.fail(str(exc))


def _new_text(request, before, exact=False):
    value = request.content
    if value is None or exact:
        return value
    if before and before.startswith('\ufeff') and not value.startswith('\ufeff'):
        value = '\ufeff' + value
    if before and '\r\n' in before and before.count('\r\n') == before.count('\n'):
        value = value.replace('\r\n', '\n').replace('\n', '\r\n')
    validate_text(value)
    return value


def prepare(scope, cid, args, *, run_id=None, operation_key=None, undo_of=None, exact=False):
    request = _request(args)
    request.path = workspace.relative_path(request.path)
    fingerprint = digest(json.dumps(request.model_dump(), sort_keys=True, ensure_ascii=False).encode())
    with _guard, run_store.coordinated(), SessionLocal() as db:
        available(db, cid)
        workspace.validate_scope(db, cid, scope)
        if not scope:
            workspace.fail('请先关联项目文件夹')
        if operation_key:
            existing = db.query(ProjectChange).filter_by(operation_key=operation_key).first()
            if existing:
                if (existing.request_hash != fingerprint or existing.project_id != scope['project_id'] or existing.conversation_id != cid
                        or existing.scope.get('workspace_revision', 0) != scope.get('workspace_revision', 0)):
                    workspace.fail('同一执行步骤的修改内容不一致，拒绝复用', 409)
                return summary(existing)
        before, before_hash = read_original(scope, request.path)
        if before_hash != request.expected_sha256:
            workspace.fail('文件已发生变化，请重新读取后生成修改', 409)
        after = _new_text(request, before, exact)
        if after == before:
            workspace.fail('文件内容没有变化')
        project_scope = {k: scope[k] for k in ('project_id', 'name', 'root_path', 'root_identity')}
        project_scope['workspace_revision'] = scope.get('workspace_revision', 0)
        project_scope['rules'] = workspace.directory_rules(scope, [request.path])
        row = ProjectChange(project_id=scope['project_id'], conversation_id=cid, run_id=run_id,
            operation_key=operation_key, request_hash=fingerprint, scope=project_scope,
            path=request.path, operation=request.operation, summary=request.summary,
            before_text=before, after_text=after, before_hash=before_hash,
            after_hash=digest(after.encode('utf-8')) if after is not None else None, undo_of=undo_of)
        db.add(row); db.commit(); db.refresh(row)
        result = summary(row)
    task_events.notify_tasks_changed()
    return result


def get(db, change_id):
    row = db.get(ProjectChange, change_id)
    if row is None:
        workspace.fail('文件修改记录不存在', 404)
    return row


def summary(row):
    return {k: getattr(row, k) for k in ('id', 'project_id', 'conversation_id', 'run_id', 'path', 'operation',
            'summary', 'before_hash', 'after_hash', 'status', 'error', 'revision', 'undo_of')} | {
        'created_at': row.created_at.isoformat(), 'updated_at': row.updated_at.isoformat()}


def diff(row, offset=0, limit=400):
    before = (row.before_text or '').removeprefix('\ufeff').splitlines(keepends=True)
    after = (row.after_text or '').removeprefix('\ufeff').splitlines(keepends=True)
    lines, added, removed = [], 0, 0
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=True)
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag in ('replace', 'delete'): removed += b - a
        if tag in ('replace', 'insert'): added += d - c
    for group in matcher.get_grouped_opcodes(3):
        a, b, c, d = group[0][1], group[-1][2], group[0][3], group[-1][4]
        lines.append({'kind': 'hunk', 'text': f'@@ -{a + 1},{b - a} +{c + 1},{d - c} @@', 'old': None, 'new': None})
        for tag, a, b, c, d in group:
            if tag == 'equal':
                for i, j in zip(range(a, b), range(c, d)):
                    lines.append({'kind': 'context', 'text': before[i].rstrip('\r\n'), 'old': i + 1, 'new': j + 1})
            else:
                for i in range(a, b): lines.append({'kind': 'remove', 'text': before[i].rstrip('\r\n'), 'old': i + 1, 'new': None})
                for j in range(c, d): lines.append({'kind': 'add', 'text': after[j].rstrip('\r\n'), 'old': None, 'new': j + 1})
    def format_info(value):
        return None if value is None else {'bom': value.startswith('\ufeff'), 'final_newline': value.endswith('\n'),
                'crlf': value.count('\r\n'), 'lf': value.count('\n') - value.count('\r\n'), 'bytes': len(value.encode('utf-8'))}
    return {'lines': lines[offset:offset + limit], 'next_offset': offset + limit if offset + limit < len(lines) else None,
            'total_lines': len(lines), 'added': added, 'removed': removed,
            'before_format': format_info(row.before_text), 'after_format': format_info(row.after_text)}


def run_active(row):
    from . import task_runner
    if row.run_id and (row.run_id in run_store.active_runs() or row.run_id in task_runner.running_task_ids()):
        return True
    if row.run_id:
        with SessionLocal() as db:
            task = db.get(TaskRecord, row.run_id)
            foreground = db.get(ForegroundRun, row.run_id)
            return bool((task and task.status in ('pending', 'running')) or
                        (foreground and foreground.status == 'running'))
    return False


def _status(db, row, status, error=''):
    row.status, row.error = status, error
    row.revision += 1; row.updated_at = datetime.utcnow()
    db.commit()
    task_events.notify_tasks_changed()


def _reconcile(db, row):
    try:
        _, actual = read_original(row.scope, row.path)
        if actual == row.after_hash:
            _status(db, row, 'applied', '恢复时已核对磁盘内容与修改结果一致')
        elif actual == row.before_hash:
            _status(db, row, 'proposed', '上次写入中断，磁盘仍为原内容，可重新审阅后应用')
        else:
            _status(db, row, 'interrupted', '上次写入中断，当前文件与修改前后均不同；请查看磁盘文件并重新编辑，未自动覆盖')
    except (HTTPException, OSError) as exc:
        _status(db, row, 'interrupted', str(getattr(exc, 'detail', exc)))


def recover_interrupted():
    with _guard, SessionLocal() as db:
        for row in db.query(ProjectChange).filter_by(status='applying').all():
            _reconcile(db, row)


def apply(change_id, *, revision=None, run_id=None):
    with _guard, run_store.coordinated(), SessionLocal() as db:
        row = get(db, change_id)
        if row.status == 'applied':
            return summary(row)
        if run_id != row.run_id and run_active(row):
            workspace.fail('此修改仍由运行中的任务处理，请通过该任务的审批操作', 409)
        if revision is not None and revision != row.revision:
            workspace.fail('修改记录已更新，请刷新后重试', 409)
        if row.status not in ('proposed', 'conflict'):
            workspace.fail('此修改不能直接应用，请根据当前文件重新编辑', 409)
        available(db, row.conversation_id)
        workspace.validate_scope(db, row.conversation_id, row.scope)
        if workspace.directory_rules(row.scope, [row.path]) != row.scope['rules']:
            _status(db, row, 'conflict', '适用的目录规范已修改，请重新生成修改')
            workspace.fail(row.error, 409)
        # Journal the intent before creating directories/files or changing bytes.
        _status(db, row, 'applying')
        try:
            with workspace_write.locked_target(row.scope, row.path, create=row.operation == 'create', delete=row.operation == 'delete') as file:
                data = file.read(MAX_EDIT_BYTES + 1)
                if row.operation != 'create' and digest(data) != row.before_hash:
                    _status(db, row, 'conflict', '文件已被其他操作修改，未覆盖；请重新读取后生成修改')
                    workspace.fail(row.error, 409)
                if row.operation == 'delete':
                    workspace_write.delete_content(file)
                else:
                    workspace_write.write_content(file, row.after_text.encode('utf-8'))
            _status(db, row, 'applied')
        except (OSError, HTTPException) as exc:
            if row.status == 'applying':
                if isinstance(exc, OSError) and exc.errno in (80, 183):
                    _status(db, row, 'conflict', '目标文件已经存在，未覆盖；请重新读取后编辑')
                    workspace.fail(row.error, 409)
                if isinstance(exc, OSError) and exc.errno in (2, 3) and row.operation != 'create':
                    _status(db, row, 'conflict', '原文件或目录已被外部删除，未写入；请重新读取后编辑')
                    workspace.fail(row.error, 409)
                # Any write exception may be partial. Reconcile before exposing retry.
                _reconcile(db, row)
                if row.status == 'proposed':
                    _status(db, row, 'conflict', str(getattr(exc, 'detail', exc)))
            if row.status == 'applied':
                return summary(row)
            workspace.fail(row.error or '写入未完成，请检查文件并重试', 409)
        return summary(row)


def reject(change_id, revision=None):
    with _guard, SessionLocal() as db:
        row = get(db, change_id)
        if row.status == 'rejected': return summary(row)
        if revision is not None and row.revision != revision:
            workspace.fail('修改记录已更新，请刷新', 409)
        if row.status not in ('proposed', 'conflict'):
            workspace.fail('此修改已应用或中断，不能拒绝', 409)
        _status(db, row, 'rejected')
        return summary(row)


def undo(change_id, cid):
    with _guard, SessionLocal() as db:
        row = get(db, change_id)
        if row.status != 'applied': workspace.fail('只能撤销已应用的修改', 409)
        project = workspace.conversation_project(db, cid)
        if project is None: workspace.fail('请在此项目的会话中撤销修改', 409)
        scope = workspace.project_scope(project)
        scope['workspace_revision'] = workspace.binding_revision(db, cid)
        if scope['project_id'] != row.project_id: workspace.fail('撤销会话不属于此项目', 409)
        existing = db.query(ProjectChange).filter(ProjectChange.undo_of == row.id, ProjectChange.status.in_(['proposed', 'conflict', 'applied'])).order_by(
            (ProjectChange.status == 'applied').desc(), ProjectChange.created_at.desc()).first()
        if existing and not (existing.status != 'applied' and existing.conversation_id == cid
                and existing.scope.get('workspace_revision', 0) != scope['workspace_revision']):
            return summary(existing)
        return prepare(scope, cid, {'path': row.path, 'operation': 'delete' if row.before_text is None else 'create' if row.after_text is None else 'update',
            'expected_sha256': row.after_hash, 'content': row.before_text, 'summary': f'撤销：{row.summary or row.path}'}, undo_of=row.id, exact=True)
