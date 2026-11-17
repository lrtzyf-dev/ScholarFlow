"""Durable preview, per-entry retry and explicit bibliographic adoption."""
from __future__ import annotations
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import bibliography_store as store
from ..bibliography_parse import parse, BibliographyError
from ..bibliography_models import ImportBatch, PaperMetadataRevision
from .. import source_store as sources
from ..source_runtime import conversation_scope
from .sources import _source_errors

router=APIRouter(prefix='/api/bibliography',tags=['bibliography'])


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid')
    conversation_id:str=Field(pattern=r'^[\w-]{1,64}$')


class Preview(Scope):
    request_id:str=Field(pattern=r'^[A-Za-z0-9_-]{8,96}$')
    title:str=Field(min_length=1,max_length=200)
    format:Literal['doi','bibtex','ris']
    content:str=Field(min_length=1,max_length=4*1024*1024)


@router.post('/preview')
def preview(req:Preview,db:Session=Depends(get_db)):
    with _source_errors(db):
        try: entries=parse(req.format,req.content)
        except (BibliographyError,UnicodeError) as exc: raise sources.SourceError(str(exc)) from None
        db.execute(text('BEGIN IMMEDIATE'))
        value=store.create_batch(db,cid=req.conversation_id,request_id=req.request_id,title=req.title,
            format=req.format,raw=req.content,entries=entries)
        db.commit();return store.batch_output(db,value)


@router.get('/batches')
def batches(conversation_id:str,db:Session=Depends(get_db)):
    with _source_errors(db):
        conversation_scope(db,conversation_id,writing=False)
        return {'items':[{'id':b.id,'title':b.title,'format':b.format,'created_at':b.created_at.isoformat()} for b in
            db.scalars(select(ImportBatch).where(ImportBatch.conversation_id==conversation_id).order_by(ImportBatch.created_at.desc()).limit(100))]}


@router.get('/batches/{batch_id}')
def batch(batch_id:str,conversation_id:str,db:Session=Depends(get_db)):
    with _source_errors(db): return store.batch_output(db,store.batch(db,batch_id,conversation_id))


class Decision(Scope):
    expected_version:int=Field(ge=1)
    action:Literal['create','keep','update','skip']


@router.post('/entries/{entry_id}/decision')
def decision(entry_id:str,req:Decision,db:Session=Depends(get_db)):
    with _source_errors(db):
        db.execute(text('BEGIN IMMEDIATE'))
        entry=store.get_entry(db,entry_id,req.conversation_id)
        entry=store.decide_entry(db,entry,expected_version=req.expected_version,action=req.action)
        db.commit();return store.entry_output(db,entry)


class Refresh(Scope):
    expected_version:int=Field(ge=1)


@router.post('/entries/{entry_id}/refresh')
def refresh_entry(entry_id:str,req:Refresh,db:Session=Depends(get_db)):
    with _source_errors(db):
        db.execute(text('BEGIN IMMEDIATE'))
        entry=store.get_entry(db,entry_id,req.conversation_id)
        if entry.version!=req.expected_version or entry.decision: raise sources.SourceConflict('条目已处理或更新，请刷新')
        if entry.status=='identity_conflict': entry.error=''
        store.classify(db,entry);entry.version+=1
        db.commit();return store.entry_output(db,entry)


@router.post('/entries/{entry_id}/resolve')
def resolve_entry(entry_id:str,req:Refresh,db:Session=Depends(get_db)):
    from ..bibliography_network import resolve_doi
    with _source_errors(db):
        entry=store.get_entry(db,entry_id,req.conversation_id)
        if entry.version!=req.expected_version or entry.decision: raise sources.SourceConflict('条目已处理或更新，请刷新')
        doi=entry.metadata_value.get('doi')
        if not doi: raise sources.SourceError('此条目没有可解析的DOI；请校正后重新预览')
        db.rollback()
        value,error=None,''
        try: value=resolve_doi(doi)
        except BibliographyError as exc: error=str(exc)
        db.execute(text('BEGIN IMMEDIATE'))
        entry=store.get_entry(db,entry_id,req.conversation_id)
        if entry.version!=req.expected_version or entry.decision: raise sources.SourceConflict('解析期间条目已修改，未覆盖新内容')
        if value: entry.metadata_value=value
        entry.error=error;entry.version+=1;store.classify(db,entry)
        db.commit();return store.entry_output(db,entry)


@router.get('/papers/{paper_id}/metadata-history')
def metadata_history(paper_id:str,db:Session=Depends(get_db)):
    rows=list(db.scalars(select(PaperMetadataRevision).where(PaperMetadataRevision.paper_id==paper_id).order_by(PaperMetadataRevision.version.desc()).limit(100)))
    return {'items':[{'version':r.version,'before':r.before,'after':r.after,'actor':r.actor,'created_at':r.created_at.isoformat()} for r in rows]}
