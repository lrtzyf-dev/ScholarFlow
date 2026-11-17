import base64
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
import zipfile

import pytest

from app import release_manifest as release
from app import release_info
from app.release_dependencies import write_json
from app.release_files import digest, relative_path, source_inventory, validate_records
from app.release_info import ReleaseError, canonical_json
from app.release_schema import schema_snapshot, compatibility_preflight, create_target_schema

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = {'generation': 1, 'checkpoint_protocol': 1, 'rollback_policy': 'exact_schema_or_verified_compatible_snapshot'}


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("CREATE TABLE example(value TEXT NOT NULL); INSERT INTO example VALUES ('initial'); CREATE TABLE runtime_runs(status TEXT);")
    return path


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path / 'source';root.mkdir()
    (root / 'frontend').mkdir()
    value = json.loads((ROOT / 'release.json').read_text(encoding='utf-8'))
    write_json(root / 'release.json', value)
    write_json(root / 'frontend/package.json', {'version': value['version']})
    write_json(root / 'frontend/package-lock.json', {'version': value['version'], 'lockfileVersion': 3, 'packages': {'': {'version': value['version']}}})
    db = database(tmp_path / 'test-data/scholarflow.db')
    monkeypatch.setattr(release, 'create_target_schema', lambda _: schema_snapshot(db))
    def deps(_root, output, _wheelhouse):
        coverage = {'python': {'wheel_hashes_complete': False}, 'npm': {'integrity_missing_or_link': []}}
        write_json(output / 'dependency-coverage.json', coverage)
        write_json(output / 'sbom.cdx.json', {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1, 'components': []})
        return coverage
    monkeypatch.setattr(release, 'collect_dependencies', deps)
    output = tmp_path / 'prepared'
    result = release.prepare(root, output)
    return root, output, db, result


def test_single_version_and_windows_resource():
    value = release.check_versions(ROOT)
    text = release.windows_version({**value, 'version': '1.2.3', 'build_number': 45})
    assert "filevers=(1, 2, 3, 45)" in text
    assert "prodvers=(1, 2, 3, 45)" in text
    assert 'ProductVersion' in text
    assert release_info.VERSION == value['version']


def test_prepared_integrity_is_not_publisher_or_installer(prepared):
    root, output, _, manifest = prepared
    result = release.verify(output, source_root=root)
    assert result['source_verified'] and result['integrity'] == 'verified'
    assert result['signature'] == {'status': 'unsigned', 'trusted': False}
    assert not result['public_release_ready']
    with pytest.raises(ReleaseError, match='公开发行'):
        release.verify(output, require_public=True)
    with pytest.raises(ReleaseError, match='不存在的新目录'):
        release.prepare(root, output)


def test_integrity_detects_tamper_and_extra_hidden_files(prepared):
    _, output, _, _ = prepared
    (output / '.unexpected.exe').write_bytes(b'extra')
    with pytest.raises(ReleaseError, match='清单外'):
        release.verify(output)
    (output / '.unexpected.exe').unlink()
    (output / 'windows-version.txt').write_text('tampered')
    with pytest.raises(ReleaseError, match='完整性'):
        release.verify(output)


def test_source_change_requires_new_prepare(prepared):
    root, output, _, _ = prepared
    (root / 'frontend/src').mkdir()
    (root / 'frontend/src/new.ts').write_text('changed')
    with pytest.raises(ReleaseError, match='源码'):
        release.verify(output, source_root=root)


def test_secret_paths_not_in_source_inventory(prepared):
    root, _, _, _ = prepared
    for name in ('backend/.env', 'backend/data/config.json', 'verification/private.json', 'frontend/.npmrc'):
        path=root/name;path.parent.mkdir(parents=True, exist_ok=True);path.write_text('synthetic-secret')
    records = source_inventory(root)
    assert not any(r['path'].endswith(('.env', '.npmrc', 'config.json', 'private.json')) for r in records)


@pytest.mark.parametrize('path', ['../outside', '/abs', 'C:/abs', 'folder/../outside', 'A\\b', 'NUL.txt', 'a:x', 'a//b', 'a.'])
def test_reject_unsafe_manifest_paths(path):
    with pytest.raises(ReleaseError): relative_path(path)


def test_reject_case_duplicate_and_symlink(prepared):
    root, output, _, _ = prepared
    with pytest.raises(ReleaseError):
        validate_records([{'path': p, 'size': 0, 'sha256': '0'*64} for p in ('A.txt', 'a.txt')])
    try:
        (output / 'linked').symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip('Windows account cannot create physical symlink')
    with pytest.raises(ReleaseError, match='链接|联接'):
        release.verify(output)


def test_frontend_receipt_requires_matching_inputs(prepared, tmp_path):
    root, _, _, _ = prepared
    dist=root/'frontend/dist';dist.mkdir()
    write_json(dist/'release-build.json', {'format':'scholarflow-frontend-inputs','version':1,'files':source_inventory(root)})
    manifest=release.prepare(root,tmp_path/'current')
    assert manifest['coverage']['frontend_provenance_verified']
    (root/'frontend/index.html').write_text('changed')
    with pytest.raises(ReleaseError, match='构建已过期'):
        release.prepare(root,tmp_path/'stale')


def test_version_mismatch_rejected(prepared):
    root, _, _, _=prepared
    write_json(root/'frontend/package.json',{'version':'0.2.0'})
    with pytest.raises(ReleaseError,match='不一致'):release.check_versions(root)


def test_signature_needs_external_trust_and_rejects_forgery(prepared):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    _, output, _, manifest = prepared
    private=Ed25519PrivateKey.generate()
    pub=base64.b64encode(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
    write_json(output/'release-signature.json',{'algorithm':'Ed25519','key_id':'test-publisher',
        'signature':base64.b64encode(private.sign(canonical_json(manifest))).decode()})
    assert release.verify(output)['signature']['status']=='untrusted_key'
    assert release.verify(output,trusted_keys={'test-publisher':pub})['signature']['trusted']
    forged=copy.deepcopy(manifest);forged['created_at']='2026-01-01T00:00:00Z';forged['build_id']=digest({k:v for k,v in forged.items() if k!='build_id'})
    write_json(output/'release-manifest.json',forged)
    with pytest.raises(ReleaseError,match='签名验证失败'):
        release.verify(output,trusted_keys={'test-publisher':pub})


def test_schema_fingerprint_detects_constraints_without_reading_rows(tmp_path):
    a=database(tmp_path/'a.db');b=database(tmp_path/'b.db')
    before=schema_snapshot(a)
    with closing(sqlite3.connect(a)) as db:
        db.execute("UPDATE example SET value='modified'");db.commit()
    assert schema_snapshot(a)==before
    with closing(sqlite3.connect(b)) as db:
        db.execute('CREATE UNIQUE INDEX example_unique ON example(value)');db.commit()
    assert schema_snapshot(a)['sha256']!=schema_snapshot(b)['sha256']


def test_preflight_backup_freshness_and_no_mutation(prepared):
    from app.data_backup import create_backup
    _, _, db, _=prepared
    backup=create_backup(db.parent)
    before=db.read_bytes()
    result=compatibility_preflight(db,schema_snapshot(db),CONTRACT,current_contract=CONTRACT,backup=backup['path'])
    assert result['eligible_for_install_recheck'] and not result['installation_performed']
    assert result['backup']['matches_database']
    assert db.read_bytes()==before
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE example SET value='newer'");conn.commit()
    assert 'backup_database_differs' in compatibility_preflight(db,schema_snapshot(db),CONTRACT,current_contract=CONTRACT,backup=backup['path'])['blockers']


def test_preflight_active_contract_and_schema_fail_closed(prepared,tmp_path):
    _, _, db, _=prepared
    original=schema_snapshot(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO runtime_runs VALUES('running')")
        conn.execute('CREATE TABLE future(value TEXT)');conn.commit()
    result=compatibility_preflight(db,original,CONTRACT,direction='rollback')
    assert {'active_or_queued_tasks','rollback_schema_incompatible','current_data_contract_unknown','verified_full_backup_required'}<=set(result['blockers'])
    contract={**CONTRACT,'checkpoint_protocol':2}
    result=compatibility_preflight(db,schema_snapshot(db),CONTRACT,current_contract=contract)
    assert 'data_or_checkpoint_contract_mismatch' in result['blockers']


def test_preflight_holds_no_runtime_ownership(prepared):
    from app.runtime_owner import RuntimeOwner
    _, _, db, _=prepared
    owner=RuntimeOwner(db.parent)
    owner._acquire('service')
    try:
        before=(db.parent/'.runtime.json').read_bytes()
        result=compatibility_preflight(db,schema_snapshot(db),CONTRACT,current_contract=CONTRACT)
        assert 'runtime_owner_active' in result['blockers']
        assert (db.parent/'.runtime.json').read_bytes()==before
    finally:owner.finish_shutdown(idle=True)


def test_isolated_schema_registration_covers_current_tables():
    schema=create_target_schema(ROOT)
    names={item['name'] for item in schema['objects']}
    assert {'source_library_bindings','artifact_manifests','runtime_runs','zotero_bindings'}<=names


def test_release_api_does_not_claim_signature(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.release import router
    app=FastAPI();app.include_router(router)
    with TestClient(app) as client:
        response=client.get('/api/release')
    assert response.status_code==200
    assert response.json()['version']==release_info.VERSION
    assert response.json()['publisher_signature']=='not_verified'


@pytest.mark.parametrize('cwd', ['.', 'backend'])
def test_launcher_import_without_pythonpath_or_user_state(cwd):
    script=ROOT/'desktop/launcher.py'
    code="""
import sys,runpy
module=runpy.run_path(sys.argv[1],run_name='launcher_import_probe')
assert '0.1.0' in module['APP_TITLE']
assert 'app.config' not in sys.modules
assert 'app.db' not in sys.modules
print('import-ok')
"""
    env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
    result=subprocess.run([sys.executable,'-c',code,str(script)],cwd=ROOT/cwd,env=env,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert result.stdout.strip()==b'import-ok'


def test_launcher_terminal_worker_dispatch_precedes_ui_and_data():
    code="""
import sys,types,runpy
worker=types.ModuleType('app.terminal_worker')
def probe():
    assert 'webview' not in sys.modules
    assert 'app.config' not in sys.modules
    assert 'app.db' not in sys.modules
    print('terminal-worker-ok')
worker.main=probe
sys.modules['app.terminal_worker']=worker
path=sys.argv[1];sys.argv=[path,'--terminal-worker']
runpy.run_path(path,run_name='__main__')
"""
    env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
    result=subprocess.run([sys.executable,'-c',code,str(ROOT/'desktop/launcher.py')],cwd=ROOT,env=env,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert result.stdout.strip()==b'terminal-worker-ok'


def test_wheel_lock_uses_top_level_metadata_and_exact_artifact_hash(tmp_path):
    from app.release_wheels import wheel_lock
    path=tmp_path/'example-1.0-py3-none-any.whl'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('example-1.0.dist-info/METADATA','Name: example\nVersion: 1.0\n')
        archive.writestr('example/_vendor/nested-1.0.dist-info/METADATA','Name: nested\nVersion: 1.0\n')
    lock,manifest=wheel_lock(tmp_path)
    assert 'example==1.0' in lock and '--hash=sha256:' in lock
    assert manifest['package_count']==1 and manifest['files'][0]['name']=='example'
    with zipfile.ZipFile(tmp_path/'example-2.0-py3-none-any.whl','w') as archive:
        archive.writestr('example-2.0.dist-info/METADATA','Name: example\nVersion: 2.0\n')
    with pytest.raises(ReleaseError,match='多个版本'):wheel_lock(tmp_path)


def test_wheel_lock_refuses_filename_identity_mismatch(tmp_path):
    from app.release_wheels import wheel_lock
    with zipfile.ZipFile(tmp_path/'example-1.0-py3-none-any.whl','w') as archive:
        archive.writestr('example-1.0.dist-info/METADATA','Name: different\nVersion: 1.0\n')
    with pytest.raises(ReleaseError,match='文件名与元数据'):wheel_lock(tmp_path)
