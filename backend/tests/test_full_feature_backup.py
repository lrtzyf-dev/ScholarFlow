"""Archive/restore exercises real research stores, not a mock file listing."""
from contextlib import closing
from pathlib import Path
import hashlib
import shutil
import sqlite3
import threading

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.main import app
from app import artifact_store, data_backup, source_store
from app.db import engine
from app.config import DATA_DIR, PDF_DIR, PARSED_DIR, settings
from app.models import Conversation, Paper
from app.artifact_models import ArtifactChange
from app.research_models import ReadingAnnotation, ReadingState, MatrixRevision
from app.source_models import SourceLibraryBinding, SourceRevision, SourceSnapshot
from app.goal_models import Goal, GoalRequirement, GoalRevision
from app.runtime_models import RuntimeRun, RunAttempt
from app.diagnostic_records import DiagnosticRecord, DiagnosticRecoveryAction


def test_six_originals_research_history_and_edited_artifact_survive_full_restart_restore(db, tmp_path, monkeypatch):
    from app import task_runner, research_runs
    from tests.test_research_persistence import seed, create, extract
    monkeypatch.setattr(task_runner, '_shutting_down', threading.Event())
    monkeypatch.setattr(research_runs, 'start', lambda ident: None)
    db.add_all([Conversation(id='research', title='Synthetic retained research'), Conversation(id='unrelated')])
    db.commit()
    with TestClient(app) as client:
        papers, originals = [], []
        for index, extension in enumerate(('pdf', 'md', 'txt', 'docx', 'xlsx', 'csv')):
            paper, original = seed(client, index, extension)
            papers.append(paper); originals.append(original)
        matrix = create(client, papers)
        matrix_path = '/api/research/matrices/' + matrix['id']
        matrix = extract(client, matrix)
        response = client.patch(matrix_path + '/cell', json={'conversation_id': 'research', 'expected_version': matrix['version'],
            'field_key': 'method', 'paper_id': papers[0], 'value': 'Human checked value', 'status': 'supported', 'note': 'Synthetic reviewed evidence'})
        assert response.status_code == 200, response.text
        expected_matrix = response.json()
        rid = originals[1]['revision_id']
        locator = client.post(f'/api/sources/revisions/{rid}/locators', json={'conversation_id':'research', 'kind':'text_lines', 'line_start':2, 'line_end':2}).json()
        note = client.post(f'/api/research/reading/{rid}/annotations', json={'conversation_id':'research', 'locator_id':locator['locator_id'],
            'kind':'note', 'body':'Retained across full restore', 'selected_text':locator['quote']}).json()
        assert note.get('id'), note
        assert client.put(f'/api/research/reading/{rid}', json={'conversation_id':'research', 'expected_version':0,
            'position':{'locator_id':locator['locator_id'], 'zoom':1.5}, 'user_read':True}).status_code == 200
    artifact = artifact_store.create(db, request_id='complete-backup-artifact', title='Research result', kind='document',
        content='# Method\nOriginal text\n\n# Result\nPreserve human result\n', analysis={},
        evidence=[{'id':'e7', 'revision_id':rid, 'locator_id':locator['locator_id']}], conversation_id='research')
    item = artifact_store.get(db, artifact['id'], 'research')
    change = artifact_store.propose(db, item, request_id='complete-backup-edit', base_revision_id=item.current_revision_id,
        content='# Method\nCorrected method\n\n# Result\nPreserve human result\n')
    artifact_store.decide(db, item, db.get(ArtifactChange, change['id']), accept=True, expected_revision_id=item.current_revision_id)
    expected_head = item.current_revision_id
    db.add_all([
        RuntimeRun(id='backup-logical-run', kind='foreground', conversation_id='research'),
        RunAttempt(id='backup-logical-attempt', run_id='backup-logical-run', number=1, writer_token='old-writer', checkpoint={'phase':'paused', 'private_synthetic':'retained'}),
        Goal(id='backup-goal', scope='run', run_id='backup-logical-run', conversation_id='research', description='Preserve independent requirements', source_message_id='input'),
        GoalRequirement(goal_id='backup-goal', identity='export', title='Export remains required', origin='user', source_message_id='input', source_revision=1),
        GoalRevision(goal_id='backup-goal', revision=1, action='create', source='user', source_message_id='input'),
        DiagnosticRecord(id='backup-diagnostic', conversation_id='research', kind='local', status='ok', payload={'remote_requests':0}),
        DiagnosticRecoveryAction(id='backup-diagnostic-action', diagnostic_id='backup-diagnostic', kind='inspect_local'),
    ])
    db.commit()
    source = tmp_path / 'source'; source.mkdir()
    data_backup._database_snapshot(Path(engine.url.database), source / settings.db_name)
    # Copy only application-managed files; original bytes are also fixed in
    # SourceSnapshot. This test never accesses a user's external project.
    for folder in (Path(PDF_DIR).relative_to(DATA_DIR), Path(PARSED_DIR).relative_to(DATA_DIR)):
        if (Path(DATA_DIR) / folder).exists():
            shutil.copytree(Path(DATA_DIR) / folder, source / folder)
    exported = data_backup.create_backup(source, database=settings.db_name, origin_data_dir=DATA_DIR)
    target = tmp_path / 'fresh-install'
    preview = data_backup.prepare_restore(target, exported['path'], expected_database=settings.db_name)
    data_backup.schedule_restore(target, preview['id'])
    data_backup.apply_pending_restore(target, expected_database=settings.db_name)
    restored_engine = create_engine('sqlite:///' + str(target / settings.db_name))
    try:
        with Session(restored_engine) as restored:
            for original, paper_id in zip(originals, papers):
                record = source_store.read_revision(restored, original['revision_id'], scope=source_store.SourceScope('conversation','research'), include_raw=True)
                assert hashlib.sha256(record['raw_bytes']).hexdigest() == original['raw_sha256']
                binding = restored.get(SourceLibraryBinding, paper_id)
                canonical = restored.get(SourceRevision, binding.revision_id)
                assert restored.get(SourceSnapshot, canonical.snapshot_id).raw_sha256 == original['raw_sha256']
                paper = restored.get(Paper, paper_id)
                for path in (paper.pdf_path, paper.parsed_path):
                    if path:
                        assert Path(path).is_relative_to(target) and Path(path).is_file()
            state = restored.get(ReadingState, rid)
            # Imported library notes/state are owned by its canonical revision.
            if state is None:
                state = restored.get(ReadingState, restored.get(SourceLibraryBinding, papers[1]).revision_id)
            assert state.user_read and state.position['zoom'] == 1.5
            assert restored.get(ReadingAnnotation, note['id']).body == 'Retained across full restore'
            history = list(restored.scalars(select(MatrixRevision).where(MatrixRevision.matrix_id == matrix['id']).order_by(MatrixRevision.version)))
            assert len(history) == expected_matrix['version']
            assert history[-1].document['data'] == expected_matrix['document']
            result = artifact_store.get(restored, artifact['id'], 'research')
            assert result.current_revision_id == expected_head
            assert artifact_store.revision(restored, result).content.endswith('# Result\nPreserve human result\n')
            assert len(artifact_store.revision(restored, result).evidence) == 1
            assert restored.get(Goal, 'backup-goal').description == 'Preserve independent requirements'
            assert restored.get(RunAttempt, 'backup-logical-attempt').checkpoint['private_synthetic'] == 'retained'
            assert restored.get(DiagnosticRecoveryAction, 'backup-diagnostic-action')
        with closing(sqlite3.connect(target / settings.db_name)) as restored:
            assert restored.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    finally:
        restored_engine.dispose()
