"""Read agent command journals and stop an owning run; never launch via HTTP."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import project_commands as commands, workspace
from ..db import get_db
from ..models import ProjectCommand
from .terminals import local_origin

router = APIRouter(prefix='/api', tags=['project-commands'])


@router.get('/projects/{project_id}/commands')
def history(project_id: str, offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    workspace.get_project(db, project_id)
    query = db.query(ProjectCommand).filter_by(project_id=project_id)
    total = query.count()
    rows = query.order_by(ProjectCommand.created_at.desc(), ProjectCommand.id.desc()).offset(offset).limit(40).all()
    return {'commands': [commands.info(row) for row in rows], 'total': total,
            'next_offset': offset + 40 if offset + 40 < total else None}


@router.get('/project-commands/{command_id}')
def detail(command_id: str, offset: int = Query(default=0, ge=0), tail: bool = True, db: Session = Depends(get_db)):
    row = commands.get(db, command_id)
    if tail:
        offset = max(0, len(row.output) - 16000)
    text = row.output[offset:offset + (16000 if tail else 8000)]
    end = offset + len(text)
    return {**commands.info(row), 'output': text, 'output_offset': offset,
            'next_offset': end if end < len(row.output) else None}


@router.post('/project-commands/{command_id}/stop')
def stop(command_id: str, request: Request):
    local_origin(request)
    return commands.stop(command_id)


@router.get('/project-commands/{command_id}/output')
def export(command_id: str, db: Session = Depends(get_db)):
    row = commands.get(db, command_id)
    text = (f'{row.summary}\nStatus: {row.status}\nDirectory: {row.cwd}\n'
            f'Exit code: {row.exit_code}\nAttempt: {row.attempt}\nOutput limited: {bool(row.output_limited)}\n'
            f'Created: {row.created_at.isoformat()} UTC\n\nCommand:\n{row.command}\n\nOutput:\n{row.output}\n')
    if row.error:
        text += f'\nNotice:\n{row.error}\n'
    return PlainTextResponse(text, headers={'Content-Disposition': f'attachment; filename="command-{row.id}.txt"'})
