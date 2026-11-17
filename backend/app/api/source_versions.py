"""Read-only fixed version differences; never promote or overwrite a source."""
from fastapi import APIRouter,Depends,Query,UploadFile,File,Form,HTTPException
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import quote
from ..db import get_db
from ..source_diff import compare
from .. import source_update_store as drafts,source_parsing,source_store
from .sources import _scope,_source_errors,_CID,_RID

router=APIRouter(prefix="/api/source-versions",tags=["source-versions"])


@router.get("/compare")
def diff(conversation_id:str=Query(pattern=_CID),left_id:str=Query(pattern=_RID),
         right_id:str=Query(pattern=_RID),offset:int=Query(default=0,ge=0),
         limit:int=Query(default=100,ge=1,le=300),changes_only:bool=True,db:Session=Depends(get_db)):
    with _source_errors(db):
        return compare(db,left_id,right_id,scope=_scope(db,conversation_id),offset=offset,
                       limit=limit,changes_only=changes_only)


@router.get("/{source_id}/update-draft")
def read_draft(source_id:str,conversation_id:str=Query(pattern=_CID),db:Session=Depends(get_db)):
    with _source_errors(db):return drafts.read(db,_scope(db,conversation_id),source_id)


@router.put("/{source_id}/update-draft")
def stage_draft(source_id:str,file:UploadFile=File(),conversation_id:str=Form(pattern=_CID),
                expected_revision:int=Form(ge=0),baseline_revision_id:str=Form(pattern=_RID),
                request_id:str=Form(pattern=r"^[A-Za-z0-9_-]{8,96}$"),db:Session=Depends(get_db)):
    name=file.filename or ""
    ext=name.rsplit(".",1)[-1].casefold()if "."in name else ""
    if (not name or len(name)>255 or name!=name.strip()or any(c in name for c in"/\\")
        or any(ord(c)<32 or ord(c)==127 or 0xD800<=ord(c)<=0xDFFF for c in name)):
        raise HTTPException(422,"待更新原件文件名无效")
    if ext not in source_parsing.MEDIA:raise HTTPException(415,"此格式不能作为待更新原件")
    raw=file.file.read(source_store.MAX_RAW_BYTES+1)
    if not raw or len(raw)>source_store.MAX_RAW_BYTES:raise HTTPException(413,"待更新原件为空或超过100 MB")
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        result=drafts.stage(db,_scope(db,conversation_id,writing=True),source_id,expected_revision=expected_revision,
            baseline_revision_id=baseline_revision_id,request_id=request_id,filename=name,media_type=source_parsing.MEDIA[ext],raw=raw)
        db.commit();return result


class DiscardDraft(BaseModel):
    model_config=ConfigDict(extra="forbid")
    conversation_id:str=Field(pattern=_CID)
    expected_revision:int=Field(ge=0,strict=True)


class StageLocal(DiscardDraft):
    baseline_revision_id:str=Field(pattern=_RID)
    request_id:str=Field(pattern=r"^[A-Za-z0-9_-]{8,96}$")
    project_id:str=Field(min_length=1,max_length=32)
    workspace_revision:int=Field(ge=0,strict=True)
    path:str=Field(min_length=1,max_length=1024)


@router.put("/{source_id}/update-draft/local")
def stage_local(source_id:str,req:StageLocal,db:Session=Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        result=drafts.stage_local(db,_scope(db,req.conversation_id,writing=True),source_id,
            expected_revision=req.expected_revision,baseline_revision_id=req.baseline_revision_id,request_id=req.request_id,
            local={"project_id":req.project_id,"workspace_revision":req.workspace_revision,"path":req.path})
        db.commit();return result


@router.post("/{source_id}/update-draft/discard")
def discard_draft(source_id:str,req:DiscardDraft,db:Session=Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        result=drafts.discard(db,_scope(db,req.conversation_id,writing=True),source_id,req.expected_revision)
        db.commit();return result


@router.get("/{source_id}/update-draft/file")
def draft_file(source_id:str,conversation_id:str=Query(pattern=_CID),revision:int=Query(ge=1),db:Session=Depends(get_db)):
    with _source_errors(db):
        row=drafts.original(db,_scope(db,conversation_id),source_id,revision)
        return Response(row.raw_bytes,media_type="application/octet-stream",headers={
            "Content-Disposition":"attachment; filename*=UTF-8''"+quote(row.filename,safe=""),
            "X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})
