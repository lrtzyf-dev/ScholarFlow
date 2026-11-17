"""Real isolated filesystem/SQLite/OS locks; synthetic signed executable bytes.

No actual install, old executable, model, credential or schema migration runs.
"""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from app import legacy_install as module
from app.atomic_files import atomic_write_json
from app.release_files import digest
from app.release_info import ReleaseError
from .test_release_install import setup

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def legacy(setup):
    s = setup
    old = s['target'].parent / 'flat-legacy'; old.mkdir()
    (old / 'ScholarFlow.exe').write_bytes(b'Synthetic old unsigned executable; never run')
    (old / '_internal').mkdir(); (old / '_internal/runtime.dll').write_bytes(b'synthetic library')
    (old / 'unclassified-user-file.txt').write_text('leave me in place', encoding='utf-8')
    s.update(legacy=old, new_target=old.parent / 'future-install', destination=old.parent / 'reviewed-preparation')
    return s


def observe(s):
    return module.inspect_legacy(s['legacy'], s['data'], s['new_target'], s['candidate'], trusted_keys=s['trust'])


def prepare(s, **kwargs):
    baseline = observe(s)
    return module.prepare_legacy(s['legacy'], s['data'], s['new_target'], s['candidate'], s['destination'],
        trusted_keys=s['trust'], expected_observation_sha256=baseline['observation_sha256'],
        confirm_legacy_closed=True, **kwargs)


def verify(s):
    return module.verify_preparation(s['destination'], s['legacy'], s['data'], s['new_target'], trusted_keys=s['trust'])


def files(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file()}


def test_inspection_is_readonly_and_layout_is_not_publisher_identity(legacy):
    s = legacy; old = files(s['legacy']); data = files(s['data']); parent = set(s['legacy'].parent.iterdir())
    value = observe(s)
    assert value['read_only'] and value['classification'] == 'explicit_flat_executable_unverified'
    assert not value['install_allowed'] and not value['legacy_process_shutdown_verified']
    assert {'legacy_publisher_identity_unverified', 'legacy_data_contract_unknown'} <= set(value['blockers'])
    assert files(s['legacy']) == old and files(s['data']) == data and set(s['legacy'].parent.iterdir()) == parent
    assert 'synthetic-never-export' not in json.dumps(value)
    assert 'original' not in json.dumps(value)


def test_prepare_and_verify_retains_originals_and_sanitizes_archive(legacy):
    s = legacy; old = files(s['legacy']); data = files(s['data'])
    value = prepare(s)
    assert value['state'] == 'prepared' and not value['install_allowed'] and not value['data_modified']
    assert not value['migration_performed'] and value['original_program_retained'] and value['original_data_retained']
    assert files(s['legacy']) == old and files(s['data']) == data and not s['new_target'].exists()
    raw = (s['destination'] / 'preparation.json').read_text(encoding='utf-8')
    assert 'synthetic-never-export' not in raw
    with zipfile.ZipFile(s['destination'] / 'data.sfbak') as archive:
        config = archive.read('data/config.json')
        assert b'synthetic-never-export' not in config
        assert json.loads(config)['llm_api_key'] == ''
    assert verify(s)['state'] == 'prepared_recheck_passed'
    assert not verify(s)['install_allowed']
    assert not s['health']  # Neither old nor candidate program is executed.


def test_colocated_explicit_data_is_not_copied_as_program_or_replaced(legacy):
    s = legacy; nested = s['legacy'] / 'portable-data'; shutil.copytree(s['data'], nested); s['data'] = nested
    before = files(nested); exe = (s['legacy'] / 'ScholarFlow.exe').read_bytes()
    observed = observe(s)
    assert observed['observation']['data_inside_legacy'] and observed['observation']['program_file_count'] == 3
    prepare(s); assert verify(s)['state'] == 'prepared_recheck_passed'
    assert files(nested) == before and (s['legacy'] / 'ScholarFlow.exe').read_bytes() == exe
    assert not s['new_target'].exists()


@pytest.mark.parametrize('case', ['confirmation', 'stale_data', 'stale_program', 'stale_candidate', 'existing_destination', 'nested_destination', 'occupied_target'])
def test_preparation_rejects_unreviewed_or_overlapping_inputs_without_changes(legacy, case):
    s = legacy; observed = observe(s)
    confirm = True
    if case == 'confirmation': confirm = 1  # Strict bool, not generic truthiness.
    elif case == 'stale_data': (s['data'] / 'paper.txt').write_text('new source')
    elif case == 'stale_program': (s['legacy'] / 'ScholarFlow.exe').write_bytes(b'new binary')
    elif case == 'stale_candidate': s['candidate'] = s['create']('another-signed', 3)
    elif case == 'existing_destination': s['destination'].mkdir(); (s['destination'] / 'keep').write_text('preserve')
    elif case == 'nested_destination': s['destination'] = s['legacy'] / 'bad-preparation'
    else: s['new_target'].mkdir(); (s['new_target'] / 'keep').write_text('preserve')
    old, data = files(s['legacy']), files(s['data'])
    with pytest.raises(ReleaseError):
        module.prepare_legacy(s['legacy'], s['data'], s['new_target'], s['candidate'], s['destination'],
            trusted_keys=s['trust'], expected_observation_sha256=observed['observation_sha256'], confirm_legacy_closed=confirm)
    assert files(s['legacy']) == old and files(s['data']) == data


def test_real_runtime_lock_and_database_writer_prevent_preparation(legacy):
    s = legacy
    with module._exclusive(s['data'] / '.runtime.lock'):
        assert 'runtime_owner_active_or_unavailable' in observe(s)['blockers']
        with pytest.raises(ReleaseError, match='占用'): prepare(s)
    with closing(sqlite3.connect(s['data'] / 'scholarflow.db')) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError): prepare(s)
        writer.rollback()
    assert not s['destination'].exists() and not s['new_target'].exists()


@pytest.mark.parametrize('case', ['active', 'restore_pending', 'barrier'])
def test_live_or_unrecovered_data_is_not_prepared(legacy, case):
    s = legacy
    if case == 'active':
        with closing(sqlite3.connect(s['data'] / 'scholarflow.db')) as connection:
            connection.execute("INSERT INTO runtime_runs VALUES ('running')"); connection.commit()
    elif case == 'restore_pending':
        path = module.data_backup.recovery_root(s['data']); path.mkdir(); (path / 'pending.json').write_text('{}')
    else: module.barrier_path(s['data']).write_text('{}')
    with pytest.raises(ReleaseError, match='活动/排队'): prepare(s)
    assert not s['destination'].exists()


def test_schema_difference_stays_blocked_even_after_successful_backup(legacy):
    s = legacy
    with closing(sqlite3.connect(s['data'] / 'scholarflow.db')) as connection:
        connection.execute('CREATE TABLE legacy_extra(value TEXT)'); connection.commit()
    assert 'reviewed_schema_migration_required' in observe(s)['blockers']
    result = prepare(s)
    assert 'reviewed_schema_migration_required' in result['blockers'] and not result['install_allowed']
    assert not verify(s)['install_allowed']
    assert not s['new_target'].exists()


def test_portable_backup_comparison_excludes_database_credentials_without_changing_originals(legacy):
    s = legacy
    with closing(sqlite3.connect(s['data'] / 'scholarflow.db')) as connection:
        connection.executescript("CREATE TABLE model_credentials(id TEXT, protected_value TEXT); "
            "INSERT INTO model_credentials VALUES ('local-id','synthetic-protected-never-export'); "
            "CREATE TABLE model_profiles(credential_ref TEXT, enabled INTEGER, version INTEGER); "
            "INSERT INTO model_profiles VALUES ('local-id',1,7);")
    before = (s['data'] / 'scholarflow.db').read_bytes()
    prepare(s); assert verify(s)['state'] == 'prepared_recheck_passed'
    assert (s['data'] / 'scholarflow.db').read_bytes() == before
    with zipfile.ZipFile(s['destination'] / 'data.sfbak') as archive:
        raw = archive.read('data/scholarflow.db')
        assert b'synthetic-protected-never-export' not in raw


def test_backup_io_failure_retains_review_record_and_original_program(legacy, monkeypatch):
    s = legacy; before, data = files(s['legacy']), files(s['data'])
    def fail(*args, **kwargs): raise OSError('controlled archive write failure')
    monkeypatch.setattr(module.data_backup, 'create_backup', fail)
    with pytest.raises(OSError, match='controlled'): prepare(s)
    assert json.loads((s['destination'] / 'preparation.json').read_text(encoding='utf-8'))['state'] == 'interrupted'
    assert files(s['legacy']) == before and files(s['data']) == data and not s['new_target'].exists()


@pytest.mark.parametrize('case', ['missing_field', 'unknown_field', 'bool_version', 'forged_rollback'])
def test_malformed_preparation_cannot_be_upgraded_to_an_authority(legacy, case):
    s = legacy; prepare(s)
    path = s['destination'] / 'preparation.json'; value = json.loads(path.read_text(encoding='utf-8'))
    if case == 'missing_field': value.pop('entrypoint')
    elif case == 'unknown_field': value['execute'] = True
    elif case == 'bool_version': value['version'] = True
    else: value['rollback']['schema_rollback_available'] = True
    value['preparation_sha256'] = digest({k: v for k, v in value.items() if k != 'preparation_sha256'})
    atomic_write_json(path, value)
    with pytest.raises(ReleaseError, match='准备记录未完成'): verify(s)
    assert not s['new_target'].exists()


def test_modified_archive_and_rehashed_unsigned_receipt_cannot_hide_stale_source_file(legacy):
    s = legacy; prepare(s)
    path = s['destination'] / 'data.sfbak'
    with zipfile.ZipFile(path) as archive:
        records = {name: archive.read(name) for name in archive.namelist()}
    records['data/paper.txt'] = b'validly hashed, wrong source version'
    manifest = json.loads(records['manifest.json'])
    for record in manifest['files']:
        if record['path'] == 'paper.txt':
            record.update(size=len(records['data/paper.txt']), sha256=hashlib.sha256(records['data/paper.txt']).hexdigest())
    records['manifest.json'] = json.dumps(manifest).encode()
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        for name, raw in records.items(): archive.writestr(name, raw)
    path.write_bytes(content.getvalue())
    receipt_path = s['destination'] / 'preparation.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    receipt['backup_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt['preparation_sha256'] = digest({k: v for k, v in receipt.items() if k != 'preparation_sha256'})
    atomic_write_json(receipt_path, receipt)
    with pytest.raises(ReleaseError, match='备份资料或配置'): verify(s)
    assert not s['new_target'].exists()


def test_verification_refuses_a_record_copied_inside_original_data(legacy):
    s = legacy; prepare(s)
    nested = s['data'] / 'copied-preparation'; shutil.copytree(s['destination'], nested)
    s['destination'] = nested; before = files(s['data'])
    with pytest.raises(ReleaseError, match='必须位于旧程序'): verify(s)
    assert files(s['data']) == before


def test_closed_crash_wal_is_preserved_and_preparation_immediately_rechecks(legacy):
    s = legacy
    code = """import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0')
c.execute("INSERT INTO samples VALUES ('committed-before-real-exit')");c.commit();os._exit(0)
"""
    result = subprocess.run([sys.executable, '-c', code, str(s['data'] / 'scholarflow.db')], capture_output=True, timeout=20)
    assert result.returncode == 0
    database = s['data'] / 'scholarflow.db'; wal = database.with_name(database.name + '-wal')
    original_db, original_wal = database.read_bytes(), wal.read_bytes()
    prepare(s)
    assert database.read_bytes() == original_db
    assert wal.read_bytes() == original_wal
    assert verify(s)['state'] == 'prepared_recheck_passed'
    assert database.read_bytes() == original_db and wal.read_bytes() == original_wal


def test_runtime_without_close_checkpoint_control_refuses_preparation(legacy, monkeypatch):
    s = legacy; old, data = files(s['legacy']), files(s['data'])
    monkeypatch.delattr(module.sqlite3, 'SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE')
    with pytest.raises(ReleaseError, match='保留原WAL'): prepare(s)
    assert files(s['legacy']) == old and files(s['data']) == data and not s['destination'].exists()


@pytest.mark.parametrize('case', ['data_recovery', 'target_updates'])
def test_cli_never_bootstraps_publisher_trust_from_recovery_or_update_records(legacy, case):
    s = legacy
    folder = (module.data_backup.recovery_root(s['data']) if case == 'data_recovery' else
        s['new_target'].parent / ('.' + s['new_target'].name + '-updates'))
    folder.mkdir(); trust = folder / 'trust.json'; trust.write_text(json.dumps(s['trust']))
    result = subprocess.run([sys.executable, str(ROOT / 'desktop/install_tool.py'), 'inspect-legacy', str(s['candidate']),
        '--legacy', str(s['legacy']), '--data', str(s['data']), '--target', str(s['new_target']), '--trusted-keys', str(trust)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and '信任库必须' in result.stderr
    assert not s['destination'].exists() and not s['new_target'].exists()


@pytest.mark.parametrize('boundary', ['candidate_copied', 'backup_created', 'before_prepared'])
def test_partial_preparation_is_retained_but_never_install_authority(legacy, monkeypatch, boundary):
    s = legacy; old, data = files(s['legacy']), files(s['data'])
    class PowerLoss(BaseException): pass
    def crash(name):
        if name == boundary: raise PowerLoss()
    monkeypatch.setattr(module, '_checkpoint', crash)
    with pytest.raises(PowerLoss): prepare(s)
    assert s['destination'].exists()
    with pytest.raises(ReleaseError, match='准备记录未完成'): verify(s)
    assert files(s['legacy']) == old and files(s['data']) == data and not s['new_target'].exists()


def test_source_edit_during_preparation_retains_backup_without_accepting_it(legacy, monkeypatch):
    s = legacy
    def edit(name):
        if name == 'backup_created': (s['data'] / 'paper.txt').write_text('external edit')
    monkeypatch.setattr(module, '_checkpoint', edit)
    with pytest.raises(ReleaseError, match='准备期间|备份资料或配置'): prepare(s)
    assert (s['destination'] / 'data.sfbak').is_file()
    assert json.loads((s['destination'] / 'preparation.json').read_text(encoding='utf-8'))['state'] == 'interrupted'
    assert (s['data'] / 'paper.txt').read_text() == 'external edit' and not s['new_target'].exists()


@pytest.mark.parametrize('case', ['old_program', 'data', 'backup', 'candidate', 'record_authority', 'different_data'])
def test_preparation_recheck_rejects_changes_and_cannot_grant_installation(legacy, case):
    s = legacy; prepare(s)
    if case == 'old_program': (s['legacy'] / 'ScholarFlow.exe').write_bytes(b'changed')
    elif case == 'data': (s['data'] / 'paper.txt').write_text('changed')
    elif case == 'backup': (s['destination'] / 'data.sfbak').write_bytes(b'changed')
    elif case == 'candidate': (s['destination'] / 'candidate/payload/ScholarFlow.exe').write_bytes(b'changed')
    elif case == 'record_authority':
        path = s['destination'] / 'preparation.json'; value = json.loads(path.read_text(encoding='utf-8'))
        value['install_allowed'] = True
        value['preparation_sha256'] = digest({k: v for k, v in value.items() if k != 'preparation_sha256'})
        atomic_write_json(path, value)
    else:
        other = s['data'].parent / 'another-data'; shutil.copytree(s['data'], other); s['data'] = other
    with pytest.raises((ReleaseError, module.data_backup.BackupError)): verify(s)
    assert not s['new_target'].exists()


def test_cli_full_prepare_path_is_offline_and_never_executes_the_fixture(legacy):
    s = legacy; trust_path = s['legacy'].parent / 'external-trust.json'
    trust_path.write_text(json.dumps(s['trust']))
    base = [sys.executable, str(ROOT / 'desktop/install_tool.py')]
    common = ['--legacy', str(s['legacy']), '--data', str(s['data']), '--target', str(s['new_target']), '--trusted-keys', str(trust_path)]
    first = subprocess.run(base + ['inspect-legacy', str(s['candidate'])] + common, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    observed = json.loads(first.stdout)
    second = subprocess.run(base + ['prepare-legacy-adoption', str(s['candidate'])] + common +
        ['--destination', str(s['destination']), '--observation-sha256', observed['observation_sha256'], '--confirm-legacy-closed'], capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr
    assert not json.loads(second.stdout)['install_allowed']
    third = subprocess.run(base + ['verify-legacy-preparation', '--preparation', str(s['destination'])] + common, capture_output=True, text=True, timeout=30)
    assert third.returncode == 0, third.stderr
    assert json.loads(third.stdout)['state'] == 'prepared_recheck_passed'
    assert not s['new_target'].exists() and 'synthetic-never-export' not in first.stdout + second.stdout + third.stdout
