"""Desktop project registration, file browsing and explicit chat association."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import workspace, task_events, run_store, project_search, workspace_switches, workspace_transfers
from ..agents_md import SCAFFOLD_TEMPLATE
from ..db import get_db
from ..models import Project, Conversation, QARecord, TaskRecord, ForegroundRun, DeletedConversation
from ..conversation_store import ensure_conversation

router = APIRouter(prefix="/api", tags=["projects"])


@router.get('/conversations/{cid}/workspace-transfers')
def list_transfers(cid: str):
    return workspace_transfers.listing(cid)


@router.post('/conversations/{cid}/workspace-transfers')
def prepare_transfer(cid: str, req: workspace_transfers.Prepare):
    return workspace_transfers.prepare(cid, req)


@router.get('/conversations/{cid}/workspace-transfers/{id}/backup')
def transfer_backup(cid: str, id: str, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    row = workspace_transfers._get(db, cid, id)
    return FileResponse(workspace_transfers.backup_path(row), media_type='application/zip', filename='git-transfer-' + row.id + '.zip')


@router.post('/conversations/{cid}/workspace-transfers/{id}/{action}')
def decide_transfer(cid: str, id: str, action: str):
    result = workspace_transfers.decide(cid, id, action)
    task_events.notify_tasks_changed()
    return result


@router.get('/conversations/{cid}/workspace-switches')
def list_switches(cid: str):
    return workspace_switches.listing(cid)


@router.post('/conversations/{cid}/workspace-switches')
def prepare_switch(cid: str, req: workspace_switches.Prepare):
    return workspace_switches.prepare(cid, req)


@router.post('/conversations/{cid}/workspace-switches/{id}/apply')
def apply_switch(cid: str, id: str):
    result = workspace_switches.apply(cid, id)
    task_events.notify_tasks_changed()
    return result


@router.post('/conversations/{cid}/workspace-switches/{id}/cancel')
def cancel_switch(cid: str, id: str):
    return workspace_switches.cancel(cid, id)


def describe(project):
    data = workspace.project_scope(project)
    data["id"] = data.pop("project_id")
    data.pop("root_identity")
    try:
        workspace.root_path(workspace.project_scope(project))
        data.update(available=True, error="")
    except HTTPException as exc:
        data.update(available=False, error=exc.detail)
    return data


class CreateProject(BaseModel):
    path: str = Field(min_length=1, max_length=2048)
    name: str = Field(default="", max_length=120)


@router.get("/projects")
def projects(db: Session = Depends(get_db)):
    return [describe(p) for p in db.query(Project).order_by(Project.created_at.desc()).all()]


@router.post("/projects")
def create(req: CreateProject, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    project = workspace.register(db, req.path, req.name)
    db.commit()
    task_events.notify_tasks_changed()
    return describe(project)


@router.get("/projects/{project_id}/conversations")
def conversations(project_id: str, cursor: str | None = Query(default=None, max_length=512), db: Session = Depends(get_db)):
    import base64
    import json
    from datetime import datetime
    from sqlalchemy import or_, and_
    from ..conversation_activity import conversation_summaries
    workspace.get_project(db, project_id)
    query = db.query(Conversation).filter(Conversation.project_id == project_id, Conversation.archived_at.is_(None),
                                        ~Conversation.id.in_(db.query(DeletedConversation.id)))
    if cursor:
        try:
            pid, at, cid = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if pid != project_id or not isinstance(cid, str):
                raise ValueError()
            at = datetime.fromisoformat(at)
        except (ValueError, TypeError):
            raise HTTPException(400, "项目会话游标无效，请刷新")
        query = query.filter(or_(Conversation.updated_at < at, and_(Conversation.updated_at == at, Conversation.id < cid)))
    rows = query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(41).all()
    more = len(rows) > 40
    rows = rows[:40]
    summaries = {c["id"]: c for c in conversation_summaries(db, ids=[c.id for c in rows])}
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([project_id, last.updated_at.isoformat(), last.id]).encode()).decode()
    return {"conversations": [summaries[c.id] for c in rows if c.id in summaries], "next_cursor": next_cursor}


@router.get("/projects/{project_id}/files")
def files(project_id: str, path: str = Query(default="", max_length=1024),
          offset: int = Query(default=0, ge=0), q: str = Query(default="", max_length=120), db: Session = Depends(get_db)):
    return workspace.list_files(workspace.project_scope(workspace.get_project(db, project_id)), path, offset=offset, query=q)


@router.get("/projects/{project_id}/file")
def file(project_id: str, path: str = Query(max_length=1024), offset: int = Query(default=0, ge=0),
         sha256: str | None = Query(default=None, pattern=r'^[a-f0-9]{64}$'), db: Session = Depends(get_db)):
    result = workspace.read_text(workspace.project_scope(workspace.get_project(db, project_id)), path, offset=offset)
    if sha256 is not None and result['sha256'] != sha256:
        workspace.fail('文件内容已变化，请重新搜索或刷新文件', 409)
    return result


@router.post('/projects/{project_id}/search')
def search(project_id: str, req: project_search.SearchRequest, db: Session = Depends(get_db)):
    return project_search.search(workspace.project_scope(workspace.get_project(db, project_id)), req)


@router.get("/projects/{project_id}/rules")
def rules(project_id: str, db: Session = Depends(get_db)):
    return {"rules": workspace.directory_rules(workspace.project_scope(workspace.get_project(db, project_id)), [])}


@router.post("/projects/{project_id}/init")
def init(project_id: str, db: Session = Depends(get_db)):
    scope = workspace.project_scope(workspace.get_project(db, project_id))
    root = workspace.root_path(scope)
    path = root / "AGENTS.md"
    try:
        with path.open("xb") as target:
            workspace._check_handle(target, scope, path)
            target.write(SCAFFOLD_TEMPLATE.encode("utf-8"))
        created = True
    except FileExistsError:
        workspace.read_text(scope, "AGENTS.md")
        created = False
    except OSError:
        raise HTTPException(400, "无法在项目目录创建 AGENTS.md，请检查权限")
    return {"created": created, "path": str(path), "rules": workspace.directory_rules(scope, [])}


@router.get("/conversations/{cid}/project")
def chat_project(cid: str, db: Session = Depends(get_db)):
    if db.get(DeletedConversation, cid):
        raise HTTPException(410, "会话已删除")
    project = workspace.conversation_project(db, cid)
    return {"project": describe(project) if project else None, "workspace_revision": workspace.binding_revision(db, cid)}


class BindProject(BaseModel):
    project_id: str = Field(min_length=1, max_length=32)


@router.put("/conversations/{cid}/project")
def bind(cid: str, req: BindProject, db: Session = Depends(get_db)):
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        if db.get(DeletedConversation, cid):
            raise HTTPException(410, "会话已删除")
        project = workspace.get_project(db, req.project_id)
        workspace.root_path(workspace.project_scope(project))
        conv = db.get(Conversation, cid)
        if conv and conv.archived_at:
            raise HTTPException(409, "请先恢复此会话")
        if conv and conv.project_id == project.id:
            return {"project": describe(project)}
        if (conv and conv.project_id) or any(db.query(model).filter(model.conversation_id == cid).first()
                for model in (QARecord, TaskRecord, ForegroundRun)) or cid in run_store.active_runs().values():
            raise HTTPException(409, "此会话已开始或已关联项目，请在所选项目中新建会话")
        conv = ensure_conversation(db, cid)
        conv.project_id = project.id
        db.commit()
    task_events.notify_tasks_changed()
    return {"project": describe(project)}
