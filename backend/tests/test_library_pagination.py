from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy import event
from app.main import app
from app.models import Paper,Reference
from app import source_store,source_library
from app.source_models import SourceLibraryImport

client=TestClient(app)


def test_page_bounds_statistics_literal_search_and_stable_ties(db):
    for index in range(65):
        db.add(Paper(id=f'paper-{index:03}',title=f'Article {index:03}',authors='Researcher',abstract='100% material' if index==2 else 'ordinary',
            status='parsed' if index%2==0 else 'metadata_only',page_count=index,created_at=datetime(2026,1,1)))
    db.add(Reference(paper_id='paper-000',ref_index=1,linked_paper_id='paper-002',page=1));db.commit()
    first=client.get('/api/papers/page?limit=40').json();second=client.get('/api/papers/page?limit=40&offset=40').json()
    assert first['total']==65 and len(first['items'])==40 and len(second['items'])==25
    assert first['next_offset']==40 and second['next_offset'] is None
    assert len({p['id'] for p in first['items']+second['items']})==65
    assert first['statistics']=={'papers':65,'pages':sum(range(65)),'parsed':33,'internal_references':1}
    assert client.get('/api/papers/page',params={'q':'100%'}).json()['total']==1
    assert client.get('/api/papers/page',params={'q':'%'}).json()['total']==1
    assert client.get('/api/papers/page?status=parsed&sort=title').json()['items'][0]['id']=='paper-000'
    assert client.get('/api/papers/page?sort=pages').json()['items'][0]['id']=='paper-064'
    assert client.get('/api/papers/page?limit=101').status_code==422


def test_list_source_metadata_never_loads_original_or_extraction_body(db):
    scope=source_store.SourceScope('library','metadata-paper')
    item=source_store.capture_source(db,scope=scope,authorization=source_store.SourceAuthorization(scope,'metadata-test','upload'),
        kind='upload',title='original.md',media_type='text/markdown',raw_bytes=b'original bytes',text='original bytes',text_origin='provided_text')
    db.add(Paper(id='metadata-paper',title='Metadata'));db.add(SourceLibraryImport(revision_id=item['revision_id'],paper_id='metadata-paper',request_id='test',raw_sha256=item['raw_sha256']));db.commit()
    statements=[]
    def observe(conn,cursor,statement,parameters,context,executemany): statements.append(statement.lower())
    event.listen(db.get_bind(),'before_cursor_execute',observe)
    try: result=source_library.library_metadata_many(db,['metadata-paper'])
    finally: event.remove(db.get_bind(),'before_cursor_execute',observe)
    assert result['metadata-paper']['source_media_type']=='text/markdown'
    assert not any('source_snapshots.raw_bytes' in query or 'source_snapshots.text,' in query for query in statements)
    assert not any('source_extractions.document as' in query for query in statements)
