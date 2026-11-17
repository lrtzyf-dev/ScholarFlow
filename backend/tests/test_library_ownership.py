"""Library originals and user annotations survive deletion of source chats."""
import threading
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.models import Conversation,Paper,Chunk
from app.source_models import Source,SourceRevision,SourceLibraryBinding,SourceLibraryImport
from app.research_models import ReadingAnnotation


@pytest.fixture
def client(db,monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner,'_shutting_down',threading.Event())
    db.add_all([Conversation(id='research',title='Origin'),Conversation(id='unrelated',title='Reader')]);db.commit()
    with TestClient(app) as client:yield client


@pytest.mark.parametrize('extension',['pdf','md','txt','docx','xlsx','csv'])
def test_six_formats_keep_original_location_annotation_and_reading_after_chat_delete(client,db,extension):
    from tests.test_research_persistence import seed
    pid,original=seed(client,1,extension)
    rid=original['revision_id'];path=f'/api/sources/revisions/{rid}'
    doc=client.get(path+'/document',params={'conversation_id':'research'}).json()
    if extension=='pdf':location={'kind':'pdf_page','page_index':0}
    elif extension=='docx':location={'kind':'word_paragraphs','paragraph_start':1,'paragraph_end':1}
    elif extension in ('csv','xlsx'):location={'kind':'sheet_cells','sheet':doc['sheets'][0]['name'],'cell_range':'A1'}
    else:location={'kind':'text_lines','line_start':1,'line_end':2}
    response=client.post(path+'/locators',json={'conversation_id':'research',**location});assert response.status_code==200,response.text
    locator=response.json()
    response=client.post(f'/api/research/reading/{rid}/annotations',json={'conversation_id':'research','locator_id':locator['locator_id'],
        'kind':'annotation','body':'Keep this reviewed note','selected_text':locator['quote']})
    assert response.status_code==200,response.text
    note=response.json()
    response=client.put(f'/api/research/reading/{rid}',json={'conversation_id':'research','expected_version':0,
        'position':{'locator_id':locator['locator_id'],'zoom':1.3},'user_read':True})
    assert response.status_code==200,response.text
    binding=db.get(SourceLibraryBinding,pid);canonical=binding.revision_id
    assert canonical!=rid and db.get(Source,db.get(SourceRevision,canonical).source_id).scope_type=='library'
    # A canonical ID alone never bypasses public conversation authorization.
    assert client.get(f'/api/sources/revisions/{canonical}',params={'conversation_id':'unrelated'}).status_code==404
    response=client.delete('/api/conversations/research');assert response.status_code==200,response.text
    db.expire_all();assert db.get(SourceRevision,rid) is None
    assert db.get(SourceRevision,canonical) is not None and db.get(Paper,pid) is not None
    assert db.get(ReadingAnnotation,note['id']) is not None
    response=client.post(f'/api/sources/library/{pid}/open',json={'conversation_id':'unrelated'})
    assert response.status_code==200,response.text
    reopened=response.json();assert reopened['raw_sha256']==original['raw_sha256']
    newrid=reopened['revision_id'];assert newrid not in (rid,canonical)
    notes=client.get(f'/api/research/reading/{newrid}/annotations',params={'conversation_id':'unrelated'}).json()['items']
    assert len(notes)==1 and notes[0]['id']==note['id'] and notes[0]['body']=='Keep this reviewed note'
    newloc=notes[0]['locator'];assert newloc['revision_id']==newrid and newloc['quote']==locator['quote']
    reading=client.get(f'/api/research/reading/{newrid}',params={'conversation_id':'unrelated'}).json()
    assert reading['user_read'] and reading['position']['zoom']==1.3
    assert client.get(f'/api/sources/revisions/{newrid}/locators/{newloc["locator_id"]}',params={'conversation_id':'unrelated'}).status_code==200
    update=client.patch(f'/api/research/annotations/{note["id"]}',json={'conversation_id':'unrelated','expected_version':note['version'],
        'body':'Edited after origin deletion','deleted':False})
    assert update.status_code==200,update.text
    if extension!='pdf':
        from app.source_library import augment_library_evidence
        chunk=db.scalar(select(Chunk).where(Chunk.paper_id==pid))
        evidence=augment_library_evidence(db,[{'paper_id':pid,'chunk_id':chunk.id,'quote':chunk.content}],'unrelated')[0]
        assert evidence.get('revision_id')==newrid and evidence.get('locator_id')


def test_notes_created_before_import_are_preserved_once_and_deleted_note_does_not_resurrect(client,db):
    response=client.post('/api/sources/upload',files={'file':('notes.md',b'# Kept\nSource quote')},data={'conversation_id':'research','request_id':'pre-import-notes'})
    rid=response.json()['revision_id']
    loc=client.post(f'/api/sources/revisions/{rid}/locators',json={'conversation_id':'research','kind':'text_lines','line_start':2,'line_end':2}).json()
    original=client.post(f'/api/research/reading/{rid}/annotations',json={'conversation_id':'research','locator_id':loc['locator_id'],'kind':'note',
        'body':'Before library adoption','selected_text':'Source quote'}).json()
    imported=client.post('/api/sources/library-import',json={'conversation_id':'research','revision_ids':[rid],'request_id':'pre-import-adoption'}).json()
    pid=imported['items'][0]['paper_id']
    notes=client.get(f'/api/research/reading/{rid}/annotations',params={'conversation_id':'research'}).json()['items']
    assert len(notes)==1 and notes[0]['id']!=original['id']
    assert client.patch('/api/research/annotations/'+notes[0]['id'],json={'conversation_id':'research','expected_version':1,
        'body':'Removed in library','deleted':True}).status_code==200
    for _ in range(2):
        assert client.get(f'/api/research/reading/{rid}/annotations',params={'conversation_id':'research'}).json()['items']==[]
    assert client.delete('/api/conversations/research').status_code==200
    opened=client.post(f'/api/sources/library/{pid}/open',json={'conversation_id':'unrelated'}).json()
    assert client.get(f'/api/research/reading/{opened["revision_id"]}/annotations',params={'conversation_id':'unrelated'}).json()['items']==[]


def test_legacy_receipt_migrates_atomically_before_deleting_original_conversation(client,db):
    response=client.post('/api/sources/upload',files={'file':('legacy.md',b'Legacy original quote')},
        data={'conversation_id':'research','request_id':'legacy-source-only'})
    record=response.json();rid=record['revision_id']
    locator=client.post(f'/api/sources/revisions/{rid}/locators',json={'conversation_id':'research','kind':'text_lines','line_start':1,'line_end':1}).json()
    note=client.post(f'/api/research/reading/{rid}/annotations',json={'conversation_id':'research','locator_id':locator['locator_id'],
        'kind':'note','body':'Legacy library note','selected_text':'Legacy original quote'}).json()
    paper=Paper(title='Legacy retained source',content_hash=record['raw_sha256'],status='parsed');db.add(paper);db.flush()
    db.add(SourceLibraryImport(revision_id=rid,raw_sha256=record['raw_sha256'],paper_id=paper.id,request_id='legacy-receipt',chunk_locators={}));db.commit()
    pid=paper.id;assert db.get(SourceLibraryBinding,pid) is None
    result=client.delete('/api/conversations/research');assert result.status_code==200,result.text
    db.expire_all();assert db.get(SourceLibraryBinding,pid) is not None
    opened=client.post(f'/api/sources/library/{pid}/open',json={'conversation_id':'unrelated'})
    assert opened.status_code==200,opened.text
    notes=client.get(f'/api/research/reading/{opened.json()["revision_id"]}/annotations',params={'conversation_id':'unrelated'}).json()['items']
    assert len(notes)==1 and notes[0]['body']=='Legacy library note' and notes[0]['id']!=note['id']


def test_same_original_different_extraction_versions_keep_separate_anchors_after_delete(client,db):
    from app import source_store as sources
    from app.source_models import SourceLibraryRevisionOrigin
    raw=b'One immutable original';scope=sources.SourceScope('conversation','research');records=[]
    for index,text in enumerate(['First extraction wording','Second extraction wording']):
        record=sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,f'parse-{index}','upload'),
            kind='upload',title=f'Parsed version {index}',raw_bytes=raw,text=text,media_type='text/plain',parser=f'fixture-parser-{index}')
        records.append(record);db.commit()
        locator=client.post(f'/api/sources/revisions/{record["revision_id"]}/locators',json={'conversation_id':'research','kind':'text_lines','line_start':1,'line_end':1}).json()
        response=client.post(f'/api/research/reading/{record["revision_id"]}/annotations',json={'conversation_id':'research',
            'locator_id':locator['locator_id'],'kind':'note','body':f'Notes for extraction {index}','selected_text':text})
        assert response.status_code==200,response.text
    paper=Paper(title='Versioned extraction',status='parsed',content_hash=records[0]['raw_sha256']);db.add(paper);db.flush()
    for index,record in enumerate(records):db.add(SourceLibraryImport(revision_id=record['revision_id'],raw_sha256=record['raw_sha256'],
        paper_id=paper.id,request_id=f'old-version-receipt-{index}',chunk_locators={}))
    db.commit();pid=paper.id
    response=client.delete('/api/conversations/research');assert response.status_code==200,response.text
    versions=client.get(f'/api/sources/library/{pid}/versions',params={'conversation_id':'unrelated'})
    assert versions.status_code==200,versions.text
    assert len(versions.json()['items'])==2
    for index,original in enumerate(records):
        origin=db.get(SourceLibraryRevisionOrigin,original['revision_id'])
        response=client.post(f'/api/sources/library/{pid}/open',json={'conversation_id':'unrelated','library_revision_id':origin.library_revision_id})
        assert response.status_code==200,response.text
        rid=response.json()['revision_id']
        notes=client.get(f'/api/research/reading/{rid}/annotations',params={'conversation_id':'unrelated'}).json()['items']
        assert len(notes)==1 and notes[0]['body']==f'Notes for extraction {index}'
        assert notes[0]['locator']['quote']==['First extraction wording','Second extraction wording'][index]
