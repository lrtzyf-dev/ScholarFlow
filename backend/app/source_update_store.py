"""CAS draft staging. No parsing, model calls or library import at selection."""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import func,select
from . import source_store as store
from .source_update_models import SourceUpdateDraft
from .timeutil import utcnow

MAX_CONVERSATION_DRAFT_BYTES=300*1024**2


def _identity(row):
    return [row.conversation_id,row.source_id,row.revision,row.state,row.kind,row.local_context,row.filename,row.media_type,
            row.raw_sha256,row.raw_size,row.expected_version,row.baseline_revision_id,row.request_id]


def _hash(value):return hashlib.sha256(value).hexdigest()


def _fingerprint(row):
    return _hash(json.dumps(_identity(row),ensure_ascii=False,separators=(",",":")).encode())


def _read(db,scope,source_id,*,body=False):
    # Authorization first, even for a cancelled/empty draft.
    store.current_source(db,source_id,scope=scope)
    row=db.get(SourceUpdateDraft,(scope.scope_id,source_id),populate_existing=True)
    # Earlier staged uploads have no local-kind fields. Accept only their exact
    # old digest, never bless unknown bytes when adding columns on upgrade.
    legacy_valid=row is not None and row.kind=="upload"and row.local_context is None and row.fingerprint==_hash(
        json.dumps(_identity(row)[:4]+_identity(row)[6:],ensure_ascii=False,separators=(",",":")).encode())
    if row is not None and (row.fingerprint!=_fingerprint(row)and not legacy_valid or body and row.kind=="upload"and row.state=="pending"and (
        row.raw_bytes is None or len(row.raw_bytes)!=row.raw_size or _hash(row.raw_bytes)!=row.raw_sha256)):
        raise store.SourceCorruption("待更新原件校验失败，请重新选择文件")
    return row


def describe(row):
    if row is None:return {"revision":0,"state":"empty","draft":None}
    return {"revision":row.revision,"state":row.state,"draft":None if row.state!="pending"else{
        "filename":row.filename,"media_type":row.media_type,"raw_sha256":row.raw_sha256,"kind":row.kind,"local":row.local_context,
        "raw_size":row.raw_size,"expected_version":row.expected_version,
        "baseline_revision_id":row.baseline_revision_id,"request_id":row.request_id,
        "updated_at":row.updated_at.isoformat()+"Z","persistence":"application_database"}}


def read(db,scope,source_id):return describe(_read(db,scope,source_id))


def stage(db,scope,source_id,*,expected_revision,baseline_revision_id,request_id,filename,media_type,raw):
    row=_read(db,scope,source_id)
    baseline=store.read_revision(db,baseline_revision_id,scope=scope)
    if baseline["source_id"]!=source_id or baseline["kind"]!="upload":
        raise store.SourceError("待更新原件须属于此上传资料的固定版本")
    raw_hash=_hash(raw)
    # A lost response may be retried with exactly the same immutable payload.
    if row is not None and row.state=="pending"and row.request_id==request_id:
        if (row.kind,row.filename,row.media_type,row.raw_sha256,row.baseline_revision_id)!=("upload",filename,media_type,raw_hash,baseline_revision_id):
            raise HTTPException(409,"此暂存请求编号已绑定其他文件")
        return describe(row)
    if expected_revision!=(row.revision if row else 0):
        raise HTTPException(409,"另一窗口已改变待更新文件，请重新载入后核对")
    used=db.scalar(select(func.coalesce(func.sum(SourceUpdateDraft.raw_size),0)).where(
        SourceUpdateDraft.conversation_id==scope.scope_id,SourceUpdateDraft.source_id!=source_id,
        SourceUpdateDraft.state=="pending"))or 0
    if used+len(raw)>MAX_CONVERSATION_DRAFT_BYTES:
        raise HTTPException(413,"本会话待更新原件已超过300 MB，请先保存或取消已有更新")
    if row is None:
        row=SourceUpdateDraft(conversation_id=scope.scope_id,source_id=source_id,revision=0)
        db.add(row)
    row.revision+=1;row.state="pending";row.kind="upload";row.local_context=None;row.filename=filename;row.media_type=media_type
    row.raw_bytes=raw;row.raw_sha256=raw_hash;row.raw_size=len(raw)
    row.expected_version=baseline["version"];row.baseline_revision_id=baseline_revision_id;row.request_id=request_id
    row.updated_at=utcnow();row.fingerprint=_fingerprint(row);db.flush()
    return describe(row)


def stage_local(db,scope,source_id,*,expected_revision,baseline_revision_id,request_id,local):
    from . import workspace
    from .source_capture import _project
    row=_read(db,scope,source_id)
    baseline=store.read_revision(db,baseline_revision_id,scope=scope)
    _project(db,{"conversation_id":scope.scope_id,**local})
    path=workspace.relative_path(local["path"])
    context={**local,"path":path}
    if baseline["source_id"]!=source_id or baseline["kind"]!="local"or baseline["origin_ref"]!=f'project:{local["project_id"]}:{path}':
        raise store.SourceError("待更新位置不属于此项目资料")
    if row is not None and row.state=="pending"and row.request_id==request_id:
        if row.kind!="local"or row.local_context!=context or row.baseline_revision_id!=baseline_revision_id:
            raise HTTPException(409,"此暂存请求编号已绑定其他项目资料")
        return describe(row)
    if expected_revision!=(row.revision if row else 0):raise HTTPException(409,"另一窗口已改变待更新资料，请重新载入后核对")
    if row is None:
        row=SourceUpdateDraft(conversation_id=scope.scope_id,source_id=source_id,revision=0);db.add(row)
    row.revision+=1;row.state="pending";row.kind="local";row.local_context=context
    row.filename=baseline["title"];row.media_type=baseline["media_type"];row.raw_bytes=None;row.raw_size=0;row.raw_sha256=None
    row.expected_version=baseline["version"];row.baseline_revision_id=baseline_revision_id;row.request_id=request_id
    row.updated_at=utcnow();row.fingerprint=_fingerprint(row);db.flush()
    return describe(row)


def discard(db,scope,source_id,expected_revision):
    row=_read(db,scope,source_id)
    if expected_revision!=(row.revision if row else 0):raise HTTPException(409,"待更新文件已改变，请重新载入后核对")
    if row is None:return describe(row)
    _clear(row,"cancelled");db.flush()
    return describe(row)


def _clear(row,state):
    row.revision+=1;row.state=state
    for key in ("filename","media_type","raw_bytes","raw_sha256","expected_version","baseline_revision_id","request_id","local_context"):
        setattr(row,key,None)
    row.raw_size=0;row.updated_at=utcnow();row.fingerprint=_fingerprint(row)


def consume_if_matching(db,scope,source_id,request_id,raw_sha256):
    row=_read(db,scope,source_id)
    if row is None or row.state!="pending"or row.request_id!=request_id:return
    if row.kind=="upload"and row.raw_sha256!=raw_sha256:raise store.SourceCorruption("暂存原件与提交版本不一致")
    _clear(row,"consumed");db.flush()


def original(db,scope,source_id,expected_revision):
    row=_read(db,scope,source_id,body=True)
    if row is None or row.state!="pending"or row.kind!="upload":raise HTTPException(404,"没有待恢复的更新原件")
    if row.revision!=expected_revision:raise HTTPException(409,"待更新原件已改变，请重新载入后核对")
    return row
