"""Observe and prepare historical flat installs without adopting or executing them.

A filename/layout is not publisher identity. Preparation creates a verified data
archive and signed candidate copy outside the old installation, then stops. It
never authorizes replacement, schema migration, shortcut changes or rollback.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from . import data_backup
from .atomic_files import atomic_write_json
from .install_barrier import barrier_path
from .release_files import digest, file_record, no_link, relative_path, validate_records
from .release_info import ReleaseError, read_json
from .release_install import ACTIVE, _exclusive, _files, _data_fingerprint, verify_candidate
from .release_schema import logical_database_digest, pending_runs, readonly, runtime_lock_probe, schema_snapshot

FORMAT = 'scholarflow-legacy-adoption-preparation'
BASE_BLOCKERS = ['legacy_publisher_identity_unverified', 'legacy_data_contract_unknown',
                 'legacy_process_shutdown_unverified', 'adoption_transaction_not_implemented']
ROLLBACK = {'mode': 'original_left_in_place', 'automatic_rollback_available': False,
            'schema_rollback_available': False, 'legacy_publisher_verified': False, 'recheck_required': True}
PREPARATION_FIELDS = {'format', 'version', 'id', 'state', 'observation', 'observation_sha256',
    'entrypoint', 'install_allowed', 'migration_performed', 'legacy_process_shutdown_verified',
    'user_confirmed_legacy_closed', 'blockers', 'original_program_retained', 'original_data_retained',
    'rollback', 'backup_sha256', 'credential_portability', 'external_project_files_included', 'preparation_sha256'}


def _path(value):
    return no_link(no_link(value).resolve())


def _separate(a, b):
    return a != b and not a.is_relative_to(b) and not b.is_relative_to(a)


def _inputs(legacy, data, target, candidate):
    legacy, data, target, candidate = map(_path, (legacy, data, target, candidate))
    if not legacy.is_dir() or not data.is_dir() or not target.parent.is_dir():
        raise ReleaseError('旧程序、明确的数据目录及新目标父目录必须存在')
    if legacy == data or legacy.is_relative_to(data):
        raise ReleaseError('数据目录与程序文件无法分离识别，不能准备接管')
    if target.exists():
        raise ReleaseError('接管的新目标必须尚不存在；不会覆盖现有目录')
    if not all(_separate(target, p) for p in (legacy, data, candidate)) or not _separate(candidate, legacy) or not _separate(candidate, data):
        raise ReleaseError('新目标与候选必须位于旧程序和数据目录之外')
    recovery = _path(data_backup.recovery_root(data))
    if not _separate(target, recovery) or not _separate(candidate, recovery):
        raise ReleaseError('新目标和候选不能占用应用备份恢复目录')
    return legacy, data, target, candidate


def _program(legacy, data):
    rows = []
    for directory, directories, files in os.walk(legacy, followlinks=False):
        root = Path(directory)
        kept = []
        for name in directories:
            path = no_link(root / name)
            if path not in {data, _path(data_backup.recovery_root(data))}:
                kept.append(name)
        directories[:] = kept
        for name in files:
            path = no_link(root / name)
            rows.append(file_record(legacy, path))
    return validate_records(sorted(rows, key=lambda row: row['path']))


def _entry(legacy, entrypoint):
    parts = relative_path(entrypoint).parts
    if len(parts) != 1 or not entrypoint.lower().endswith('.exe'):
        raise ReleaseError('旧平铺入口必须是明确指定的根目录 EXE 文件')
    if (legacy / 'install-manifest.json').exists() or (legacy / 'payload').exists():
        raise ReleaseError('已有候选封套或混合目录，不能按旧平铺程序识别')
    path = no_link(legacy / entrypoint)
    if not path.is_file():
        raise ReleaseError('明确指定的旧平铺程序入口不存在')
    return file_record(legacy, path)


def _database(data):
    path = no_link(data / 'scholarflow.db')
    if not path.is_file():
        raise ReleaseError('旧数据必须包含明确的 scholarflow.db；不会创建或猜测数据库')
    return path


def _database_files(database):
    journal = no_link(database.with_name(database.name + '-journal'))
    if journal.exists() and journal.stat().st_size:
        raise ReleaseError('旧数据库仍有恢复日志，请先由原软件完成恢复再检查')
    paths = [database]
    wal = no_link(database.with_name(database.name + '-wal'))
    if wal.exists(): paths.append(wal)
    return [file_record(database.parent, path) for path in paths]


@contextmanager
def _observation_database(database):
    """SQLite may create SHM/WAL even for mode=ro; never open live input here.

    Copy both DB and any existing WAL, checking exact source and copied bytes.
    SQLite rebuilds its indexes only inside this fresh temporary directory. A
    changing source fails closed rather than discarding committed WAL frames.
    """
    before = _database_files(database)
    with tempfile.TemporaryDirectory(prefix='legacy-observation-') as temporary:
        root = Path(temporary)
        for record in before:
            source = no_link(database.parent / record['path'])
            shutil.copyfile(source, root / record['path'])
            if file_record(root, root / record['path']) != record:
                raise ReleaseError('旧数据库快照复制不一致，请关闭原软件后重试')
        if _database_files(database) != before:
            raise ReleaseError('旧数据库或WAL在检查期间变化，未接受混合版本快照')
        yield root / database.name
        if _database_files(database) != before:
            raise ReleaseError('旧数据库或WAL在检查期间变化，未接受混合版本快照')


def _observe(legacy, data, target, candidate, trusted_keys, entrypoint, runtime_owner=None):
    legacy, data, target, candidate = _inputs(legacy, data, target, candidate)
    entry = _entry(legacy, entrypoint)
    manifest = verify_candidate(candidate, trusted_keys)
    database = _database(data)
    with _observation_database(database) as snapshot:
        schema = schema_snapshot(snapshot)
        with closing(readonly(snapshot)) as connection:
            connection.execute('BEGIN')
            database_sha = logical_database_digest(connection)
        if runtime_owner is None:
            with runtime_lock_probe(data) as owner:
                counts = pending_runs(snapshot)
        else:
            owner = runtime_owner
            counts = pending_runs(snapshot)
    records = _program(legacy, data)
    blockers = list(BASE_BLOCKERS)
    if schema['sha256'] != manifest['target_schema']['sha256']:
        blockers.append('reviewed_schema_migration_required')
    if owner == 'in_use_or_unavailable': blockers.append('runtime_owner_active_or_unavailable')
    if any(number for rows in counts.values() for status, number in rows.items() if status in ACTIVE):
        blockers.append('active_or_queued_tasks')
    if barrier_path(data).exists() or (data_backup.recovery_root(data) / 'pending.json').exists():
        blockers.append('data_recovery_pending')
    # Rows and configuration values never enter the report. Unknown old status
    # values are counted without exporting arbitrary strings from user rows.
    known = ACTIVE | {'completed', 'interrupted', 'failed', 'cancelled', 'partial', 'ended', 'done', 'stopped', 'blocked'}
    public_counts = {table: {status: number for status, number in rows.items() if status in known}
                     for table, rows in counts.items()}
    unknown_counts = {table: sum(number for status, number in rows.items() if status not in known)
                      for table, rows in counts.items()}
    observed = {'legacy': str(legacy), 'data': str(data), 'proposed_target': str(target),
        'entrypoint': entry, 'program_sha256': digest(records), 'program_file_count': len(records),
        'data_sha256': _data_fingerprint(data), 'database_logical_sha256': database_sha,
        'current_schema_sha256': schema['sha256'], 'candidate_id': manifest['candidate_id'],
        'target_schema_sha256': manifest['target_schema']['sha256'],
        'candidate_data_contract': manifest['definition']['data_contract'],
        'data_inside_legacy': data.is_relative_to(legacy)}
    return {'format': 'scholarflow-legacy-observation', 'version': 1,
        'classification': 'explicit_flat_executable_unverified', 'observation': observed,
        'observation_sha256': digest(observed), 'runtime_owner': owner,
        'legacy_process_shutdown_verified': False, 'task_counts': public_counts,
        'other_status_counts': unknown_counts, 'blockers': blockers,
        'read_only': True, 'install_allowed': False, 'migration_performed': False,
        'public_release_ready': False}


def inspect_legacy(legacy, data, target, candidate, *, trusted_keys, entrypoint='ScholarFlow.exe'):
    """No input files/locks/directories created; no old executable/config loaded."""
    return _observe(legacy, data, target, candidate, trusted_keys, entrypoint)


def _checkpoint(name):
    """Test-only injection seam, never supplied by candidates or CLI input."""


def _verify_portable_database(source, archived, directory):
    # Managed credentials are removed from exported DB rows. Compare both
    # databases after applying that same production export transform rather
    # than incorrectly requiring a sanitized archive to equal secret originals.
    expected = directory / 'expected-portable.sqlite3'
    with _observation_database(source) as snapshot:
        data_backup._database_snapshot(snapshot, expected)
    data_backup.credentials.sanitized_database(expected)
    with closing(readonly(expected)) as a, closing(readonly(archived)) as b:
        if logical_database_digest(a) != logical_database_digest(b):
            raise ReleaseError('备份与本次旧数据库的凭证排除快照不符')


def _verify_archive_matches_source(data, manifest, extracted, directory):
    if manifest.get('database') != 'scholarflow.db':
        raise ReleaseError('备份的数据库名称与明确指定的旧数据不符')
    inventory = data_backup._inventory(data, 'scholarflow.db')
    expected = {name for name in inventory if not data_backup.credentials.excluded_managed_path(name)}
    entries = {entry['path']: entry for entry in manifest['files']}
    if set(entries) != expected:
        raise ReleaseError('备份文件集合与本次旧数据不符')
    _verify_portable_database(_database(data), extracted / 'scholarflow.db', directory)
    for name, entry in entries.items():
        if name == 'scholarflow.db': continue
        if data_backup.credentials.is_managed_configuration(name):
            with no_link(data / name).open('rb') as stream:
                raw = stream.read(data_backup.credentials.MAX_CONFIGURATION_BYTES + 1)
            portable = data_backup.credentials.sanitized_configuration(name, raw)
            size, sha = len(portable), hashlib.sha256(portable).hexdigest()
        else:
            record = file_record(data, data / name)
            size, sha = record['size'], record['sha256']
        if (entry['size'], entry['sha256']) != (size, sha):
            raise ReleaseError('备份资料或配置与本次旧数据的凭证排除版本不符')


def prepare_legacy(legacy, data, target, candidate, destination, *, trusted_keys,
                   expected_observation_sha256, confirm_legacy_closed=False, entrypoint='ScholarFlow.exe'):
    if confirm_legacy_closed is not True:
        raise ReleaseError('请先关闭旧程序并明确记录关闭确认；结构识别不能证明旧进程已退出')
    legacy, data, target, candidate = _inputs(legacy, data, target, candidate)
    destination = _path(destination)
    if destination.exists() or not destination.parent.is_dir() or not all(
            _separate(destination, p) for p in (legacy, data, target, candidate, _path(data_backup.recovery_root(data)))):
        raise ReleaseError('准备目录必须是外部全新目录，不能覆盖程序、数据、候选或新目标')
    lock = destination.parent / ('.legacy-prepare-' + digest([str(legacy), str(data)])[:32] + '.lock')
    with _exclusive(lock), runtime_lock_probe(data) as owner:
        if owner == 'in_use_or_unavailable':
            raise ReleaseError('数据目录仍有运行锁占用，未准备接管')
        no_checkpoint = getattr(sqlite3, 'SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE', None)
        if no_checkpoint is None or not hasattr(sqlite3.Connection, 'setconfig') or not hasattr(sqlite3.Connection, 'getconfig'):
            raise ReleaseError('当前SQLite运行环境不能保证关闭连接时保留原WAL，未准备接管')
        with closing(sqlite3.connect(_database(data).as_uri() + '?mode=rw', uri=True, timeout=1)) as connection:
            # A crashed old process may leave committed frames only in WAL.
            # Closing our final live connection must not checkpoint those frames
            # into the retained original or invalidate its frozen observation.
            connection.setconfig(no_checkpoint, True)
            if connection.getconfig(no_checkpoint) is not True:
                raise ReleaseError('无法禁止关闭连接时写回原WAL，未准备接管')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('BEGIN IMMEDIATE')
            try:
                before = _observe(legacy, data, target, candidate, trusted_keys, entrypoint, owner)
                forbidden = {'active_or_queued_tasks', 'data_recovery_pending'}
                if forbidden.intersection(before['blockers']):
                    raise ReleaseError('仍有活动/排队任务或待恢复事务，未准备接管')
                if before['observation_sha256'] != expected_observation_sha256:
                    raise ReleaseError('旧程序、数据或候选已变化，请重新检查并审阅准备计划')
                destination.mkdir()
                preparation = {'format': FORMAT, 'version': 1, 'id': uuid4().hex, 'state': 'preparing',
                    'observation': before['observation'], 'observation_sha256': before['observation_sha256'],
                    'entrypoint': entrypoint, 'install_allowed': False, 'migration_performed': False,
                    'legacy_process_shutdown_verified': False, 'user_confirmed_legacy_closed': True,
                    'blockers': before['blockers'], 'original_program_retained': True,
                    'original_data_retained': True, 'rollback': dict(ROLLBACK)}
                atomic_write_json(destination / 'preparation.json', preparation)
                try:
                    shutil.copytree(candidate, destination / 'candidate')
                    verify_candidate(destination / 'candidate', trusted_keys)
                    for row in _files(destination / 'candidate'):
                        with (destination / 'candidate' / row['path']).open('r+b') as stream:
                            os.fsync(stream.fileno())
                    _checkpoint('candidate_copied')
                    exported = data_backup.create_backup(data, destination=destination / 'data.sfbak')
                    _checkpoint('backup_created')
                    with tempfile.TemporaryDirectory(prefix='legacy-backup-check-', dir=destination.parent) as temporary:
                        extracted = Path(temporary) / 'data'
                        backup_manifest = data_backup.verify_archive(destination / 'data.sfbak', extract_to=extracted)
                        _verify_archive_matches_source(data, backup_manifest, extracted, Path(temporary))
                    after = _observe(legacy, data, target, candidate, trusted_keys, entrypoint, owner)
                    if after['observation_sha256'] != before['observation_sha256']:
                        raise ReleaseError('准备期间旧程序或资料变化，保留备份但不批准接管')
                    preparation.update(state='prepared', backup_sha256=exported['sha256'],
                        credential_portability=backup_manifest['credential_portability'],
                        external_project_files_included=False)
                    preparation['preparation_sha256'] = digest(preparation)
                    _checkpoint('before_prepared')
                    atomic_write_json(destination / 'preparation.json', preparation)
                    return {'state': 'prepared', 'preparation_id': preparation['id'], 'directory': str(destination),
                        'preparation_sha256': preparation['preparation_sha256'],
                        'install_allowed': False, 'migration_performed': False, 'data_modified': False,
                        'legacy_process_shutdown_verified': False, 'blockers': after['blockers'],
                        'original_program_retained': True, 'original_data_retained': True,
                        'credential_portability': preparation['credential_portability'], 'public_release_ready': False}
                except Exception:
                    preparation.update(state='interrupted', install_allowed=False)
                    preparation.pop('preparation_sha256', None)
                    atomic_write_json(destination / 'preparation.json', preparation)
                    raise
            finally:
                connection.rollback()


def verify_preparation(destination, legacy, data, target, *, trusted_keys):
    """Recheck a preparation. Its unsigned record never authorizes installation."""
    destination = _path(destination)
    if not destination.is_dir() or not all(_separate(destination, p) for p in
            map(_path, (legacy, data, target, data_backup.recovery_root(data)))):
        raise ReleaseError('准备记录必须位于旧程序、数据、新目标和恢复目录之外')
    value = read_json(no_link(destination / 'preparation.json'), 256 * 1024)
    if (not isinstance(value, dict) or set(value) != PREPARATION_FIELDS or value.get('format') != FORMAT
            or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', value['id'])
            or value.get('state') != 'prepared' or value.get('install_allowed') is not False
            or value.get('migration_performed') is not False or value.get('legacy_process_shutdown_verified') is not False
            or value.get('user_confirmed_legacy_closed') is not True or value.get('rollback') != ROLLBACK
            or value.get('original_program_retained') is not True or value.get('original_data_retained') is not True
            or value.get('external_project_files_included') is not False
            or value.get('credential_portability') != data_backup.credentials.EXCLUDED_RECONFIGURE
            or not isinstance(value.get('entrypoint'), str)
            or value.get('preparation_sha256') != digest({k: v for k, v in value.items() if k != 'preparation_sha256'})):
        raise ReleaseError('准备记录未完成或不完整；不能作为安装或恢复授权')
    observed = value.get('observation')
    if not isinstance(observed, dict) or [observed.get(key) for key in ('legacy', 'data', 'proposed_target')] != list(map(str, map(_path, (legacy, data, target)))):
        raise ReleaseError('准备记录与明确指定的旧程序、数据和新目标不符')
    current = inspect_legacy(legacy, data, target, destination / 'candidate', trusted_keys=trusted_keys,
        entrypoint=value['entrypoint'])
    if current['observation_sha256'] != value['observation_sha256']:
        raise ReleaseError('准备后的旧程序、数据或候选已变化，必须重新准备')
    archive = no_link(destination / 'data.sfbak')
    if file_record(destination, archive)['sha256'] != value.get('backup_sha256'):
        raise ReleaseError('准备备份内容已变化')
    with tempfile.TemporaryDirectory(prefix='legacy-preparation-verify-', dir=destination.parent) as temporary:
        extracted = Path(temporary) / 'data'
        manifest = data_backup.verify_archive(archive, extract_to=extracted)
        _verify_archive_matches_source(_path(data), manifest, extracted, Path(temporary))
    after = inspect_legacy(legacy, data, target, destination / 'candidate', trusted_keys=trusted_keys,
        entrypoint=value['entrypoint'])
    if after['observation_sha256'] != current['observation_sha256']:
        raise ReleaseError('复核期间旧程序或资料发生变化，不能沿用准备记录')
    return {'state': 'prepared_recheck_passed', 'preparation_id': value['id'],
        'install_allowed': False, 'migration_performed': False, 'data_modified': False,
        'blockers': current['blockers'], 'legacy_process_shutdown_verified': False,
        'original_program_retained': True, 'original_data_retained': True, 'public_release_ready': False}
