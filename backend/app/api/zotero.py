"""Personal Zotero collections: read-only remote access, explicit local actions."""
from __future__ import annotations
from typing import Literal
from fastapi import APIRouter,Depends
from pydantic import Field,SecretStr
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import zotero_store as store, bibliography_network as net, source_store as sources
from ..bibliography_models import ConnectorBinding,ConnectorItem,ConnectorAttachmentVersion
from ..credential_protection import protect,CredentialProtectionError
from ..source_runtime import conversation_scope
from ..timeutil import utcnow
from .bibliography import Scope
from .sources import _source_errors

router=APIRouter(prefix='/api/zotero',tags=['zotero'])


class Credentials(Scope):
    user_id:str=Field(pattern=r'^[1-9][0-9]{0,15}$')
    api_key:SecretStr


def key(req):
    value=req.api_key.get_secret_value().strip()
    if not 8<=len(value)<=256 or any(c.isspace() or ord(c)<32 for c in value):
        raise sources.SourceError('Zotero密钥格式无效；请粘贴个人资料库API密钥')
    return value


class Collections(Credentials):
    start:int=Field(default=0,ge=0,le=10000)


@router.post('/collections')
def collections(req:Collections,db:Session=Depends(get_db)):
    with _source_errors(db):
        conversation_scope(db,req.conversation_id);db.rollback()
        try: return store.list_collections(req.user_id,key(req),start=req.start)
        except net.BibliographyError as exc: raise sources.SourceError(str(exc)) from None


class Connect(Credentials):
    collection_key:str=Field(pattern=r'^[A-Z0-9]{8}$')


@router.post('/bindings')
def connect(req:Connect,db:Session=Depends(get_db)):
    with _source_errors(db):
        conversation_scope(db,req.conversation_id);db.rollback()
        value=key(req)
        try:
            access=net.verify_zotero(req.user_id,value)
            collection,_=net.zotero_request(f'/users/{req.user_id}/collections/{req.collection_key}',value)
            if (not isinstance(collection,dict) or collection.get('key')!=req.collection_key
                    or not isinstance(collection.get('data'),dict) or not collection['data'].get('name')):
                raise net.ConnectorError('资料集响应无效，未保存连接')
            protected=protect(value)
        except (net.BibliographyError,CredentialProtectionError) as exc: raise sources.SourceError(str(exc)) from None
        db.execute(text('BEGIN IMMEDIATE'));conversation_scope(db,req.conversation_id)
        row=db.scalar(select(ConnectorBinding).where(ConnectorBinding.conversation_id==req.conversation_id,
            ConnectorBinding.user_id==req.user_id,ConnectorBinding.collection_key==req.collection_key))
        if row is None:
            row=ConnectorBinding(conversation_id=req.conversation_id,user_id=req.user_id,collection_key=req.collection_key,
                collection_name=collection['data']['name'],protected_credential=protected,access=access)
            db.add(row)
        else:
            row.version+=1;row.status='active';row.protected_credential=protected;row.access=access
            row.collection_name=collection['data']['name'];row.last_error='';row.updated_at=utcnow()
        db.commit();return store.output(row)


@router.get('/bindings')
def bindings(conversation_id:str,db:Session=Depends(get_db)):
    with _source_errors(db):
        conversation_scope(db,conversation_id,writing=False)
        return {'items':[store.output(r) for r in db.scalars(select(ConnectorBinding).where(ConnectorBinding.conversation_id==conversation_id)
            .order_by(ConnectorBinding.created_at.desc()))]}


class Version(Scope):
    expected_version:int=Field(ge=1)


class State(Version):
    action:Literal['pause','resume','revoke']


@router.post('/bindings/{binding_id}/state')
def state(binding_id:str,req:State,db:Session=Depends(get_db)):
    with _source_errors(db):
        db.execute(text('BEGIN IMMEDIATE'))
        row=store.get_binding(db,binding_id,req.conversation_id,writing=True)
        if row.version!=req.expected_version: raise sources.SourceConflict('连接状态已改变，请刷新')
        if req.action=='resume' and (row.status not in ('paused','active') or not row.protected_credential):
            raise sources.SourceConflict('请重新验证密钥连接，不能直接恢复失效或已撤销的凭据')
        row.status={'pause':'paused','resume':'active','revoke':'revoked'}[req.action]
        if req.action=='revoke': row.protected_credential=None
        row.version+=1;row.updated_at=utcnow();db.commit()
        return store.output(row)


@router.post('/bindings/{binding_id}/sync')
def sync(binding_id:str,req:Version,db:Session=Depends(get_db)):
    with _source_errors(db):
        row=store.get_binding(db,binding_id,req.conversation_id,writing=True)
        if row.version!=req.expected_version: raise sources.SourceConflict('连接状态已改变，请刷新')
        value=store.credential(row);uid,collection=row.user_id,row.collection_key
        db.rollback()
        def active():
            db.rollback()
            current=store.get_binding(db,binding_id,req.conversation_id,writing=True)
            if current.version!=req.expected_version or current.status!='active':
                raise sources.SourceConflict('同步期间连接已暂停、撤销或发生其他修改；未提交本次快照')
            db.rollback()
        try: version,items=store.snapshot(uid,collection,value,check_cancel=active)
        except net.BibliographyError as exc:
            db.rollback();db.execute(text('BEGIN IMMEDIATE'))
            row=store.get_binding(db,binding_id,req.conversation_id,writing=True)
            if row.version==req.expected_version and row.status=='active':
                row.last_error=str(exc);row.version+=1;row.updated_at=utcnow()
                if getattr(exc,'invalid_credential',False): row.status='invalid'
                db.commit()
            raise sources.SourceError(str(exc)) from None
        db.rollback();db.execute(text('BEGIN IMMEDIATE'))
        row=store.get_binding(db,binding_id,req.conversation_id,writing=True)
        if row.version!=req.expected_version or row.status!='active': raise sources.SourceConflict('同步期间连接已改变，未提交本次快照')
        result=store.commit_snapshot(db,row,version,items);db.commit();return result


@router.get('/bindings/{binding_id}/items')
def items(binding_id:str,conversation_id:str,offset:int=0,db:Session=Depends(get_db)):
    with _source_errors(db):
        store.get_binding(db,binding_id,conversation_id)
        if offset<0 or offset>100000: raise sources.SourceError('分页位置无效')
        rows=list(db.scalars(select(ConnectorItem).where(ConnectorItem.binding_id==binding_id).order_by(ConnectorItem.remote_key)
            .offset(offset).limit(101)))
        return {'items':[store.item_output(r) for r in rows[:100]],'next_offset':offset+100 if len(rows)>100 else None}


@router.get('/bindings/{binding_id}/items/{item_id}/versions')
def attachment_versions(binding_id:str,item_id:str,conversation_id:str,offset:int=0,db:Session=Depends(get_db)):
    with _source_errors(db):
        store.get_binding(db,binding_id,conversation_id)
        item=db.get(ConnectorItem,item_id)
        if item is None or item.binding_id!=binding_id: raise sources.SourceNotFound('此连接没有所选附件')
        if offset<0 or offset>100000: raise sources.SourceError('分页位置无效')
        rows=list(db.scalars(select(ConnectorAttachmentVersion).where(ConnectorAttachmentVersion.item_id==item_id)
            .order_by(ConnectorAttachmentVersion.remote_version.desc()).offset(offset).limit(101)))
        return {'items':[{'remote_version':r.remote_version,'source_revision_id':r.source_revision_id,'remote_md5':r.remote_md5,
            'created_at':r.created_at.isoformat()} for r in rows[:100]],'next_offset':offset+100 if len(rows)>100 else None}


class Attachment(Version):
    remote_version:int=Field(ge=1)


def attachment_item(db,binding_id,item_id,req):
    row=store.get_binding(db,binding_id,req.conversation_id,writing=True)
    item=db.get(ConnectorItem,item_id,populate_existing=True)
    if item is None or item.binding_id!=binding_id: raise sources.SourceNotFound('此连接没有所选附件')
    if row.version!=req.expected_version or item.remote_version!=req.remote_version:
        raise sources.SourceConflict('连接或附件版本已改变，请重新选择')
    data=item.data.get('data',{})
    if item.state!='available' or data.get('itemType')!='attachment' or data.get('contentType')!='application/pdf':
        raise sources.SourceError('此入口仅下载所选资料集中可用的PDF附件')
    return row,item


@router.post('/bindings/{binding_id}/items/{item_id}/download')
def download(binding_id:str,item_id:str,req:Attachment,db:Session=Depends(get_db)):
    from .. import zotero_files
    with _source_errors(db):
        row,item=attachment_item(db,binding_id,item_id,req)
        saved=db.scalar(select(ConnectorAttachmentVersion).where(ConnectorAttachmentVersion.item_id==item.id,
            ConnectorAttachmentVersion.remote_version==item.remote_version))
        if saved:
            sources.read_revision(db,saved.source_revision_id,scope=conversation_scope(db,req.conversation_id))
            return {'revision_id':saved.source_revision_id,'conversation_id':req.conversation_id,'existing':True}
        value=store.credential(row)
        if not row.access.get('files') or item.data['data'].get('linkMode') not in ('imported_file','imported_url'):
            raise sources.SourceError('附件不在Zotero存储或连接没有文件权限；不会读取远端本地路径或任意链接')
        uid,remote_key,md5=row.user_id,item.remote_key,item.data['data'].get('md5')
        db.rollback()
        try:
            raw=zotero_files.download_pdf(uid,remote_key,value,md5);extraction=zotero_files.prepare(raw)
        except (net.BibliographyError,ValueError) as exc: raise sources.SourceError(str(exc)) from None
        db.execute(text('BEGIN IMMEDIATE'));row,item=attachment_item(db,binding_id,item_id,req)
        if row.status!='active': raise sources.SourceConflict('下载期间连接已暂停或撤销，未保存新原件')
        rid=zotero_files.save(db,row,item,raw,extraction);db.commit()
        return {'revision_id':rid,'conversation_id':req.conversation_id,'existing':False}


@router.post('/bindings/{binding_id}/items/{item_id}/attach-fulltext')
def attach_fulltext(binding_id:str,item_id:str,req:Attachment,db:Session=Depends(get_db)):
    from .. import zotero_files
    from ..models import Paper
    with _source_errors(db):
        path=None
        try:
            db.execute(text('BEGIN IMMEDIATE'));row,item=attachment_item(db,binding_id,item_id,req)
            if not item.source_revision_id: raise sources.SourceConflict('请先保存所选版本的PDF原件')
            pid,path=zotero_files.attach_to_metadata(db,row,item,item.source_revision_id);db.commit()
        except Exception:
            db.rollback()
            if path is not None:
                p=db.get(Paper,path.stem)
                if p is None or p.pdf_path!=str(path): path.unlink(missing_ok=True)
            raise
        if path is not None:
            from ..tasks import schedule_parse
            schedule_parse(pid)
        db.expire_all();paper=db.get(Paper,pid)
        return {'paper_id':pid,'index_status':paper.status,'source_revision_id':item.source_revision_id}
