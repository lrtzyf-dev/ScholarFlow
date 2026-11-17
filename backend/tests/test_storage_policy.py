"""Retention is explicit review; logical bytes never claim physical erasure."""
from datetime import timedelta,timezone
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app.main import app
from app import storage_policy as service,data_cleanup
from app.models import Conversation,QARecord,DeletedConversation,DesktopState
from app.storage_models import CleanupReceipt
from app.source_store import SourceScope,SourceAuthorization,capture_source
from app.timeutil import utcnow

client=TestClient(app)


def seed(db,cid='storage-private'):
    db.add(Conversation(id=cid,title='SECRET_TITLE',archived_at=utcnow()-timedelta(days=200)))
    db.add(QARecord(id='storage-answer-'+cid,conversation_id=cid,question='SECRET_QUESTION',answer='SECRET_ANSWER'))
    db.flush();scope=SourceScope('conversation',cid)
    capture_source(db,scope=scope,authorization=SourceAuthorization(scope,'test-upload','upload'),kind='upload',
        title='SECRET_FILENAME.md',raw_bytes='中文原件'.encode(),text='中文原件',media_type='text/markdown')
    db.commit()


def test_default_policy_read_does_not_write_or_remove(db):
    seed(db)
    assert service.policy(db)['revision']==0
    assert not service.policy(db)['automatic_deletion']
    assert db.get(DesktopState,service.KEY)is None
    assert db.get(Conversation,'storage-private')


def test_policy_is_persistent_cas_and_all_fields_validated(db):
    value={**service.DEFAULT,'archived_review_days':30}
    r=client.put('/api/storage/policy',json={'expected_revision':0,**value})
    assert r.status_code==200,r.text
    db.expire_all();assert service.policy(db)['values']['archived_review_days']==30
    assert client.put('/api/storage/policy',json={'expected_revision':0,**value}).status_code==409
    for key,bad in [('archived_review_days',True),('backup_reminder_days',0),('capacity_warning_bytes',1024),('backup_review_days',1.5)]:
        assert client.put('/api/storage/policy',json={'expected_revision':1,**value,key:bad}).status_code==422


def test_preview_measures_utf8_blob_payload_and_disclaims_disk_reclaim(db):
    seed(db)
    r=client.get('/api/conversations/storage-private/deletion-preview')
    assert r.status_code==200,r.text
    space=r.json()['storage_preview']
    assert space['logical_payload_bytes']>len('中文原件'.encode())*2
    assert space['by_table']['source_snapshots']>=len('中文原件'.encode())*2
    assert space['physical_reclaimed_bytes']is None
    assert space['backup_copies_deleted']is False
    assert not space['includes_database_pages_or_indexes']
    assert db.scalar(select(func.count()).select_from(CleanupReceipt))==0


def test_receipt_atomic_survives_deletion_but_contains_no_private_content(db):
    seed(db)
    r=client.delete('/api/conversations/storage-private')
    assert r.status_code==200,r.text
    rid=r.json()['cleanup']['receipt_id'];db.expire_all()
    assert db.get(DeletedConversation,'storage-private')
    row=db.get(CleanupReceipt,rid);assert row
    raw=json.dumps(service.receipts(db),ensure_ascii=False)
    for private in ('SECRET_TITLE','SECRET_QUESTION','SECRET_ANSWER','SECRET_FILENAME','storage-private','中文原件'):
        assert private not in raw
    assert row.document['remote_erasure_verified']is False and row.document['backups_erased']is False
    assert data_cleanup.preview(db,'storage-private')['total_records']==0
    assert client.delete('/api/conversations/storage-private').json()['already_deleted']
    assert db.scalar(select(func.count()).select_from(CleanupReceipt))==1


def test_rollback_removes_receipt_and_restores_originals(db):
    seed(db)
    data_cleanup.remove(db,'storage-private')
    db.rollback()
    assert db.get(Conversation,'storage-private')
    assert db.scalar(select(func.count()).select_from(CleanupReceipt))==0


def test_tampered_receipt_refused(db):
    seed(db);client.delete('/api/conversations/storage-private');db.expire_all()
    row=db.scalar(select(CleanupReceipt));row.document={'total_records':999};db.commit()
    assert client.get('/api/storage/cleanup-receipts').status_code==409


def test_receipts_paginate_without_content_identity(db):
    for i in range(33):service.record_cleanup(db,str(i),{'records':{},'total_records':0,'storage_preview':{'logical_payload_bytes':0},'retained_project_journals':{},'preserved':[],'disk_erasure':'logical_removal_only'})
    db.commit()
    first=client.get('/api/storage/cleanup-receipts').json()
    service.record_cleanup(db,'new-after-first',{'records':{},'total_records':0,'storage_preview':{'logical_payload_bytes':0},'retained_project_journals':{},'preserved':[],'disk_erasure':'logical_removal_only'});db.commit()
    second=client.get('/api/storage/cleanup-receipts',params={'cursor':first['next_cursor']}).json()
    assert len(first['items'])==30 and first['next_cursor']
    assert len(second['items'])==3 and second['next_cursor']is None
    assert not ({r['id']for r in first['items']}&{r['id']for r in second['items']})
    assert client.get('/api/storage/cleanup-receipts',params={'cursor':'invalid!'}).status_code==422


def test_retention_review_only_lists_old_archived_and_never_deletes(db,tmp_path,monkeypatch):
    from app import config,backup_service
    monkeypatch.setattr(config,'DATA_DIR',tmp_path/'data');monkeypatch.setattr(backup_service,'DATA_DIR',tmp_path/'data')
    seed(db)
    db.add_all([Conversation(id='recent',archived_at=utcnow()-timedelta(days=1)),Conversation(id='active',created_at=utcnow()-timedelta(days=999))]);db.commit()
    result=service.status(db)
    assert [v['id']for v in result['archived_review']['items']]==['storage-private']
    assert result['automatic_deletion']is False
    assert db.get(Conversation,'storage-private')and db.get(Conversation,'active')
    assert result['reminders']['backup_due']is True
    assert str(tmp_path)not in json.dumps(result)


def test_space_categories_and_utc_backup_review(db,tmp_path,monkeypatch):
    from app import config,backup_service
    from app.data_backup import recovery_root
    data=tmp_path/'data';(data/'pdf').mkdir(parents=True);(data/'parsed').mkdir()
    (data/'test.db').write_bytes(b'd'*10);(data/'pdf'/'paper.pdf').write_bytes(b'p'*20)
    (data/'parsed'/'paper.json').write_bytes(b'j'*30);(data/'test.npy').write_bytes(b'i'*40)
    recovery=recovery_root(data);(recovery/'exports').mkdir(parents=True)
    backup=recovery/'exports'/('a'*32+'.sfbak');backup.write_bytes(b'b'*50)
    old=(utcnow()-timedelta(days=100)).replace(tzinfo=timezone.utc).timestamp();os.utime(backup,(old,old))
    monkeypatch.setattr(config,'DATA_DIR',data);monkeypatch.setattr(backup_service,'DATA_DIR',data)
    result=service.status(db)
    assert result['storage']['by_category']=={'database':10,'originals':20,'parsed':30,'indexes':40,'other':0}
    assert result['storage']['recovery_file_bytes']==50
    assert result['backup_review'][0]['age_days']==100
    assert result['storage']['total_file_bytes']==150


def test_recent_successful_backup_clears_reminder_failed_does_not(db,tmp_path,monkeypatch):
    from app import config,backup_service
    from app.data_backup import recovery_root
    data=tmp_path/'data'
    monkeypatch.setattr(config,'DATA_DIR',data);monkeypatch.setattr(backup_service,'DATA_DIR',data)
    now=utcnow().replace(tzinfo=timezone.utc).isoformat()
    jobs=recovery_root(data)/'jobs';jobs.mkdir(parents=True)
    target=jobs/('a'*32+'.json')
    target.write_text(json.dumps({'kind':'create','status':'succeeded','created_at':now,'finished_at':now}),encoding='utf-8')
    for i in range(35):(jobs/(f'{i:032x}'+'.json')).write_text(json.dumps({'kind':'prepare','status':'succeeded','created_at':now}),encoding='utf-8')
    assert service.status(db)['reminders']['backup_due']is False
    target.write_text(json.dumps({'kind':'create','status':'failed','created_at':now}),encoding='utf-8')
    assert service.status(db)['reminders']['backup_due']is True


def test_capacity_threshold_warns_without_cancelling_tasks_or_deleting(db,tmp_path,monkeypatch):
    from app import config,backup_service
    data=tmp_path/'data';data.mkdir();target=data/'owned.bin';target.write_bytes(b'x'*(1024**2+1))
    monkeypatch.setattr(config,'DATA_DIR',data);monkeypatch.setattr(backup_service,'DATA_DIR',data)
    service.update_policy(db,{**service.DEFAULT,'capacity_warning_bytes':1024**2},0);db.commit()
    assert service.status(db)['reminders']['capacity_warning']is True
    assert target.exists()


def test_symlink_file_does_not_count_external_data(tmp_path):
    outside=tmp_path/'external';outside.write_bytes(b'private-external-data')
    root=tmp_path/'data';root.mkdir()
    try:(root/'linked').symlink_to(outside)
    except OSError:pytest.skip('Windows symlink permission unavailable')
    files,skipped=service._files(root)
    assert files==[]and skipped==1


@pytest.mark.skipif(os.name!='nt',reason='Windows junction boundary')
def test_real_windows_junction_not_followed(tmp_path):
    import subprocess
    root=tmp_path/'application';root.mkdir()
    external=tmp_path/'external';external.mkdir();(external/'private.bin').write_bytes(b'private-junction-data')
    link=root/'junction'
    script=tmp_path/'make-junction.ps1'
    script.write_text('param([string]$LinkPath,[string]$TargetPath)\nNew-Item -ItemType Junction -Path $LinkPath -Target $TargetPath -ErrorAction Stop | Out-Null',encoding='utf-8')
    process=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(script),str(link),str(external)],
        capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode==0,process.stderr
    try:
        files,skipped=service._files(root)
        assert files==[]and skipped==1
        assert (external/'private.bin').read_bytes()==b'private-junction-data'
    finally:
        # Remove only the junction entry. Never recursively delete its target.
        if link.exists():link.rmdir()
