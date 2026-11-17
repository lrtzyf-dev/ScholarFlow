import re
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import execution_environments as store
from ..models import DeletedConversation, ProjectCommand

router=APIRouter(prefix='/api',tags=['execution-environments'])

class Policy(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode: str
    image: str=''
    write_path: str=''
    expected_revision: int=Field(ge=0)

@router.get('/conversation-execution-environments/{cid}')
def get(cid: str,db: Session=Depends(get_db)):
    if db.get(DeletedConversation,cid): raise HTTPException(410,'会话已删除')
    return store.snapshot_policy(db,cid)

@router.put('/conversation-execution-environments/{cid}')
def save(cid: str,body: Policy,db: Session=Depends(get_db)):
    if not re.fullmatch(r'[\w-]{1,64}',cid): raise HTTPException(422,'会话身份无效')
    db.execute(text('BEGIN IMMEDIATE'))
    return store.save_policy(db,cid,body.model_dump(exclude={'expected_revision'}),body.expected_revision)

@router.get('/execution-environments/diagnostics')
def diagnostics(image: str=Query(default='',max_length=320)):
    return store.diagnostics(image)

@router.post('/project-commands/{command_id}/cleanup-container')
def cleanup(command_id: str,db: Session=Depends(get_db)):
    row=db.get(ProjectCommand,command_id)
    if not row or (row.scope.get('execution_environment') or {}).get('mode')!='container':
        raise HTTPException(404,'受限命令不存在')
    from ..project_commands import _running, _guard
    with _guard:
        if command_id in _running: raise HTTPException(409,'命令仍在执行，请先停止')
        result=store.cleanup(command_id,row.project_id)
    return {'confirmed_absent':result,'command_repeated':False,'message':'已确认该命令容器不存在。' if result else '未能确认清理，请核对 Docker 引擎和容器记录。'}
