"""Isolated SF-038/039 contracts; all remote responses are synthetic."""
from copy import deepcopy
import threading
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.models import Conversation,Paper
from app.bibliography_parse import parse,normalize_doi
from app.bibliography_models import ImportBatch,ImportEntry,PaperIdentifier,PaperMetadataRevision,ConnectorBinding,ConnectorItem,ConnectorItemRevision
from app import bibliography_network as net


@pytest.fixture
def client(db,monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner,'_shutting_down',threading.Event())
    db.add_all([Conversation(id='bib',title='Bibliography'),Conversation(id='other',title='Other')]);db.commit()
    with TestClient(app) as client: yield client


def preview(client,content,format='bibtex',request='import-request-1'):
    result=client.post('/api/bibliography/preview',json={'conversation_id':'bib','request_id':request,'title':'References',
        'format':format,'content':content})
    assert result.status_code==200,result.text
    return result.json()


def decision(client,row,action):
    return client.post(f'/api/bibliography/entries/{row["id"]}/decision',json={'conversation_id':'bib',
        'expected_version':row['version'],'action':action})


def test_parser_balanced_inline_nested_macros_and_invalid_entries():
    content='@string{x = "Real"}\n@article{a,title=x # { {Nested} @article{inside} Title},author={A and B},year=2025,doi={HTTPS://doi.org/10.1234/AbC}}@book{b,title="Two",year=2024}\n@article{bad,title={Oops}\n@article{good,title={Good}}'
    rows=parse('bibtex',content)
    assert len(rows)==4
    assert rows[0]['metadata']['title']=='Real {Nested} @article{inside} Title'
    assert rows[0]['metadata']['doi']=='10.1234/abc'
    assert rows[1]['metadata']['title']=='Two'
    assert 'error' in rows[2]
    assert rows[3]['metadata']['title']=='Good'
    assert normalize_doi('doi: 10.1234/X')=='10.1234/x'
    assert normalize_doi('https://evil.invalid/10.1234/x') is None
    comments=parse('bibtex','% @article{ignored,title={Not a paper}}\n@article{real, % ignored }\n title={100% data}, % comment\n year=2024}')
    assert len(comments)==1 and comments[0]['metadata']['title']=='100% data'


def test_ris_partial_error_continuation_and_raw_retained():
    text='TY  - JOUR\nTI  - Real\n  continuation\nAU  - One\nAU  - Two\nPY  - 2024/04\nER  -\nTY  - JOUR\nTI  - Missing ending'
    rows=parse('ris',text)
    assert len(rows)==2 and rows[0]['metadata']['title']=='Real continuation'
    assert rows[0]['metadata']['authors']=='One; Two'
    assert 'ER' in rows[0]['raw'] and 'error' in rows[1]


def test_preview_idempotency_metadata_only_conflict_and_scope(client,db):
    content='@article{one,title={Real research},author={A and B},year=2025,doi={10.1234/one}}\n@article{bad,author={No title}}'
    batch=preview(client,content)
    assert [r['status'] for r in batch['items']]==['pending','invalid']
    assert preview(client,content)['id']==batch['id']
    row=batch['items'][0];result=decision(client,row,'create')
    assert result.status_code==200,result.text
    pid=result.json()['paper_id'];p=db.get(Paper,pid)
    assert p.status=='metadata_only' and not p.pdf_path and not p.content_hash
    assert decision(client,row,'create').json()['paper_id']==pid
    assert decision(client,batch['items'][1],'create').status_code==422
    assert client.get('/api/bibliography/batches/'+batch['id'],params={'conversation_id':'other'}).status_code==404
    altered=content.replace('Real research','Updated title')
    next_batch=preview(client,altered,request='import-request-2');row=next_batch['items'][0]
    assert row['status']=='conflict' and row['candidate_paper_id']==pid
    updated=decision(client,row,'update');assert updated.status_code==200,updated.text
    db.expire_all();assert db.get(Paper,pid).title=='Updated title'
    assert len(list(db.scalars(select(Paper))))==1
    assert len(list(db.scalars(select(PaperMetadataRevision))))==2
    assert client.post('/api/bibliography/preview',json={'conversation_id':'bib','request_id':'import-request-1','title':'References',
        'format':'bibtex','content':altered}).status_code==409


def test_duplicate_in_batch_refresh_and_legacy_candidate(client,db):
    p=Paper(title='Exact old work',authors='A',published_at='2025',status='parsed',content_hash='existing-hash')
    db.add(p);db.commit()
    b=preview(client,'@article{one,title={Exact old work},author={A},year=2025}')
    assert b['items'][0]['candidate_paper_id']==p.id
    assert decision(client,b['items'][0],'keep').status_code==200
    b=preview(client,'@article{a,title={New},doi={10.1234/n}}@article{b,title={New},doi={10.1234/n}}',request='duplicate-request')
    assert decision(client,b['items'][0],'create').status_code==200
    assert decision(client,b['items'][1],'create').status_code==409
    result=client.post('/api/bibliography/entries/'+b['items'][1]['id']+'/refresh',json={'conversation_id':'bib','expected_version':1})
    assert result.json()['status']=='duplicate'
    assert decision(client,result.json(),'keep').status_code==200


def test_doi_resolution_failure_retry_and_exact_identity(client,monkeypatch):
    row=preview(client,'HTTPS://doi.org/10.1234/ONE','doi')['items'][0]
    assert row['status']=='needs_resolution'
    def response(url,**kwargs):
        assert url=='https://api.crossref.org/works/10.1234%2Fone'
        return {'message':{'DOI':'10.1234/wrong','title':['Wrong']}},{}
    monkeypatch.setattr(net,'get_json',response)
    path='/api/bibliography/entries/'+row['id']+'/resolve'
    result=client.post(path,json={'conversation_id':'bib','expected_version':row['version']})
    assert result.status_code==200,result.text
    assert result.json()['status']=='invalid'
    monkeypatch.setattr(net,'get_json',lambda *a,**k:({'message':{'DOI':'10.1234/one','title':['Right'],'published':{'date-parts':[[2024]]}}},{}))
    result=client.post(path,json={'conversation_id':'bib','expected_version':result.json()['version']})
    assert result.json()['metadata']['title']=='Right' and result.json()['status']=='pending'


def zotero_fixture(monkeypatch):
    state={'version':10,'members':{'ABCD1234':4},'items':{'ABCD1234':{'key':'ABCD1234','version':4,'data':{
        'title':'Zotero research','itemType':'journalArticle','DOI':'10.1234/z','date':'2024','creators':[{'creatorType':'author','name':'Author'}]}},
        'PDF01234':{'key':'PDF01234','version':5,'data':{'itemType':'attachment','parentItem':'ABCD1234','title':'Original PDF',
            'contentType':'application/pdf','linkMode':'imported_file','md5':'f'*32}}},'fail':False,'calls':[]}
    def request(path,credential,params=None):
        assert credential=='synthetic-zotero-test-key';state['calls'].append((path,params))
        headers={'last-modified-version':str(state['version'])}
        if path=='/keys/current':return {'userID':123,'access':{'user':{'library':True,'files':True}}},headers
        if path=='/users/123/collections/COLL1234':return {'key':'COLL1234','data':{'name':'Selected collection'}},headers
        if path=='/users/123/collections':return [{'key':'COLL1234','data':{'name':'Selected collection'}}],headers
        if state['fail']:raise net.ConnectorError('Synthetic offline')
        if path.endswith('/collections/COLL1234/items'):return deepcopy(state['members']),headers
        if path=='/users/123/items':return [deepcopy(state['items'][k]) for k in params['itemKey'].split(',')],headers
        if path.endswith('/ABCD1234/children'):return [deepcopy(state['items']['PDF01234'])] if 'PDF01234' in state['items'] else [],headers
        raise AssertionError(path)
    monkeypatch.setattr(net,'zotero_request',request)
    return state


def connect(client):
    result=client.post('/api/zotero/bindings',json={'conversation_id':'bib','user_id':'123','collection_key':'COLL1234','api_key':'synthetic-zotero-test-key'})
    assert result.status_code==200,result.text
    assert 'synthetic-zotero-test-key' not in result.text and 'protected_credential' not in result.text
    return result.json()


def sync(client,b):
    return client.post(f'/api/zotero/bindings/{b["id"]}/sync',json={'conversation_id':'bib','expected_version':b['version']})


def test_zotero_scope_snapshot_adoption_idempotency_remote_removal_and_revoke(client,db,monkeypatch):
    state=zotero_fixture(monkeypatch);b=connect(client)
    stored=db.get(ConnectorBinding,b['id']);assert stored.protected_credential.startswith('dpapi:')
    r=sync(client,b);assert r.status_code==200,r.text
    b=r.json()['binding'];batch_id=r.json()['batch_id'];assert b['cursor']==10
    batch=client.get('/api/bibliography/batches/'+batch_id,params={'conversation_id':'bib'}).json()
    assert len(batch['items'])==1
    pid=decision(client,batch['items'][0],'create').json()['paper_id']
    r=sync(client,b);assert r.status_code==200,r.text
    assert r.json()['changed_items']==0 and r.json()['batch_id'] is None
    b=r.json()['binding'];assert len(list(db.scalars(select(ConnectorItemRevision))))==2
    state['members']={};state['version']=11
    r=sync(client,b);assert r.status_code==200,r.text
    b=r.json()['binding'];db.expire_all();assert db.get(Paper,pid) is not None
    assert {x.state for x in db.scalars(select(ConnectorItem))}=={'not_in_selected_collection'}
    assert client.get(f'/api/zotero/bindings/{b["id"]}/items',params={'conversation_id':'other'}).status_code==404
    r=client.post(f'/api/zotero/bindings/{b["id"]}/state',json={'conversation_id':'bib','expected_version':b['version'],'action':'revoke'})
    assert r.status_code==200,r.text
    db.expire_all();assert db.get(ConnectorBinding,b['id']).protected_credential is None
    assert sync(client,r.json()).status_code==409
    assert all('delete' not in path.lower() for path,_ in state['calls'])


def test_zotero_failure_keeps_cursor_and_retries(client,db,monkeypatch):
    state=zotero_fixture(monkeypatch);b=connect(client);state['fail']=True
    assert sync(client,b).status_code==422
    b=client.get('/api/zotero/bindings',params={'conversation_id':'bib'}).json()['items'][0]
    assert b['cursor']==0
    assert not list(db.scalars(select(ImportBatch))) and not list(db.scalars(select(ConnectorItem)))
    state['fail']=False;r=sync(client,b);assert r.status_code==200,r.text


def test_zotero_pause_during_io_discards_snapshot(client,db,monkeypatch):
    from app.db import SessionLocal
    from app import zotero_store
    zotero_fixture(monkeypatch);b=connect(client);original=zotero_store.snapshot
    def pause(*args,**kwargs):
        result=original(*args,**kwargs)
        with SessionLocal() as separate:
            current=separate.get(ConnectorBinding,b['id']);current.status='paused';current.version+=1;separate.commit()
        return result
    monkeypatch.setattr(zotero_store,'snapshot',pause)
    assert sync(client,b).status_code==409
    db.expire_all();assert db.get(ConnectorBinding,b['id']).cursor==0
    assert not list(db.scalars(select(ImportBatch)))


def test_zotero_changed_library_version_does_not_commit(client,db,monkeypatch):
    state=zotero_fixture(monkeypatch);b=connect(client);original=net.zotero_request
    def mutate(path,key,params=None):
        result=original(path,key,params)
        if path.endswith('/children'):state['version']+=1
        return result
    monkeypatch.setattr(net,'zotero_request',mutate)
    assert sync(client,b).status_code==422
    db.expire_all();assert db.get(ConnectorBinding,b['id']).cursor==0
    assert not list(db.scalars(select(ConnectorItem)))


def test_parallel_import_decisions_do_not_duplicate_identity(client,db):
    from concurrent.futures import ThreadPoolExecutor
    first=preview(client,'@article{a,title={One identity},doi={10.1234/shared}}',request='parallel-request-1')['items'][0]
    second=preview(client,'@article{b,title={One identity},doi={10.1234/shared}}',request='parallel-request-2')['items'][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda row:decision(client,row,'create'),[first,second]))
    assert sorted(r.status_code for r in results)==[200,409]
    assert len(list(db.scalars(select(Paper))))==1


def test_zotero_invalid_credential_blocks_until_reconnect(client,db,monkeypatch):
    state=zotero_fixture(monkeypatch);b=connect(client)
    def revoked(*a,**k):raise net.ConnectorError('Authorization revoked',invalid_credential=True)
    monkeypatch.setattr(net,'zotero_request',revoked)
    assert sync(client,b).status_code==422
    b=client.get('/api/zotero/bindings',params={'conversation_id':'bib'}).json()['items'][0]
    assert b['status']=='invalid' and b['cursor']==0
    assert client.post(f'/api/zotero/bindings/{b["id"]}/state',json={'conversation_id':'bib','expected_version':b['version'],'action':'resume'}).status_code==409
    zotero_fixture(monkeypatch);reconnected=connect(client)
    assert reconnected['id']==b['id'] and reconnected['status']=='active'


def test_zotero_children_pagination_and_unchanged_metadata_receipt(client,db,monkeypatch):
    from app import zotero_store
    state=zotero_fixture(monkeypatch);original=net.zotero_request;starts=[]
    def paged(path,key,params=None):
        if path.endswith('/children'):
            start=params['start'];starts.append(start)
            count=100 if start==0 else 1
            return [{'key':f'F{i:07d}','version':6,'data':{'itemType':'attachment','parentItem':'ABCD1234'}}
                for i in range(start,start+count)],{'last-modified-version':'10'}
        return original(path,key,params)
    monkeypatch.setattr(net,'zotero_request',paged)
    version,rows=zotero_store.snapshot('123','COLL1234','synthetic-zotero-test-key')
    assert version==10 and len(rows)==102 and starts==[0,100]
    b=connect(client);r=sync(client,b);assert r.status_code==200,r.text
    b=r.json()['binding'];state['items']['ABCD1234']['meta']={'numChildren':101}
    r=sync(client,b);assert r.status_code==200,r.text
    assert r.json()['changed_items']==0


def test_pdf_parse_preserves_explicit_bibliography(client,db,sample_pdf):
    from app.pipeline.indexer import ingest_pdf
    row=preview(client,'@article{one,title={Human-approved title},author={Chosen Author},abstract={Reviewed abstract},year=2025}')['items'][0]
    pid=decision(client,row,'create').json()['paper_id'];paper=db.get(Paper,pid)
    paper.pdf_path=str(sample_pdf);db.commit()
    ingest_pdf(db,paper);db.refresh(paper)
    assert paper.title=='Human-approved title' and paper.authors=='Chosen Author' and paper.abstract=='Reviewed abstract'


def test_zotero_malformed_authorization_is_rejected(monkeypatch):
    monkeypatch.setattr(net,'zotero_request',lambda *a,**k:([],{}))
    with pytest.raises(net.ConnectorError): net.verify_zotero('123','synthetic')


def test_zotero_attachment_fixed_version_then_same_paper_fulltext(client,db,monkeypatch,sample_pdf):
    from app import zotero_files,tasks
    from app.bibliography_models import ConnectorAttachmentVersion
    from app.source_models import SourceLibraryImport
    state=zotero_fixture(monkeypatch);b=connect(client)
    r=sync(client,b).json();b=r['binding']
    batch=client.get('/api/bibliography/batches/'+r['batch_id'],params={'conversation_id':'bib'}).json()
    pid=decision(client,batch['items'][0],'create').json()['paper_id']
    attachment=db.scalar(select(ConnectorItem).where(ConnectorItem.remote_key=='PDF01234'))
    monkeypatch.setattr(zotero_files,'download_pdf',lambda *a:sample_pdf.read_bytes())
    calls=[];monkeypatch.setattr(tasks,'schedule_parse',lambda pid:calls.append(pid))
    url=f'/api/zotero/bindings/{b["id"]}/items/{attachment.id}'
    payload={'conversation_id':'bib','expected_version':b['version'],'remote_version':attachment.remote_version}
    result=client.post(url+'/download',json=payload);assert result.status_code==200,result.text
    rid=result.json()['revision_id']
    assert client.post(url+'/download',json=payload).json()['revision_id']==rid
    result=client.post(url+'/attach-fulltext',json=payload);assert result.status_code==200,result.text
    assert result.json()['paper_id']==pid and calls==[pid]
    assert client.post(url+'/attach-fulltext',json=payload).status_code==200 and calls==[pid]
    db.expire_all();assert db.get(Paper,pid).status=='uploaded' and len(list(db.scalars(select(Paper))))==1
    assert db.get(SourceLibraryImport,rid).paper_id==pid
    state['items']['PDF01234']['version']=11;state['version']=11
    r=sync(client,b);assert r.status_code==200,r.text
    db.expire_all();assert db.get(ConnectorItem,attachment.id).source_revision_id is None
    assert len(list(db.scalars(select(ConnectorAttachmentVersion))))==1
    assert client.get(f'/api/sources/revisions/{rid}',params={'conversation_id':'bib'}).status_code==200
    result=client.delete('/api/conversations/bib');assert result.status_code==200,result.text
    reopened=client.post(f'/api/sources/library/{pid}/open',json={'conversation_id':'other'})
    assert reopened.status_code==200,reopened.text
    history=client.get(f'/api/bibliography/papers/{pid}/metadata-history').json()['items']
    assert history[0]['after']['raw_entry'] and history[0]['after']['raw_source_revision_id']
    db.expire_all();assert db.get(Paper,pid) is not None


def test_attachment_redirect_never_forwards_key_and_checks_content(monkeypatch,sample_pdf):
    import hashlib,httpx
    from app import zotero_files,netguard
    raw=sample_pdf.read_bytes();calls=[]
    def handler(request):
        calls.append(request)
        if request.url.host=='api.zotero.org':
            assert request.headers['Zotero-API-Key']=='synthetic-test-only'
            return httpx.Response(302,headers={'location':'https://files.example.invalid/signed?temporary=redacted'})
        assert 'Zotero-API-Key' not in request.headers
        return httpx.Response(200,content=raw)
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    monkeypatch.setattr(netguard,'request_guard_hook',lambda request:None)
    monkeypatch.setattr(netguard,'assert_public_url',lambda url:None)
    assert zotero_files.download_pdf('123','PDF01234','synthetic-test-only',hashlib.md5(raw).hexdigest())==raw
    with pytest.raises(net.ConnectorError,match='版本不匹配'):
        zotero_files.download_pdf('123','PDF01234','synthetic-test-only','0'*32)
    assert len(calls)==4


def test_zotero_backup_excludes_credentials_and_requires_configuration(client,db,monkeypatch,tmp_path):
    import sqlite3
    from app import backup_credentials
    zotero_fixture(monkeypatch);b=connect(client)
    path=tmp_path/'snapshot.db'
    source=db.get_bind().raw_connection()
    with sqlite3.connect(path) as target: source.driver_connection.backup(target)
    source.close()
    result=backup_credentials.sanitized_database(path)
    assert result['removed_records']['zotero_credentials']==1
    with sqlite3.connect(path) as check:
        assert check.execute('SELECT protected_credential,status,version FROM zotero_bindings').fetchone()==(None,'needs_configuration',b['version']+1)
    backup_credentials.validate_excluded_backup(tmp_path,'snapshot.db',['snapshot.db'])
    with sqlite3.connect(path) as check:check.execute("UPDATE zotero_bindings SET status='active'")
    with pytest.raises(backup_credentials.CredentialExportError):backup_credentials.validate_excluded_backup(tmp_path,'snapshot.db',['snapshot.db'])
