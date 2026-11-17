from contextlib import contextmanager
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import config, diagnostic_records, diagnostic_probes
from app.models import Conversation, ForegroundRun
from app.db import SessionLocal

client=TestClient(app)


def test_saved_local_diagnostics_and_export_have_no_raw_detail_or_paths(db,monkeypatch):
    from app.api import diagnostic_history
    monkeypatch.setattr(diagnostic_history,'run_diagnostics',lambda *a:{"overall":"error","checks":[
        {"id":"database","label":"private label","status":"error","detail":"test-key-48cd0fb143fc61e5 C:/Users/private raw document"},
        {"id":"untrusted","status":"error","detail":"private"}],"data_dir":"C:/Users/private"})
    result=client.post('/api/debug/records/local',json={}).json()
    assert result['payload']['checks']==[{"id":"database","label":"数据库完整性","status":"error"}]
    exported=client.get(f"/api/debug/records/{result['id']}/export-preview").json()
    assert all(secret not in json.dumps(exported) for secret in ['sk-private','C:/Users','raw document','private label'])
    assert client.get('/api/debug/records').json()['items'][0]['id']==result['id']
    action=result['actions'][0]
    assert client.post(f"/api/debug/records/actions/{action['id']}/dismiss").json()=={"state":"dismissed","repaired":False}
    assert client.post('/api/debug/records/local',json={'conversation_id':'C:/private/document.md'}).status_code==422


@pytest.mark.parametrize('status,category',[(400,'parameters'),(401,'authentication'),(402,'quota'),(429,'rate_limit'),(503,'upstream')])
def test_explicit_probe_saves_error_category_not_provider_body(db,monkeypatch,status,category):
    monkeypatch.setattr(config.settings,'llm_api_key','synthetic-history-key')
    monkeypatch.setattr(config.settings,'llm_demo_mode',False)
    monkeypatch.setattr(config.settings,'llm_base_url','https://diagnostic.example.test/v1')
    monkeypatch.setattr(config.settings,'llm_model','CaseModel')
    real=httpx.Client;calls=[]
    def handler(request):
        calls.append(request);return httpx.Response(status,json={'error':{'message':'test-key-84a07b8467f2df61 raw document'}})
    monkeypatch.setattr(httpx,'Client',lambda *a,**kw:real(*a,**kw,transport=httpx.MockTransport(handler)))
    target=next(v for v in diagnostic_probes.probe_targets() if v['service']=='llm')
    result=client.post('/api/debug/probe',json={'service':'llm','expected_config':target['expected_config']}).json()
    assert len(calls)==1 and result['failure_kind']==category and result['http_status']==status
    assert result['diagnostic_record']['payload']['failure_kind']==category
    assert 'test-key-84a07b8467f2df61' not in json.dumps(result)
    assert len(client.get('/api/debug/records').json()['items'])==1


def test_invalid_run_association_is_rejected_before_a_paid_request(db,monkeypatch):
    db.add(Conversation(id='a'));db.add(Conversation(id='b'));db.add(ForegroundRun(id='chat_association_test',conversation_id='a',question='q'));db.commit()
    from app.api import diagnostics
    monkeypatch.setattr(diagnostics,'run_probe',lambda *a:pytest.fail('cross-scope probe started'))
    assert client.post('/api/debug/probe',json={'service':'llm','expected_config':'a'*64,'conversation_id':'b','run_id':'chat_association_test'}).status_code==404


def test_embedding_config_rechecked_after_queue_before_http(db,monkeypatch):
    from app import request_resources
    monkeypatch.setattr(config.settings,'embedding_base_url','https://embedding.example.test/v1')
    monkeypatch.setattr(config.settings,'embedding_model','embed')
    monkeypatch.setattr(config.settings,'embedding_api_key','synthetic-old-key')
    target=next(v for v in diagnostic_probes.probe_targets() if v['service']=='embedding')
    original=request_resources.request_admission
    @contextmanager
    def admission(**kwargs):
        with original(**kwargs) as value:
            monkeypatch.setattr(config.settings,'embedding_api_key','synthetic-new-key')
            yield value
    monkeypatch.setattr(request_resources,'request_admission',admission)
    monkeypatch.setattr(httpx,'Client',lambda *a,**kw:pytest.fail('revoked embedding configuration reached HTTP'))
    result=diagnostic_probes.run_probe('embedding',target['expected_config'])
    assert result['status']=='error' and result['failure_kind']=='configuration_changed'


def test_corrupt_source_still_allows_owned_diagnostic_and_receipt_lookup_is_narrow(db,monkeypatch):
    from app import source_store,call_ledger
    from app.source_models import SourceSnapshot
    from sqlalchemy import update
    db.add(Conversation(id='diag-source'));db.add(Conversation(id='other-source'));db.flush()
    scope=source_store.SourceScope('conversation','diag-source')
    item=source_store.capture_source(db,scope=scope,authorization=source_store.SourceAuthorization(scope,'diag-source','upload'),
        kind='upload',title='private.md',media_type='text/markdown',raw_bytes=b'private',text='private',text_origin='provided_text')
    db.execute(update(SourceSnapshot).values(raw_bytes=b'corrupt'));db.commit()
    saved=client.post('/api/debug/records/local',json={'conversation_id':'diag-source','source_revision_id':item['revision_id']})
    assert saved.status_code==200
    assert client.post('/api/debug/records/local',json={'conversation_id':'other-source','source_revision_id':item['revision_id']}).status_code==404
    call_id=call_ledger.begin_call(provider='test',service='llm',endpoint='https://private.example.test/v1',db=db)
    receipt=diagnostic_records.save(db,{'status':'error','failure_kind':'timeout','calls':[{'call_id':call_id}]},kind='probe');db.commit()
    response=client.get(f"/api/debug/records/{receipt['id']}/calls").json()
    assert response['items'][0]['call_id']==call_id and response['items'][0]['usage_source']=='unknown'
    assert 'private.example' not in json.dumps(response)
