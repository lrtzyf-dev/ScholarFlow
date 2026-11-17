"""SQLite schema evidence and conservative, read-only upgrade preflight."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from .release_files import digest, no_link
from .release_info import ReleaseError
from .release_database import observation_database


def readonly(database):
    database = no_link(database)
    if not database.is_file():
        raise ReleaseError('预检数据库不存在')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def schema_snapshot(database):
    with closing(readonly(database)) as conn:
        conn.execute('BEGIN')
        integrity = conn.execute('PRAGMA quick_check').fetchall()
        if integrity != [('ok',)]:
            raise ReleaseError('数据库完整性检查失败')
        rows = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        objects = [{'type': t, 'name': n, 'table': table, 'sql': sql} for t, n, table, sql in rows]
        # sqlite_schema stores defaults, CHECK/UNIQUE constraints, FK declarations,
        # indexes, triggers and FTS definitions; no user rows are exported.
        body = {'format': 'scholarflow-sqlite-schema', 'version': 1, 'objects': objects}
        return {**body, 'sha256': digest(body)}


def validate_schema(value):
    if not isinstance(value, dict) or set(value) != {'format', 'version', 'objects', 'sha256'} or value['format'] != 'scholarflow-sqlite-schema' or value['version'] != 1:
        raise ReleaseError('目标数据结构清单无效')
    body = {k: value[k] for k in ('format', 'version', 'objects')}
    if not isinstance(value['objects'], list) or value['sha256'] != digest(body):
        raise ReleaseError('目标数据结构指纹不匹配')
    return value


def create_target_schema(root):
    # A stub config prevents even loading the user's .env or actual DATA_DIR.
    # init_db runs the real registration/migration code in a disposable process.
    code = '''
import sys,types,pathlib
folder=pathlib.Path(sys.argv[1])
module=types.ModuleType('app.config')
module.DB_PATH=folder/'schema.db';module.DATA_DIR=folder
sys.modules['app.config']=module
from app.db import init_db,engine
init_db();engine.dispose()
from app.release_schema import schema_snapshot
from app.release_info import canonical_json
pathlib.Path(sys.argv[2]).write_bytes(canonical_json(schema_snapshot(module.DB_PATH)))
'''
    with tempfile.TemporaryDirectory(prefix='scholarflow-schema-') as temporary:
        output = Path(temporary) / 'schema.json'
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
        env.update(PYTHONUTF8='1', DATA_DIR=temporary, LLM_API_KEY='', EMBEDDING_API_KEY='')
        result = subprocess.run([sys.executable, '-c', code, temporary, str(output)], cwd=Path(root) / 'backend', env=env,
                                capture_output=True, timeout=90)
        if result.returncode:
            # Do not expose arbitrary subprocess output in a public report.
            raise ReleaseError('隔离数据库结构生成失败，请检查模型注册及迁移代码')
        return validate_schema(json.loads(output.read_text(encoding='utf-8')))


@contextmanager
def runtime_lock_probe(directory):
    """Observe an existing owner lock; never creates a lock/token/descriptor."""
    path = no_link(Path(directory) / '.runtime.lock')
    if not path.exists():
        yield 'not_present'
        return
    stream = path.open('r+b')
    locked = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        yield 'free_during_probe' if locked else 'in_use_or_unavailable'
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def pending_runs(database):
    # Counts only: avoid emitting question text, credentials or checkpoint bodies.
    counts = {}
    with closing(readonly(database)) as conn:
        conn.execute('BEGIN')
        for table in ('runtime_runs', 'run_attempts', 'foreground_runs', 'tasks'):
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if 'status' in columns:
                counts[table] = {str(status): count for status, count in conn.execute(f'SELECT status,COUNT(*) FROM "{table}" GROUP BY status')}
    return counts


def logical_database_digest(connection):
    hasher = hashlib.sha256()
    for statement in connection.iterdump():
        raw = statement.encode('utf-8')
        hasher.update(len(raw).to_bytes(8, 'big'))
        hasher.update(raw)
    return hasher.hexdigest()


def compatibility_preflight(database, target_schema, target_contract, *, direction='upgrade', current_contract=None, backup=None):
    if direction not in ('upgrade', 'rollback'):
        raise ReleaseError('预检方向无效')
    target_schema = validate_schema(target_schema)
    # The whole preflight observes one checked external DB+WAL version. Even a
    # mode=ro source connection can create files beside the user's original.
    with runtime_lock_probe(Path(database).parent) as owner:
        with observation_database(database) as snapshot:
            return _compatibility_snapshot(snapshot, target_schema, target_contract,
                direction=direction, current_contract=current_contract, backup=backup, owner=owner)


def _compatibility_snapshot(database, target_schema, target_contract, *, direction, current_contract, backup, owner):
    current = schema_snapshot(database)
    blockers = []
    if owner == 'in_use_or_unavailable':
        blockers.append('runtime_owner_active')
    counts = pending_runs(database)
    active_states = {'running', 'queued', 'pending', 'starting', 'stopping', 'resuming', 'cancelling', 'dispatching'}
    if any(n for rows in counts.values() for status, n in rows.items() if status in active_states):
        blockers.append('active_or_queued_tasks')
    if current['sha256'] != target_schema['sha256']:
        blockers.append('schema_migration_rehearsal_required' if direction == 'upgrade' else 'rollback_schema_incompatible')
    if current_contract is None:
        blockers.append('current_data_contract_unknown')
    elif (current_contract.get('generation') != target_contract.get('generation') or
          current_contract.get('checkpoint_protocol') != target_contract.get('checkpoint_protocol')):
        blockers.append('data_or_checkpoint_contract_mismatch')
    backup_info = {'status': 'not_provided', 'matches_database': False}
    if backup is None:
        blockers.append('verified_full_backup_required')
    else:
        # Full archive verification including DB and credential sanitation in a
        # disposable new directory. No replacement of the user's DATA_DIR.
        from .data_backup import verify_archive, BackupError
        try:
            with tempfile.TemporaryDirectory(prefix='scholarflow-release-backup-') as temporary:
                staged = Path(temporary) / 'verified'
                manifest = verify_archive(no_link(backup), extract_to=staged)
                archived = staged / manifest['database']
                # A backup with the same schema but stale rows cannot authorize
                # replacement. Compare a consistent logical snapshot, not WAL bytes.
                with closing(readonly(database)) as a, closing(readonly(archived)) as b:
                    a.execute('BEGIN');b.execute('BEGIN')
                    current_hash = logical_database_digest(a)
                    backup_hash = logical_database_digest(b)
                matches = current_hash == backup_hash
                backup_info = {'status': 'verified', 'matches_database': matches, 'id': manifest.get('id'),
                               'external_project_files_included': manifest.get('external_project_files_included', False)}
                if not matches:
                    blockers.append('backup_database_differs')
        except (BackupError, OSError, KeyError, sqlite3.Error):
            backup_info = {'status': 'invalid', 'matches_database': False}
            blockers.append('backup_invalid')
    return {'direction': direction, 'eligible_for_install_recheck': not blockers, 'installation_performed': False,
            'exclusive_lock_required_at_install': True, 'runtime_owner': owner, 'task_counts': counts,
            'schema_equal': current['sha256'] == target_schema['sha256'], 'current_schema_sha256': current['sha256'],
            'target_schema_sha256': target_schema['sha256'], 'backup': backup_info, 'blockers': blockers,
            'scope': 'read_only_preflight; file backup freshness and installation must be rechecked under exclusive lock'}
