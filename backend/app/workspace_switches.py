"""Atomic, reviewed chat attachment changes within a managed repository family.

This changes future execution scope only. Git contents, refs, indexes and running
terminals stay in their original projects; this is not a Git-state handoff.
"""
from contextlib import ExitStack
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from . import workspace, worktrees, git_operations, project_git as git, workspace_write, run_store
from .db import SessionLocal
from .models import Conversation, DeletedConversation, DesktopState, ManagedWorktree, WorkspaceSwitch


class Prepare(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,64}$')
    target_project_id: str = Field(min_length=1, max_length=32)
    workspace_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=0)


def _conversation(db, cid):
    if db.get(DeletedConversation, cid):
        workspace.fail('会话已删除', 410)
    conv = db.get(Conversation, cid)
    if not conv or not conv.project_id:
        workspace.fail('请先关联一个项目', 409)
    if conv.archived_at:
        workspace.fail('请先恢复会话再切换工作目录', 409)
    return conv


def _get(db, cid, id):
    row = db.get(WorkspaceSwitch, id)
    if not row or row.conversation_id != cid:
        workspace.fail('工作目录切换记录不存在', 404)
    return row


def describe(row):
    return {key: getattr(row, key) for key in ('id', 'conversation_id', 'request_id', 'source_project_id',
        'target_project_id', 'workspace_revision', 'snapshot', 'status', 'created_at', 'completed_at')}


def _scope(db, project_id):
    project = workspace.get_project(db, project_id)
    scope = workspace.project_scope(project)
    root = git_operations._repository(scope)
    managed = db.query(ManagedWorktree).filter_by(project_id=project_id).first()
    if managed:
        if managed.status != 'ready':
            workspace.fail('工作树不可用，请先恢复或核对记录', 409)
        worktrees._validate_checkout(managed, worktrees._repo(db, managed))
    entry = worktrees._matching(root, root)
    if not entry or any(key in entry for key in ('locked', 'prunable', 'bare')):
        workspace.fail('Git 工作目录记录不完整或被锁定，请先在外部 Git 工具处理', 409)
    return scope, root


def _state(scope, root):
    state = git.status(scope)
    # Status is an informational review, not a byte snapshot of file contents.
    return {**scope, 'branch': state['branch'],
        'head': git._text(git._git(root, 'rev-parse', '--verify', 'HEAD')).strip(),
        'status_fingerprint': state['snapshot'], 'changes': state['total'],
        'staged': state['staged_total'], 'unstaged': state['unstaged_total']}


def _inspect(db, conv, target_id, stack):
    if conv.project_id == target_id:
        workspace.fail('会话已经在此工作目录', 409)
    if worktrees._anchor(db, conv.project_id) != worktrees._anchor(db, target_id):
        workspace.fail('只能在来源项目与其托管工作树之间切换', 409)
    source, source_root = _scope(db, conv.project_id)
    target, target_root = _scope(db, target_id)
    common = worktrees._common(source_root)
    if common != worktrees._common(target_root):
        workspace.fail('两个目录不属于同一 Git 仓库', 409)
    for directory in (source_root, target_root, common):
        handle = stack.enter_context(workspace_write._handle(directory, directory=True))
        if workspace.handle_path(handle, directory) != directory:
            workspace.fail('目录身份已变化，请刷新后重试', 409)
    workspace.root_path(source); workspace.root_path(target)
    return {'source': _state(source, source_root), 'target': _state(target, target_root),
        'repository': {'path': str(common), 'identity': workspace.identity(common)}}


def _draft(db, cid):
    return db.get(DesktopState, f'conversation:{cid}')


def prepare(cid, request):
    from .api.conversations import _require_idle
    with worktrees._guard, git_operations._guard, run_store.coordinated(), SessionLocal() as db, ExitStack() as stack:
        db.execute(text('BEGIN IMMEDIATE'))
        conv = _conversation(db, cid)
        old = db.query(WorkspaceSwitch).filter_by(request_id=request.request_id).first()
        if old:
            if (old.conversation_id, old.target_project_id, old.workspace_revision, old.snapshot['draft_revision']) != (
                    cid, request.target_project_id, request.workspace_revision, request.draft_revision):
                workspace.fail('请求编号已用于其他切换，请重新审阅', 409)
            return describe(old)
        workspace.check_revision(db, cid, request.workspace_revision)
        _require_idle(db, cid, '切换工作目录')
        draft = _draft(db, cid)
        if (draft.revision if draft else 0) != request.draft_revision:
            workspace.fail('会话草稿已变化，请重新载入并审阅', 409)
        snapshot = _inspect(db, conv, request.target_project_id, stack)
        value = draft.value if draft else {}
        snapshot.update(draft_revision=request.draft_revision, file_paths=value.get('file_paths') or [],
            editor={k: value['editor'].get(k) for k in ('project_id', 'path')} if value.get('editor') else None)
        row = WorkspaceSwitch(conversation_id=cid, request_id=request.request_id,
            source_project_id=conv.project_id, target_project_id=request.target_project_id,
            workspace_revision=conv.workspace_revision or 0, snapshot=snapshot)
        db.add(row); db.commit()
        return describe(row)


def apply(cid, id):
    from .api.conversations import _require_idle
    from .api.desktop_state import ConversationState
    with worktrees._guard, git_operations._guard, run_store.coordinated(), SessionLocal() as db, ExitStack() as stack:
        db.execute(text('BEGIN IMMEDIATE'))
        conv = _conversation(db, cid)
        row = _get(db, cid, id)
        if row.status == 'applied':
            return describe(row)
        if row.status != 'prepared':
            workspace.fail('此审阅已取消或过期，请重新审阅', 409)
        workspace.check_revision(db, cid, row.workspace_revision)
        if conv.project_id != row.source_project_id:
            workspace.fail('会话来源已变化，请重新审阅', 409)
        _require_idle(db, cid, '切换工作目录')
        draft = _draft(db, cid)
        if (draft.revision if draft else 0) != row.snapshot['draft_revision']:
            workspace.fail('会话草稿已变化，请重新载入并审阅', 409)
        current = _inspect(db, conv, row.target_project_id, stack)
        if any(current[key] != row.snapshot[key] for key in current):
            workspace.fail('目录、Git 提交、分支或改动列表已变化，请重新审阅', 409)
        now = datetime.utcnow()
        conv.project_id = row.target_project_id
        conv.workspace_revision = row.workspace_revision + 1
        conv.updated_at = now
        value = ConversationState.model_validate(draft.value if draft else {}).model_dump()
        # Preserve text, images, reading state and the editor's explicit project.
        # Relative file references must be selected again in the target checkout.
        value.update(workspace_revision=conv.workspace_revision, file_paths=[])
        if draft:
            draft.value = value; draft.revision += 1; draft.updated_at = now
        else:
            db.add(DesktopState(key=f'conversation:{cid}', value=value))
        row.status = 'applied'; row.completed_at = now
        for other in db.query(WorkspaceSwitch).filter_by(conversation_id=cid, status='prepared').all():
            if other.id != row.id:
                other.status = 'superseded'
        db.commit()
        return describe(row)


def listing(cid):
    with SessionLocal() as db:
        _conversation(db, cid)
        rows = db.query(WorkspaceSwitch).filter_by(conversation_id=cid).order_by(WorkspaceSwitch.created_at.desc()).limit(30).all()
        return {'switches': [describe(row) for row in rows]}


def cancel(cid, id):
    with SessionLocal() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        _conversation(db, cid)
        row = _get(db, cid, id)
        if row.status == 'prepared':
            row.status = 'cancelled'; db.commit()
        return describe(row)
