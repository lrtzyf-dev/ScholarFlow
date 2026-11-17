"""Original draft bytes survive reconnects but never become evidence implicitly."""
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func,select,update
from app.main import app
from app.db import SessionLocal
from app.models import Conversation,DeletedConversation,Paper
from app.source_models import SourceRevision,SourceSnapshot
from app.source_update_models import SourceUpdateDraft
from app import source_update_store as service,source_store as store
from app.timeutil import utcnow
from tests.test_source_diff import upload


@pytest.fixture
def client(db):
    db.add_all([Conversation(id="diff-a"),Conversation(id="diff-b")]);db.commit()
    return TestClient(app)


def url(source):return f'/api/source-versions/{source["source_id"]}/update-draft'


def stage(client,source,raw=b"draft v2",revision=0,request="draft-selection-first",name="update.md",**kwargs):
    return client.put(url(source),data={"conversation_id":"diff-a","expected_revision":revision,
        "baseline_revision_id":source["revision_id"],"request_id":request,**kwargs},files={"file":(name,raw)})


def read(client,source,**kwargs):return client.get(url(source),params={"conversation_id":"diff-a",**kwargs})


def test_draft_is_separate_persistent_and_original_bytes_round_trip(client,db):
    source=upload(client,b"fixed v1")
    saved=stage(client,source,"# private draft\n中文原件".encode()).json()
    assert saved["revision"]==1 and saved["draft"]["expected_version"]==1
    assert saved["draft"]["persistence"]=="application_database"
    with SessionLocal()as fresh:
        restored=service.read(fresh,store.SourceScope("conversation","diff-a"),source["source_id"])
        assert restored==saved
    original=client.get(url(source)+"/file",params={"conversation_id":"diff-a","revision":1})
    assert original.content=="# private draft\n中文原件".encode()
    assert original.headers["content-type"]=="application/octet-stream"
    assert original.headers["cache-control"]=="no-store"
    assert db.scalar(select(func.count()).select_from(SourceRevision))==1
    assert db.scalar(select(func.count()).select_from(Paper))==0
    assert "private draft"not in str(read(client,source).json())


def test_concurrent_drafts_cas_and_lost_response_replay(client,db):
    source=upload(client,b"fixed v1")
    def put(i):return stage(TestClient(app),source,f"draft {i}".encode(),request=f"draft-selection-{i}")
    with ThreadPoolExecutor(2)as pool:responses=list(pool.map(put,[1,2]))
    assert sorted(r.status_code for r in responses)==[200,409]
    winner=next(r.json()for r in responses if r.status_code==200)
    i=winner["draft"]["request_id"].rsplit("-",1)[1]
    replay=stage(client,source,f"draft {i}".encode(),request=f"draft-selection-{i}")
    assert replay.status_code==200 and replay.json()["revision"]==1
    assert stage(client,source,b"changed",request=f"draft-selection-{i}").status_code==409
    assert db.scalar(select(func.count()).select_from(SourceUpdateDraft))==1


def test_cancel_clears_bytes_and_tombstone_prevents_old_writer(client,db):
    source=upload(client,b"fixed v1");stage(client,source)
    response=client.post(url(source)+"/discard",json={"conversation_id":"diff-a","expected_revision":1})
    assert response.status_code==200 and response.json()=={"revision":2,"state":"cancelled","draft":None}
    db.expire_all();row=db.get(SourceUpdateDraft,("diff-a",source["source_id"]))
    assert row.raw_bytes is None and row.raw_size==0 and row.filename is None
    assert stage(client,source,request="draft-old-writer",revision=0).status_code==409
    assert client.get(url(source)+"/file",params={"conversation_id":"diff-a","revision":1}).status_code==404
    assert stage(client,source,request="draft-new-writer",revision=2).status_code==200


def test_explicit_upload_consumes_matching_staging_same_transaction(client,db):
    source=upload(client,b"fixed v1");stage(client,source,request="draft-promote-to-source")
    result=upload(client,b"draft v2",name="update.md",prior=source,request="draft-promote-to-source")
    assert result["version"]==2
    assert read(client,source).json()=={"revision":2,"state":"consumed","draft":None}
    assert db.scalar(select(func.count()).select_from(Paper))==0
    again=upload(client,b"draft v2",name="update.md",prior=source,request="draft-promote-to-source")
    assert again["revision_id"]==result["revision_id"]and again["replayed"]


def test_failed_promote_keeps_original_draft_and_source(client,db,monkeypatch):
    source=upload(client,b"fixed v1");stage(client,source,request="draft-promote-rollback")
    original=service._clear
    def fail(row,state):original(row,state);raise RuntimeError("inject after staging clear")
    monkeypatch.setattr(service,"_clear",fail)
    with pytest.raises(RuntimeError,match="inject"):
        upload(client,b"draft v2",name="update.md",prior=source,request="draft-promote-rollback")
    db.expire_all()
    assert read(client,source).json()["state"]=="pending"
    assert db.scalar(select(func.count()).select_from(SourceRevision))==1


def test_scope_archive_deleted_and_filename_boundaries(client,db):
    source=upload(client,b"fixed v1");stage(client,source)
    assert read(client,source,conversation_id="diff-b").status_code==404
    assert stage(client,source,conversation_id="diff-b").status_code==404
    assert stage(client,source,name="../../secret.md").status_code==422
    db.get(Conversation,"diff-a").archived_at=utcnow();db.commit()
    assert read(client,source).status_code==200
    assert stage(client,source,revision=1,request="draft-archived").status_code==409
    assert client.post(url(source)+"/discard",json={"conversation_id":"diff-a","expected_revision":1}).status_code==409
    db.add(DeletedConversation(id="diff-a"));db.commit()
    assert read(client,source).status_code==410


def test_draft_baseline_other_source_and_corruption_refused(client,db):
    source=upload(client,b"fixed v1");other=upload(client,b"other",request="draft-other-source")
    assert stage(client,source,baseline_revision_id=other["revision_id"]).status_code==422
    stage(client,source)
    db.execute(update(SourceUpdateDraft).values(raw_bytes=b"tampered"));db.commit()
    assert client.get(url(source)+"/file",params={"conversation_id":"diff-a","revision":1}).status_code==409


def test_late_original_download_refuses_replaced_draft(client):
    source=upload(client,b"fixed v1");stage(client,source)
    stage(client,source,b"other draft",revision=1,request="draft-selection-replacement")
    assert client.get(url(source)+"/file",params={"conversation_id":"diff-a","revision":1}).status_code==409


def test_conversation_deletion_counts_and_removes_staging_with_receipt(client,db):
    source=upload(client,b"fixed v1");stage(client,source,b"sensitive draft original")
    preview=client.get('/api/conversations/diff-a/deletion-preview').json()
    assert preview["records"]["source_update_drafts"]==1
    assert preview["storage_preview"]["by_table"]["source_update_drafts"]>=len(b"sensitive draft original")
    assert client.delete('/api/conversations/diff-a').status_code==200
    db.expire_all();assert db.scalar(select(func.count()).select_from(SourceUpdateDraft))==0


def test_draft_capacity_is_per_conversation_and_explicit(client,db,monkeypatch):
    source=upload(client,b"fixed v1");monkeypatch.setattr(service,"MAX_CONVERSATION_DRAFT_BYTES",10)
    assert stage(client,source,b"a"*11).status_code==413
    assert db.scalar(select(func.count()).select_from(SourceUpdateDraft))==0


def test_staged_original_survives_full_backup_restore(client,db,tmp_path):
    from app import data_backup
    from app.db import engine
    from app.config import settings,DATA_DIR
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    source=upload(client,b"fixed v1");stage(client,source,b"persistent original")
    data=tmp_path/'snapshot';data.mkdir()
    data_backup._database_snapshot(Path(engine.url.database),data/settings.db_name)
    backup=data_backup.create_backup(data,database=settings.db_name,origin_data_dir=DATA_DIR)
    target=tmp_path/'fresh-install'
    prepared=data_backup.prepare_restore(target,backup['path'],expected_database=settings.db_name)
    data_backup.schedule_restore(target,prepared['id']);data_backup.apply_pending_restore(target,expected_database=settings.db_name)
    restored_engine=create_engine('sqlite:///'+str(target/settings.db_name))
    try:
        with Session(restored_engine)as restored:
            row=service.original(restored,store.SourceScope('conversation','diff-a'),source['source_id'],1)
            assert row.raw_bytes==b'persistent original'
            assert row.raw_sha256==hashlib.sha256(row.raw_bytes).hexdigest()
    finally:restored_engine.dispose()


def test_upgrade_legacy_staging_digest_requires_exact_original_hash(client,db):
    source=upload(client,b"fixed v1");stage(client,source)
    row=db.get(SourceUpdateDraft,('diff-a',source['source_id']))
    values=service._identity(row);row.fingerprint=hashlib.sha256(json.dumps(values[:4]+values[6:],ensure_ascii=False,separators=(',',':')).encode()).hexdigest();db.commit()
    assert read(client,source).status_code==200
    db.execute(update(SourceUpdateDraft).values(filename='changed.md'));db.commit()
    assert read(client,source).status_code==409


def test_project_selection_persists_without_reading_file_and_rechecks_on_commit(client,db,tmp_path):
    from app import workspace
    root=tmp_path/'project';root.mkdir();path=root/'note.md';path.write_text('fixed v1',encoding='utf-8')
    project=workspace.register(db,str(root));conv=db.get(Conversation,'diff-a');conv.project_id=project.id;conv.workspace_revision=1;db.commit()
    capture={'conversation_id':'diff-a','request_id':'draft-local-original','project_id':project.id,'workspace_revision':1,'path':'note.md'}
    response=client.post('/api/sources/capture-project',json=capture);assert response.status_code==200,response.text
    source=response.json()
    payload={**capture,'request_id':'draft-local-selection','baseline_revision_id':source['revision_id'],'expected_revision':0}
    path.unlink()  # Staging must not reread or recreate this external file.
    response=client.put(url(source)+'/local',json=payload);assert response.status_code==200,response.text
    saved=response.json();assert saved['draft']['kind']=='local'and saved['draft']['raw_size']==0
    with SessionLocal()as fresh:
        restored=service.read(fresh,store.SourceScope('conversation','diff-a'),source['source_id'])
        assert restored==saved
    update={**capture,'request_id':'draft-local-selection','source_id':source['source_id'],'expected_version':1}
    assert client.post('/api/sources/capture-project',json=update).status_code==404
    assert read(client,source).json()['state']=='pending'
    path.write_text('current v2 after explicit confirmation',encoding='utf-8')
    response=client.post('/api/sources/capture-project',json=update)
    assert response.status_code==200,response.text
    assert response.json()['version']==2 and read(client,source).json()['state']=='consumed'
    assert db.scalar(select(func.count()).select_from(Paper))==0
