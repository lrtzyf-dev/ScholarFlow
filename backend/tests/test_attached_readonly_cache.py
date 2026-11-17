"""Actual immutable sources; no provider calls or user data."""
import json

import pytest
from sqlalchemy.orm import Session

from app import source_store as sources, source_runtime, source_documents
from app.agent import tools, readonly_cache
from app.models import Conversation
from app.runtime_context import bind_runtime_config
from app.source_parsing import extract, MEDIA
from app.timeutil import utcnow


@pytest.fixture
def attachment(db):
    db.add(Conversation(id='attached-cache',title='Cache'));db.commit()
    scope=sources.SourceScope('conversation','attached-cache')
    raw=b'alpha\nbeta\ngamma\n';parsed=extract(raw,MEDIA['md'])
    record=sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,'upload','upload'),
        kind='upload',title='notes.md',raw_bytes=raw,text=parsed.text,media_type=MEDIA['md'],text_origin='parsed_text',parser=parsed.parser)
    source_documents.save_extraction(db,record['revision_id'],scope=scope,extraction=parsed);db.commit()
    binding=source_runtime.freeze_sources(db,'attached-cache',[record['revision_id']],
        {'conversation_id':'attached-cache','run_id':'attached-run'})
    db.commit()
    return record,binding


def read(db,attachment,cache,*,args=None,action=None):
    record,binding=attachment
    arguments=args or {'source_revision_id':record['revision_id'],'line_start':1,'line_end':1}
    with bind_runtime_config(binding):
        return readonly_cache.attached_read(db,arguments,binding=binding,conversation_id='attached-cache',cache=cache,
            action=action or (lambda:tools.execute_tool(db,'read_attached_source',arguments,conversation_id='attached-cache')))


def test_actual_attachment_reuses_after_serialization_and_rejects_body_or_identity_damage(db,attachment):
    cache={};original=read(db,attachment,cache);db.commit()
    assert json.loads(original)['evidence'] and len(cache)==1
    restored=json.loads(json.dumps(cache))
    assert read(db,attachment,restored,action=lambda:pytest.fail('unchanged source reread'))==original
    item=next(iter(restored.values()));item['result']='{"evidence":[{"quote":"tampered"}]}'
    assert read(db,attachment,restored)==original
    next(iter(restored.values()))['identity']['source']['raw_sha256']='wrong'
    assert read(db,attachment,restored)==original


@pytest.mark.parametrize('hit',[False,True])
def test_attachment_authority_revoked_during_miss_or_hit_never_releases_body(db,attachment,monkeypatch,hit):
    cache={};original=read(db,attachment,cache);db.commit()
    if not hit:cache={}
    real=source_runtime.require_attached_revision;checks=[]
    def revoke():
        with Session(db.get_bind()) as writer:
            writer.get(Conversation,'attached-cache').archived_at=utcnow();writer.commit()
    if hit:
        def validate(*a,**k):
            checks.append(1)
            if len(checks)==2:revoke()
            return real(*a,**k)
        monkeypatch.setattr(source_runtime,'require_attached_revision',validate)
        action=lambda:pytest.fail('valid hit need not execute')
    else:
        action=lambda:revoke() or original
    with pytest.raises(Exception):read(db,attachment,cache,action=action)
    assert not cache


def test_attachment_disabled_policy_never_dispatches_or_consumes_cached_result(db,attachment,monkeypatch):
    cache={};read(db,attachment,cache);db.commit()
    monkeypatch.setattr(tools,'tool_policy_error',lambda n:'disabled')
    with pytest.raises(PermissionError):read(db,attachment,cache,action=lambda:pytest.fail('policy bypass'))


def test_attachment_budget_and_lru_cover_result_plus_identity(db,attachment,monkeypatch):
    record,_=attachment;cache={}
    monkeypatch.setattr(readonly_cache,'MAX_ENTRIES',2)
    def args(line):return {'source_revision_id':record['revision_id'],'line_start':line,'line_end':line}
    read(db,attachment,cache,args=args(1));read(db,attachment,cache,args=args(2));db.commit()
    read(db,attachment,cache,args=args(1));read(db,attachment,cache,args=args(3))
    assert len(cache)==2 and {v['identity']['arguments']['line_start'] for v in cache.values()}=={1,3}
    monkeypatch.setattr(readonly_cache,'MAX_BYTES',100)
    read(db,attachment,cache,args=args(1));read(db,attachment,cache,args=args(3))
    assert not cache


def test_attachment_original_damage_rechecked_on_cache_hit(db,attachment,monkeypatch):
    cache={};read(db,attachment,cache);db.commit()
    record,_=attachment
    # Corrupt only this isolated test's captured DB bytes, bypassing the
    # immutable ORM guard to exercise detection of an actually damaged store.
    from app.source_models import SourceSnapshot
    from sqlalchemy import update
    db.execute(update(SourceSnapshot).where(SourceSnapshot.id==record['snapshot_id']).values(raw_bytes=b'wrong'));db.commit()
    with pytest.raises(Exception):read(db,attachment,cache,action=lambda:pytest.fail('damaged original consumed'))


def test_shared_budget_evicts_oversized_legacy_entries_even_if_new_entry_cannot_fit(monkeypatch):
    monkeypatch.setattr(readonly_cache,'MAX_BYTES',100)
    cache={'legacy':{'result':'x'*1000}}
    readonly_cache.store_entry(cache,'new',{'result':'y'*1000})
    assert cache=={}
