from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import diagnostic_records as records
from ..diagnostics import run_diagnostics
from ..models import ForegroundRun, TaskRecord, DeletedConversation
from ..source_runtime import conversation_scope
from ..source_models import Source, SourceRevision
from ..timeutil import utcnow

router=APIRouter(prefix="/api/debug/records",tags=["diagnostic-history"])


class Context(BaseModel):
    conversation_id: str | None=Field(default=None,max_length=64,pattern=r'^[\w-]+$')
    run_id: str | None=Field(default=None,max_length=96,pattern=r'^[\w:.-]+$')
    source_revision_id: str | None=Field(default=None,max_length=64,pattern=r'^[a-f0-9]{32}$')


def validate_context(db,req):
    if req.conversation_id and db.get(DeletedConversation,req.conversation_id): raise HTTPException(410,"会话已删除")
    if req.run_id:
        run=db.get(ForegroundRun,req.run_id) or db.get(TaskRecord,req.run_id)
        if not run or run.conversation_id!=req.conversation_id: raise HTTPException(404,"运行不属于此会话")
    if req.source_revision_id:
        if not req.conversation_id: raise HTTPException(422,"资料诊断需要所属会话")
        # A corrupted payload is precisely a reason to open diagnostics. Check
        # ownership from metadata without reading, hashing or exporting bytes.
        scope=conversation_scope(db,req.conversation_id,writing=False)
        found=db.scalar(select(SourceRevision.id).join(Source,SourceRevision.source_id==Source.id).where(
            SourceRevision.id==req.source_revision_id,Source.scope_type==scope.scope_type,Source.scope_id==scope.scope_id))
        if not found: raise HTTPException(404,"资料版本不属于此会话")


@router.post("/local")
def save_local(req:Context,db:Session=Depends(get_db)):
    validate_context(db,req)
    report=run_diagnostics(req.conversation_id)
    result=records.save(db,report,kind="local",**req.model_dump())
    db.commit()
    return result


@router.get("")
def history(conversation_id:str|None=Query(default=None,max_length=64),before_id:str|None=Query(default=None,max_length=64),
            limit:int=Query(default=20,ge=1,le=100),db:Session=Depends(get_db)):
    query=select(records.DiagnosticRecord)
    if conversation_id: query=query.where(records.DiagnosticRecord.conversation_id==conversation_id)
    if before_id:
        before=db.get(records.DiagnosticRecord,before_id)
        if not before or conversation_id and before.conversation_id!=conversation_id: raise HTTPException(422,"诊断游标无效")
        from sqlalchemy import and_,or_
        query=query.where(or_(records.DiagnosticRecord.created_at<before.created_at,
            and_(records.DiagnosticRecord.created_at==before.created_at,records.DiagnosticRecord.id<before.id)))
    rows=db.scalars(query.order_by(records.DiagnosticRecord.created_at.desc(),records.DiagnosticRecord.id.desc()).limit(limit+1)).all()
    return {"items":[records.public(db,row) for row in rows[:limit]],"next_id":rows[limit-1].id if len(rows)>limit else None}


@router.get("/{record_id}/export-preview")
def preview(record_id:str,db:Session=Depends(get_db)):
    row=db.get(records.DiagnosticRecord,record_id)
    if not row: raise HTTPException(404,"诊断记录不存在")
    return {"schema":"scholarflow-diagnostics-v1","record":records.public(db,row),
        "notice":"仅含本地检查状态、接口错误分类和调用回执ID；不含凭证、原文、原始响应或本地路径。导出不会发送给他人。"}


@router.get("/{record_id}/calls")
def calls(record_id:str,db:Session=Depends(get_db)):
    from ..call_ledger import get_call, CallLedgerError
    row=db.get(records.DiagnosticRecord,record_id)
    if not row: raise HTTPException(404,"诊断记录不存在")
    allowed=("call_id","state","usage_source","actual_usage","estimated_usage","synthetic","pricing","amount","currency","amount_source","amount_scope")
    items=[]
    for call_id in row.payload.get("call_ids",[]):
        try:
            receipt=get_call(call_id,db=db)
            item={key:receipt.get(key) for key in allowed}
            pricing=item.get('pricing') or {}
            item['pricing']={key:pricing.get(key) for key in ('status','amount','currency','provider_bill_confirmed')}
            items.append(item)
        except CallLedgerError:
            items.append({"call_id":call_id,"state":"receipt_unavailable"})
    return {"items":items,"notice":"用量与费用分别展示；未知不等于零。回执不能证明超时请求未执行，也不会触发重试。"}


@router.post("/actions/{action_id}/dismiss")
def dismiss(action_id:str,db:Session=Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    row=db.get(records.DiagnosticRecoveryAction,action_id)
    if not row: raise HTTPException(404,"恢复建议不存在")
    row.state,row.updated_at="dismissed",utcnow()
    db.commit()
    return {"state":"dismissed","repaired":False}
