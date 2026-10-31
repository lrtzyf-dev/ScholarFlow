"""Managed worktree operations; arbitrary external directories are never removed."""
from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import worktrees, task_events

router = APIRouter(prefix='/api/projects/{project_id}/worktrees', tags=['worktrees'])


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)


def changed(result):
    task_events.notify_tasks_changed()
    return result


@router.get('')
def listing(project_id: str):
    return worktrees.listing(project_id)


@router.post('')
def create(project_id: str, request: worktrees.Create):
    return changed(worktrees.create(project_id, request))


@router.get('/{id}')
def detail(project_id: str, id: str):
    return worktrees.detail(project_id, id)


@router.post('/{id}/review-removal')
def preview_remove(project_id: str, id: str):
    return worktrees.preview_remove(project_id, id)


@router.post('/{id}/remove')
def remove(project_id: str, id: str, request: Decision):
    return changed(worktrees.remove(project_id, id, request.revision))


@router.post('/{id}/review-archive')
def preview_archive(project_id: str, id: str):
    return worktrees.preview_archive(project_id, id)


@router.post('/{id}/archive')
def archive(project_id: str, id: str, request: Decision):
    return changed(worktrees.archive_worktree(project_id, id, request.revision))


@router.get('/{id}/archive-backup')
def archive_backup(project_id: str, id: str):
    return FileResponse(worktrees.archive_download(project_id, id), media_type='application/zip', filename='worktree-backup.zip')


@router.get('/{id}/archive-history')
def archive_history(project_id: str, id: str):
    return worktrees.archive_history(project_id, id)


@router.delete('/{id}/archive-history/{snapshot_id}')
def delete_archive_snapshot(project_id: str, id: str, snapshot_id: str):
    return changed(worktrees.delete_archive_snapshot(project_id, id, snapshot_id))


@router.post('/{id}/restore')
def restore(project_id: str, id: str, request: Decision):
    return changed(worktrees.restore(project_id, id, request.revision))


@router.post('/{id}/reconcile')
def reconcile(project_id: str, id: str, request: Decision):
    return changed(worktrees.reconcile(project_id, id, request.revision))
