"""Preview/confirm bibliographic metadata without pretending to possess full text."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import re
from sqlalchemy import select, func
from . import source_store as sources
from .source_runtime import conversation_scope
from .models import Paper
from .bibliography_models import ImportBatch, ImportEntry, PaperIdentifier, PaperMetadataRevision
from .bibliography_parse import metadata, normalize_doi
from .timeutil import utcnow


def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def paper_fields(p):
    return {"title":p.title,"authors":p.authors,"abstract":p.abstract,"published_at":p.published_at,
        "content_hash":p.content_hash,"status":p.status}


def identities(value):
    result=[]
    if value.get('doi'): result.append(('doi',value['doi']))
    if value.get('external_id'): result.append(('zotero',value['external_id']))
    # An exact normalized bibliographic fingerprint is a duplicate candidate,
    # never a fuzzy-title claim that two scientific works must be the same.
    canonical=[re.sub(r'\s+',' ',str(value.get(k) or '')).strip().casefold() for k in ('title','authors','published_at')]
    if canonical[0]: result.append(('metadata_fingerprint',digest(canonical)))
    return result


def candidate(db,value):
    found=[]
    for kind,key in identities(value):
        identifier=db.scalar(select(PaperIdentifier).where(PaperIdentifier.kind==kind,PaperIdentifier.value==key))
        if identifier:
            paper=db.get(Paper,identifier.paper_id)
            if paper and paper.id not in [p.id for p in found]: found.append(paper)
    if not found and value.get('title'):
        target=dict(identities(value)).get('metadata_fingerprint')
        # Legacy papers have no identifier receipts yet. Exact bibliographic
        # equality is only a candidate; the user still confirms adoption.
        for paper in db.scalars(select(Paper).where(func.lower(Paper.title)==value['title'].lower())):
            if dict(identities(paper_fields(paper))).get('metadata_fingerprint')==target:
                found.append(paper)
    if len(found)>1: raise sources.SourceConflict("标识符对应多个已有文献，需先核对身份，未自动合并")
    return found[0] if found else None


def classify(db,entry):
    value=entry.metadata_value
    if entry.error:
        entry.status='invalid';return
    if not value.get('title'):
        entry.status='needs_resolution';return
    try: existing=candidate(db,value)
    except sources.SourceConflict as exc:
        entry.status='identity_conflict';entry.error=str(exc);return
    entry.candidate_paper_id=existing.id if existing else None
    entry.candidate_fingerprint=digest(paper_fields(existing)) if existing else None
    if existing:
        entry.status='conflict' if any(value.get(k) and value.get(k)!=getattr(existing,k) for k in ('title','authors','abstract','published_at')) else 'duplicate'
    else: entry.status='pending'


def create_batch(db,*,cid,request_id,title,format,raw,entries,source_revision_id=None):
    scope=conversation_scope(db,cid)
    payload=digest([title,format,raw])
    previous=db.scalar(select(ImportBatch).where(ImportBatch.conversation_id==cid,ImportBatch.request_id==request_id))
    if previous:
        if previous.request_sha256!=payload: raise sources.SourceConflict("此导入请求编号已绑定不同文件")
        return previous
    if source_revision_id is None:
        record=sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,'bibliography:'+request_id,'upload'),
            kind='upload',title=title,raw_bytes=raw.encode('utf-8'),text=raw,media_type='text/plain',parser='bibliography-v1')
        source_revision_id=record['revision_id']
    batch=ImportBatch(conversation_id=cid,request_id=request_id,request_sha256=payload,title=title,format=format,source_revision_id=source_revision_id)
    db.add(batch);db.flush()
    for index,item in enumerate(entries):
        entry=ImportEntry(batch_id=batch.id,position=index,raw_entry=item.get('raw',''),
            metadata_value=item.get('metadata') or ({'doi':item.get('doi')} if item.get('doi') else {}),error=item.get('error',''))
        classify(db,entry);db.add(entry)
    db.flush();return batch


def batch(db,batch_id,cid,writing=False):
    conversation_scope(db,cid,writing=writing)
    value=db.get(ImportBatch,batch_id)
    if not value or value.conversation_id!=cid: raise sources.SourceNotFound("当前会话没有此导入批次")
    return value


def entry_output(db,entry):
    p=db.get(Paper,entry.candidate_paper_id) if entry.candidate_paper_id else None
    return {"id":entry.id,"position":entry.position,"version":entry.version,"raw_entry":entry.raw_entry,
        "metadata":entry.metadata_value,"status":entry.status,"error":entry.error,"candidate_paper_id":entry.candidate_paper_id,
        "existing_metadata":paper_fields(p) if p else None,"paper_id":entry.paper_id,"decision":entry.decision,
        "fulltext_readable":bool(p and p.status=='parsed')}


def batch_output(db,value):
    entries=list(db.scalars(select(ImportEntry).where(ImportEntry.batch_id==value.id).order_by(ImportEntry.position)))
    return {"id":value.id,"title":value.title,"format":value.format,"conversation_id":value.conversation_id,
        "source_revision_id":value.source_revision_id,"items":[entry_output(db,e) for e in entries]}


def get_entry(db,entry_id,cid):
    value=db.get(ImportEntry,entry_id,populate_existing=True)
    if not value: raise sources.SourceNotFound("导入条目不存在")
    batch(db,value.batch_id,cid,writing=True)
    return value


def decide_entry(db,entry,*,expected_version,action):
    if entry.decision:
        if entry.decision==action and entry.version==expected_version+1: return entry
        raise sources.SourceConflict("条目已经处理，请刷新查看结果")
    if entry.version!=expected_version: raise sources.SourceConflict("条目预览已变化，请刷新后确认")
    if action=='skip':
        entry.status,entry.decision,entry.version,entry.decided_at='skipped',action,entry.version+1,utcnow();return entry
    if entry.status in {'invalid','needs_resolution','identity_conflict'}: raise sources.SourceError("请先解析或校正此条目")
    existing=candidate(db,entry.metadata_value)
    if (existing.id if existing else None)!=entry.candidate_paper_id:
        raise sources.SourceConflict("预览后已有新导入记录，请重新核对重复项")
    if existing and digest(paper_fields(existing))!=entry.candidate_fingerprint:
        raise sources.SourceConflict("已有文献元数据已变化，请重新预览，未覆盖新修改")
    if action=='create' and existing: raise sources.SourceConflict("已有相同标识符，请选择保留已有或更新元数据")
    if action in {'keep','update'} and not existing: raise sources.SourceConflict("已有记录已不存在，请重新预览")
    value=entry.metadata_value
    if action=='create':
        existing=Paper(title=value['title'],authors=value['authors'],abstract=value.get('abstract',''),
            published_at=value.get('published_at'),status='metadata_only',error='尚未取得全文，不能阅读或引用正文')
        db.add(existing);db.flush()
        before={}
    else: before=paper_fields(existing)
    if action=='update':
        for field in ('title','authors','abstract','published_at'):
            if value.get(field): setattr(existing,field,value[field])
    for kind,key in identities(value):
        ident=db.scalar(select(PaperIdentifier).where(PaperIdentifier.kind==kind,PaperIdentifier.value==key))
        if ident is None: db.add(PaperIdentifier(paper_id=existing.id,kind=kind,value=key))
        elif ident.paper_id!=existing.id: raise sources.SourceConflict("文献标识符冲突，未修改已有记录")
    # Even an explicit 'keep' is an audit receipt, with unchanged before/after.
    version=(db.scalar(select(func.max(PaperMetadataRevision.version)).where(PaperMetadataRevision.paper_id==existing.id)) or 0)+1
    raw_source=preserve_raw_entry(db,existing,entry)
    db.add(PaperMetadataRevision(paper_id=existing.id,version=version,entry_id=entry.id,before=before,
        after={**paper_fields(existing),"bibliography":deepcopy(value),"decision":action,
            "raw_entry":entry.raw_entry,"raw_source_revision_id":raw_source['revision_id']}))
    entry.paper_id,entry.status,entry.decision,entry.version,entry.decided_at=existing.id,'imported',action,entry.version+1,utcnow()
    db.flush();return entry


def preserve_raw_entry(db,paper,entry):
    scope=sources.SourceScope('library',paper.id)
    raw=entry.raw_entry or json.dumps(entry.metadata_value,ensure_ascii=False)
    return sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,'bibliography:'+entry.id,'library'),
        kind='library',title=('书目原始条目 · '+paper.title)[:200],raw_bytes=raw.encode(),text=raw,media_type='text/plain',
        origin_ref='bibliography:'+entry.id,parser='bibliography-v1')


def preserve_conversation_metadata(db,cid):
    # One additive audit revision for pre-existing imports that did not retain
    # an independently owned raw entry. Never rewrite old audit history.
    for entry in db.scalars(select(ImportEntry).join(ImportBatch,ImportBatch.id==ImportEntry.batch_id).where(
            ImportBatch.conversation_id==cid,ImportEntry.paper_id.is_not(None))):
        paper=db.get(Paper,entry.paper_id)
        if not paper:continue
        revisions=list(db.scalars(select(PaperMetadataRevision).where(PaperMetadataRevision.entry_id==entry.id)))
        if any(r.after.get('raw_source_revision_id') for r in revisions):continue
        source=preserve_raw_entry(db,paper,entry)
        version=(db.scalar(select(func.max(PaperMetadataRevision.version)).where(PaperMetadataRevision.paper_id==paper.id)) or 0)+1
        before=paper_fields(paper)
        db.add(PaperMetadataRevision(paper_id=paper.id,version=version,entry_id=entry.id,before=before,
            after={**before,'raw_entry':entry.raw_entry,'raw_source_revision_id':source['revision_id'],
                'bibliography':deepcopy(entry.metadata_value),'decision':'preserve_import_provenance'},actor='system_migration'))
        db.flush()
