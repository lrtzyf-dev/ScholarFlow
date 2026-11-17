"""Prepare and verify local release inputs, without building or installing."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

from .release_dependencies import collect_dependencies, write_json
from .release_files import contained, digest, file_record, no_link, source_inventory, validate_records, verify_records, walk_regular
from .release_info import ReleaseError, canonical_json, read_json, validate_definition
from .release_schema import create_target_schema, validate_schema

FORMAT = 'scholarflow-release-inputs'


def check_versions(root):
    value = validate_definition(read_json(contained(root, 'release.json')))
    package = read_json(contained(root, 'frontend/package.json'))
    lock = read_json(contained(root, 'frontend/package-lock.json'))
    if package.get('version') != value['version'] or lock.get('version') != value['version'] or lock.get('packages', {}).get('', {}).get('version') != value['version']:
        raise ReleaseError('前端包版本与 release.json 不一致；先运行 sync-versions')
    return value


def windows_version(value):
    parts = tuple([int(n) for n in value['version'].split('.')] + [value['build_number']])
    full = '.'.join(map(str, parts))
    # All string literals are repr-escaped. This is generated from our validated
    # definition, never supplied by a downloaded package or eval'ed by this tool.
    return """# Generated from release.json; UTF-8 Windows version resource.
VSVersionInfo(
  ffi=FixedFileInfo(filevers=%r, prodvers=%r, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('080404b0', [
    StringStruct('ProductName', %r), StringStruct('FileDescription', %r),
    StringStruct('FileVersion', %r), StringStruct('ProductVersion', %r),
    StringStruct('OriginalFilename', 'ScholarFlow.exe')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])])
""" % (parts, parts, value['product_name'], value['product_name'], full, value['version'])


def prepare(root, output, *, wheelhouse=None):
    root, output = no_link(root), no_link(output)
    if output.exists():
        raise ReleaseError('准备目录必须是不存在的新目录，不覆盖既有构建证据')
    value = check_versions(root)
    before = source_inventory(root)
    output.mkdir(parents=True)
    write_json(output / 'release.json', value)
    write_json(output / 'source-files.json', before)
    (output / 'windows-version.txt').write_text(windows_version(value), encoding='utf-8')
    coverage = collect_dependencies(root, output, wheelhouse)
    schema = create_target_schema(root)
    write_json(output / 'schema.json', schema)
    assets = []
    dist = contained(root, 'frontend/dist')
    receipt_valid = False
    if dist.is_dir():
        assets = [file_record(root, p) for p in walk_regular(dist)]
        receipt = dist / 'release-build.json'
        if receipt.is_file():
            provenance = read_json(receipt)
            wanted = [r for r in before if r['path'].startswith('frontend/') or r['path'] == 'release.json']
            if provenance.get('format') != 'scholarflow-frontend-inputs' or provenance.get('version') != 1 or provenance.get('files') != wanted:
                raise ReleaseError('前端构建已过期，请重新运行 npm run build 后准备发行输入')
            receipt_valid = True
    write_json(output / 'frontend-files.json', assets)
    after = source_inventory(root)
    if before != after:
        raise ReleaseError('源码在准备过程中发生变化，当前目录没有发行清单；请用新目录重新准备')
    files = [file_record(output, p) for p in walk_regular(output)]
    payload = {
        'format': FORMAT, 'schema_version': 1, 'artifact_state': 'prepared_inputs',
        'product_id': value['product_id'], 'version': value['version'], 'channel': value['channel'],
        'build_number': value['build_number'], 'definition_sha256': digest(value),
        'source_sha256': digest(before), 'schema_sha256': schema['sha256'],
        'data_contract': value['data_contract'], 'files': files,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'coverage': {'frontend_built': bool(assets), 'frontend_provenance_verified': receipt_valid, 'dependencies': coverage},
        'release_blockers': ['publisher_signature_required', 'final_binary_sbom_required',
            'license_obligations_review_required', 'clean_windows_install_upgrade_rollback_not_verified',
            'real_provider_acceptance_not_attached']
            + ([] if coverage['python']['wheel_hashes_complete'] else ['python_wheel_hash_lock_incomplete'])
            + ([] if not coverage['python'].get('duplicate_distributions') else ['python_environment_conflicts'])
            + ([] if not coverage.get('source_wheel_license_issues') else ['source_wheel_license_conflict'])
            + ([] if assets else ['frontend_build_missing'])
            + ([] if receipt_valid else ['frontend_build_provenance_missing'])
            + ([] if not coverage['npm']['integrity_missing_or_link'] else ['npm_integrity_incomplete']),
    }
    payload['build_id'] = digest(payload)
    write_json(output / 'release-manifest.json', payload)
    return payload


def validate_manifest(value):
    fields = {'format','schema_version','artifact_state','product_id','version','channel','build_number',
              'definition_sha256','source_sha256','schema_sha256','data_contract','files','created_at',
              'coverage','release_blockers','build_id'}
    if not isinstance(value, dict) or set(value) != fields or value.get('format') != FORMAT or value.get('schema_version') != 1 or value.get('artifact_state') != 'prepared_inputs':
        raise ReleaseError('发行构建清单结构不受支持')
    if not isinstance(value['release_blockers'], list) or not all(isinstance(b, str) for b in value['release_blockers']) or not isinstance(value['coverage'], dict):
        raise ReleaseError('发行构建清单状态无效')
    if value.get('build_id') != digest({k: v for k, v in value.items() if k != 'build_id'}):
        raise ReleaseError('构建标识与清单内容不一致')
    validate_records(value.get('files'))
    return value


def verify_signature(folder, manifest, trusted_keys=None):
    path = contained(folder, 'release-signature.json')
    if not path.exists():
        return {'status': 'unsigned', 'trusted': False}
    signature = read_json(path, 16 * 1024)
    if not isinstance(signature, dict) or set(signature) != {'algorithm', 'key_id', 'signature'} or signature.get('algorithm') != 'Ed25519':
        raise ReleaseError('发行签名格式无效')
    trusted_keys = trusted_keys or {}
    key = trusted_keys.get(signature.get('key_id'))
    if key is None:
        return {'status': 'untrusted_key', 'trusted': False}
    # Trust is supplied externally; a package's embedded public key is ignored.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True))
        public.verify(base64.b64decode(signature['signature'], validate=True), canonical_json(manifest))
    except ImportError:
        raise ReleaseError('签名验证依赖 cryptography 未安装') from None
    except Exception:
        raise ReleaseError('发行签名验证失败') from None
    return {'status': 'verified', 'trusted': True, 'key_id': signature['key_id']}


def verify(folder, *, source_root=None, trusted_keys=None, require_public=False, check_environment=False, wheelhouse=None):
    folder = no_link(folder)
    manifest = validate_manifest(read_json(contained(folder, 'release-manifest.json')))
    verify_records(folder, manifest['files'])
    # Extra executable/evidence files cannot hide outside the hashed manifest.
    expected = {i['path'].casefold() for i in manifest['files']} | {'release-manifest.json', 'release-signature.json'}
    actual = set()
    for directory, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            no_link(Path(directory) / name)
        actual.update((Path(directory) / name).relative_to(folder).as_posix().casefold() for name in files)
    if actual - expected:
        raise ReleaseError('发行目录包含清单外文件')
    definition = validate_definition(read_json(contained(folder, 'release.json')))
    if digest(definition) != manifest['definition_sha256']:
        raise ReleaseError('发行定义不匹配')
    if any(manifest[k] != definition[k] for k in ('product_id','version','channel','build_number','data_contract')):
        raise ReleaseError('构建清单的版本或数据契约与发行定义不匹配')
    schema = validate_schema(read_json(contained(folder, 'schema.json')))
    if schema['sha256'] != manifest['schema_sha256']:
        raise ReleaseError('数据结构清单不匹配')
    sources = validate_records(read_json(contained(folder, 'source-files.json')))
    if digest(sources) != manifest['source_sha256']:
        raise ReleaseError('源码清单指纹不匹配')
    if source_root:
        if source_inventory(source_root) != sources:
            raise ReleaseError('当前源码与构建清单不同，请重新准备')
        verify_records(source_root, read_json(contained(folder, 'frontend-files.json')))
        check_versions(source_root)
    if check_environment:
        from .release_dependencies import verify_python_environment
        verify_python_environment(folder, manifest['coverage']['dependencies']['python'])
    if wheelhouse:
        licenses = read_json(contained(folder, 'license-evidence.json'))
        artifacts = {r['path']: r for component in licenses['python'] for r in component['wheel_artifacts']}
        if not manifest['coverage']['dependencies']['python']['wheel_hashes_complete']:
            raise ReleaseError('该构建清单缺少完整 wheel 哈希，无法验证离线依赖闭包')
        verify_records(wheelhouse, list(artifacts.values()))
    signature = verify_signature(folder, manifest, trusted_keys)
    blockers = list(manifest.get('release_blockers', []))
    if signature['trusted']:
        blockers = [b for b in blockers if b != 'publisher_signature_required']
    elif 'publisher_signature_required' not in blockers:
        blockers.append('publisher_signature_required')
    # Prepared input manifests are never enough to certify a final installer,
    # even if someone deletes their self-reported blockers and signs them.
    blockers.append('prepared_inputs_are_not_a_verified_installer')
    if require_public:
        raise ReleaseError('尚未具备公开发行条件：' + ', '.join(blockers))
    return {'integrity': 'verified', 'source_verified': bool(source_root), 'build_id': manifest['build_id'],
            'python_environment_verified': check_environment, 'wheelhouse_verified': bool(wheelhouse),
            'signature': signature, 'public_release_ready': False, 'release_blockers': blockers,
            'artifact_state': manifest['artifact_state']}
