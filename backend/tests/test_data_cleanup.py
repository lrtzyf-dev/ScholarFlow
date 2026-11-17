"""Deletion is scoped, atomic, complete across stores, and explicit about cloud."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text

from app import data_cleanup, artifact_store
from app.db import Base
from app.main import app
from app.models import Conversation, DesktopState, DeletedConversation, QARecord, Paper, ProjectChange
from app.provider_models import ProviderFileBinding, ProviderOperationContext
from app.source_models import Source, SourceSnapshot, SourceRevision
from app.source_store import SourceScope, SourceAuthorization, capture_source, add_text_locator
from app.runtime_models import RuntimeRun, RunAttempt, RunAdmissionRequest
from app.resource_models import ResourceBudget, ResourceReservation, PersistentSubtask, PersistentSubtaskAttempt, SubtaskCallLink
from app.research_models import ResearchMatrix, MatrixRevision, ReadingAnnotation, AnnotationRevision
from app.goal_models import Goal, GoalRequirement, GoalRevision
from app.ledger_models import CallRecord, CallUsageReceipt
from app.diagnostic_records import DiagnosticRecord, DiagnosticRecoveryAction
from app.pricing_models import CallPriceAssessment, CallChargeConfirmation, BudgetChargeEvent
from app.run_center_models import RunAttentionReceipt, RunNotificationReceipt

client = TestClient(app)


def source(db, cid):
    scope = SourceScope('conversation', cid)
    row = capture_source(db, scope=scope, authorization=SourceAuthorization(scope, 'user-upload', 'upload'),
        kind='upload', title='private.md', raw_bytes=b'private content', text='private content', media_type='text/markdown')
    locator = add_text_locator(db, row['revision_id'], scope=scope, line_start=1, line_end=1)
    db.commit()
    return row, locator


def test_all_owned_content_removed_and_independent_library_other_chat_journal_preserved(db):
    db.add_all([Conversation(id='remove-me'), Conversation(id='keep-me'), Paper(id='library-paper', title='Retained independent original')]); db.commit()
    fixed, locator = source(db, 'remove-me')
    other, _ = source(db, 'keep-me')
    artifact_store.create(db, request_id='cleanup-document123', title='私有成果', kind='document', content='private artifact',
        evidence=[{'id': 'e7', 'revision_id': fixed['revision_id'], 'locator_id': locator['locator_id']}], analysis={}, conversation_id='remove-me')
    db.add_all([
        QARecord(id='qa-remove', conversation_id='remove-me', question='private prompt', answer='private answer'),
        QARecord(id='qa-keep', conversation_id='keep-me', question='other prompt', answer='other answer'),
        DesktopState(key='selection', revision=1, value={'conversation_id':'remove-me', 'other_setting': True}),
        RuntimeRun(id='run-remove', kind='foreground', conversation_id='remove-me'),
        RunAttempt(id='attempt-remove', run_id='run-remove', number=1, writer_token='writer', checkpoint={'private':'checkpoint'}),
        RunAdmissionRequest(run_id='run-remove', request_id='request-remove', attempt_id='attempt-remove'),
        Goal(id='goal-remove', scope='run', run_id='run-remove', conversation_id='remove-me', description='private goal', source_message_id='input'),
        GoalRequirement(goal_id='goal-remove', identity='requirement', title='private requirement', origin='user', source_message_id='input', source_revision=1),
        GoalRevision(goal_id='goal-remove', revision=1, action='create', source='user', source_message_id='input'),
        ResearchMatrix(id='matrix-remove', conversation_id='remove-me', title='private matrix', document={'private':'matrix'}),
        MatrixRevision(matrix_id='matrix-remove', version=1, action='create', document={'private':'matrix history'}),
        ReadingAnnotation(id='annotation-remove', revision_id=fixed['revision_id'], locator_id=locator['locator_id'], kind='note', body='private note'),
        AnnotationRevision(annotation_id='annotation-remove', version=1, document={'body':'private note history'}),
        ResourceBudget(id='budget-remove', owner_run_id='run-remove'),
        ResourceReservation(id='reservation-remove', budget_id='budget-remove', run_id='run-remove'),
        PersistentSubtask(id='subtask-remove', parent_run_id='run-remove', fingerprint='fingerprint', specification={'private':'task'}, checkpoint={'private':'child checkpoint'}),
        PersistentSubtaskAttempt(id='child-attempt-remove', subtask_id='subtask-remove', number=1, writer_token='writer', checkpoint={'private':'attempt checkpoint'}),
        CallRecord(id='call-remove', request_id='request', run_id='subtask-remove', provider='local', service='chat', endpoint='http://localhost/chat'),
        SubtaskCallLink(call_id='call-remove', subtask_id='subtask-remove', attempt=1),
        CallUsageReceipt(id='usage-remove', call_id='call-remove', event_id='event', source='provider', mode='delta', units={'total_tokens':10}),
        ProviderOperationContext(operation_id='operation-remove', conversation_id='remove-me', payload={'private':'opaque signed state'}),
        DiagnosticRecord(id='diagnostic-remove', run_id='run-remove', kind='local', status='error', payload={'state':'synthetic scoped diagnostic'}),
        DiagnosticRecoveryAction(id='diagnostic-action-remove', diagnostic_id='diagnostic-remove', kind='inspect_local'),
        CallPriceAssessment(call_id='call-remove', details={'note':'private accounting detail'}),
        CallChargeConfirmation(id='charge-remove', call_id='call-remove', revision=1, amount='0.02', currency='CNY', reference='private invoice reference'),
        BudgetChargeEvent(id='charge-event-remove', reservation_id='reservation-remove', call_id='call-remove', revision=1, amount_micros=20000,
            currency='CNY', source='user_confirmed', reference='private invoice reference'),
        RunAttentionReceipt(item_id='run:run-remove', event_key='private-run-state'),
        RunNotificationReceipt(item_id='subtask:subtask-remove', event_key='private-subtask-state'),
        RunNotificationReceipt(item_id='run:keep-owner', event_key='retained-unrelated-state'),
        ProjectChange(id='project-journal', project_id='project', conversation_id='remove-me', request_hash='hash', scope={}, path='result.md', operation='create'),
    ]); db.commit()
    before = data_cleanup.preview(db, 'remove-me')
    assert before['records']['persistent_subtask_attempts'] == 1 and before['records']['source_snapshots'] == 1
    assert all(before['records'].get(name) == 1 for name in ('diagnostic_records', 'diagnostic_recovery_actions',
        'call_price_assessments', 'call_charge_confirmations', 'budget_charge_events', 'run_attention_receipts', 'run_notification_receipts', 'subtask_call_links'))
    assert before['retained_project_journals'] == {'project_changes':1}
    response = client.delete('/api/conversations/remove-me')
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(DeletedConversation, 'remove-me') and db.get(Conversation, 'remove-me') is None
    assert db.get(QARecord, 'qa-keep') and db.get(Paper, 'library-paper') and db.get(ProjectChange, 'project-journal')
    assert db.get(RunNotificationReceipt, 'run:keep-owner')
    assert db.get(SourceRevision, other['revision_id']) and db.get(SourceRevision, fixed['revision_id']) is None
    remaining = data_cleanup.preview(db, 'remove-me')
    assert remaining['total_records'] == 0
    assert db.get(DesktopState, 'selection').value == {'conversation_id':None,'other_setting':True}
    assert client.delete('/api/conversations/remove-me').json()['already_deleted']
    assert client.get('/api/conversations/remove-me/deletion-preview').status_code == 410


def test_mid_delete_failure_rolls_back_all_stores(db, monkeypatch):
    db.add(Conversation(id='rollback')); db.commit()
    source(db, 'rollback')
    db.add(QARecord(id='private-answer', conversation_id='rollback', question='q', answer='retained-on-failure')); db.commit()
    expected = data_cleanup.preview(db, 'rollback')['total_records']
    original = db.execute
    count = 0
    def execute(statement, *args, **kwargs):
        nonlocal count
        if getattr(statement, 'is_delete', False):
            count += 1
            if count == 2: raise OSError('synthetic transaction interruption')
        return original(statement, *args, **kwargs)
    monkeypatch.setattr(db, 'execute', execute)
    with pytest.raises(OSError): data_cleanup.remove(db, 'rollback')
    db.rollback()
    assert data_cleanup.preview(db, 'rollback')['total_records'] == expected
    assert db.get(QARecord, 'private-answer').answer == 'retained-on-failure'


@pytest.mark.parametrize('state,allowed', [('prepared',True),('deleted',True),('manual_cleanup_acknowledged',True),
    ('available',False),('unknown',False),('uploading',False),('expired',False),('delete_pending',False)])
def test_cloud_state_never_assumes_remote_deletion(db, state, allowed):
    db.add(Conversation(id='cloud')); db.commit()
    db.add(ProviderFileBinding(id='binding', conversation_id='cloud', revision_id='revision', raw_sha256='hash', title='cloud.pdf',
        media_type='application/pdf', size_bytes=10, purpose='file-extract', expires_seconds=3600,
        request_id='cloud-request', configuration_version=1, credential_ref='revoked-credential', policy={}, state=state)); db.commit()
    preview = client.get('/api/conversations/cloud/deletion-preview').json()
    assert preview['can_delete'] is allowed
    response = client.delete('/api/conversations/cloud')
    assert response.status_code == (200 if allowed else 409), response.text
    db.expire_all()
    assert (db.get(Conversation, 'cloud') is None) is allowed


def test_many_records_chunked_deletion_and_scoped_table_classification(db):
    db.add(Conversation(id='many'))
    db.add_all([QARecord(id=f'qa-{n}', conversation_id='many', question='q', answer='a') for n in range(450)]); db.commit()
    preview = client.get('/api/conversations/many/deletion-preview').json()
    assert preview['records']['qa_records'] == 450
    assert client.delete('/api/conversations/many').status_code == 200
    assert db.scalar(select(func.count()).select_from(QARecord)) == 0
    tables, predicates, _ = data_cleanup._plan('any')
    for name, table in tables.items():
        if 'conversation_id' in table.c:
            assert name in data_cleanup.RETAINED or predicates[name] is not None, name
