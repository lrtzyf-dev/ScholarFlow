"""Build an independent Agent finalization fix from the current model-list bundle.

Read-only preflight:
    python desktop/build_agent_finalization.py --preflight
After final backend modules and frontend build are approved for packaging:
    python desktop/build_agent_finalization.py --build --frontend-ready --module app.example
Verify the created bundle and immutable backup:
    python desktop/build_agent_finalization.py --verify

The source is deliberately fixed to ScholarFlow-模型列表版. No existing bundle,
shortcut, process, user configuration or database is changed. Replacements are
compiled with the source bundle's Python 3.13 DLL, preserving its PE icon and
every unrequested archive module. This script does not launch the application.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import update_backend as archive_tools


ROOT = Path(__file__).resolve().parent.parent
PROGRAMS = ROOT / 'ScholarFlow程序'
BASE = PROGRAMS / 'ScholarFlow-模型列表版' / 'ScholarFlow.exe'
DESTINATION = PROGRAMS / 'ScholarFlow-任务收尾修复版'
BACKUPS = PROGRAMS / '任务收尾修复备份'
FRONTEND = ROOT / 'frontend' / 'dist'
MANIFEST_NAME = 'agent-finalization-build.json'
MODULE_PATTERN = re.compile(r'app(?:\.[A-Za-z_][A-Za-z0-9_]*)+')


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def hashes(directory: Path, *, exclude_frontend: bool = False) -> dict[str, str]:
    directory = directory.resolve(strict=True)
    result = {}
    for path in sorted(directory.rglob('*')):
        relative = path.relative_to(directory).as_posix()
        if exclude_frontend and (relative == 'frontend' or relative.startswith('frontend/')):
            continue
        require(not path.is_symlink() and path.resolve().is_relative_to(directory),
                'Bundle contains an unsupported link or external path')
        if path.is_file():
            result[relative] = digest(path)
    return result


def configure_archive_tools(modules: tuple[str, ...], added: set[str]) -> None:
    # These module globals belong only to this one packaging process. Existing
    # helpers remain unmodified on disk and never receive an install/apply call.
    archive_tools.TARGET = BASE
    archive_tools.MODULES = modules
    archive_tools.NEW_MODULES = added


def preflight(modules: tuple[str, ...] = (), new_modules: tuple[str, ...] = ()) -> dict:
    require(os.name == 'nt', 'This packaging helper requires Windows')
    require(BASE.is_file(), 'Current model-list bundle executable is missing')
    require(BASE.parent.resolve().parent == PROGRAMS.resolve(), 'Base bundle has an unexpected path')
    require((BASE.parent / '_internal/python313.dll').is_file(), 'Bundled Python 3.13 runtime is missing')
    require((FRONTEND / 'index.html').is_file() and (FRONTEND / 'assets').is_dir(),
            'Completed frontend/dist build is missing')
    require(DESTINATION.resolve().parent == PROGRAMS.resolve()
            and DESTINATION.resolve() != BASE.parent.resolve(), 'Invalid destination bundle')
    require(len(modules) == len(set(modules)), 'Requested backend module list contains duplicates')
    require(set(new_modules).issubset(modules), 'New modules must be included in --module list')
    original = archive_tools.Archive(BASE.read_bytes())
    for module in modules:
        require(MODULE_PATTERN.fullmatch(module) is not None, 'Invalid backend module name')
        source = ROOT / 'backend' / (module.replace('.', '/') + '.py')
        require(source.is_file() and source.resolve().is_relative_to((ROOT / 'backend').resolve()),
                'Requested module source is missing or outside backend')
        require(module in original.modules or module in new_modules,
                'Requested module is not in bundle; explicitly declare it with --new-module')
    for module in new_modules:
        require(module not in original.modules, '--new-module was already present in the base archive')
    require('app.model_connection' in original.modules and 'app.api.settings' in original.modules,
            'Base archive does not contain the model discovery modules')
    return {'base': str(BASE), 'base_sha256': digest(BASE), 'destination': str(DESTINATION),
            'destination_exists': DESTINATION.exists(), 'backend_modules': list(modules),
            'added_modules': list(new_modules), 'base_module_count': len(original.modules),
            'frontend_index_sha256': digest(FRONTEND / 'index.html'), 'launches_app': False}


def build(modules: tuple[str, ...], new_modules: tuple[str, ...], frontend_ready: bool) -> dict:
    require(frontend_ready, 'Pass --frontend-ready only after the final frontend build and checks pass')
    require(bool(modules), 'Supply the final changed backend module list with --module')
    before = preflight(modules, new_modules)
    require(not DESTINATION.exists(), 'Destination already exists; refusing to overwrite it')
    original_bytes = BASE.read_bytes()
    original = archive_tools.Archive(original_bytes)
    configure_archive_tools(modules, set(new_modules))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = BACKUPS / stamp
    backup.mkdir(parents=True)
    (backup / 'ScholarFlow-original.exe').write_bytes(original_bytes)
    compiled, sources, compiler_output = archive_tools.compile_modules(original, backup)
    candidate_bytes = archive_tools.rebuild(original, compiled)
    candidate = archive_tools.Archive(candidate_bytes)
    preservation = archive_tools.verify_preservation(original, candidate, compiled)
    runtime_output = archive_tools.verify_runtime_toc(candidate, backup)
    (backup / 'ScholarFlow-updated.exe').write_bytes(candidate_bytes)
    frontend_hashes = hashes(FRONTEND)
    runtime = BASE.parent / '_internal'
    runtime_hashes = hashes(runtime, exclude_frontend=True)
    staging = PROGRAMS / ('ScholarFlow-任务收尾修复版-staging-' + stamp)
    require(staging.resolve().parent == PROGRAMS.resolve()
            and staging.resolve() not in (BASE.parent.resolve(), DESTINATION.resolve()),
            'Staging path leaves the intended program directory')
    require(not staging.exists(), 'Staging directory already exists')
    staging.mkdir()

    def exclude_old_frontend(path: str, names: list[str]) -> list[str]:
        return ['frontend'] if Path(path).resolve() == runtime.resolve() and 'frontend' in names else []

    copied_runtime = staging / '_internal'
    shutil.copytree(runtime, copied_runtime, ignore=exclude_old_frontend)
    shutil.copytree(FRONTEND, copied_runtime / 'frontend')
    candidate_exe = staging / 'ScholarFlow.exe'
    candidate_exe.write_bytes(candidate_bytes)
    require(digest(BASE) == before['base_sha256'], 'Base executable changed during packaging')
    require(hashes(runtime, exclude_frontend=True) == runtime_hashes, 'Base runtime changed during packaging')
    require(hashes(FRONTEND) == frontend_hashes, 'Frontend build changed during packaging')
    for module, source in sources.items():
        require(digest(Path(source['source'])) == source['source_sha256'],
                'Backend source changed during packaging: ' + module)
    require(hashes(copied_runtime, exclude_frontend=True) == runtime_hashes, 'Copied runtime checksum mismatch')
    require(hashes(copied_runtime / 'frontend') == frontend_hashes, 'Copied frontend checksum mismatch')
    require(digest(candidate_exe) == archive_tools.digest(candidate_bytes), 'Candidate executable checksum mismatch')
    manifest = {'schema_version': 1, 'state': 'built', 'created_utc': stamp,
                'base': str(BASE), 'destination': str(DESTINATION), 'backup': str(backup),
                'base_sha256': before['base_sha256'], 'candidate_sha256': digest(candidate_exe),
                'backend_modules': list(modules), 'new_modules': list(new_modules),
                'sources': sources, 'preservation': preservation,
                'compiler': (backup / 'compiler-version.txt').read_text(encoding='utf-8'),
                'frontend': frontend_hashes, 'runtime': runtime_hashes,
                'source_bundle_unchanged': True, 'shortcuts_changed': False,
                'user_data_read_or_changed': False}
    archive_tools.save_json(backup / 'manifest.json', manifest)
    archive_tools.save_json(staging / MANIFEST_NAME, manifest)
    # Both absolute directory targets are validated immediately before this
    # same-parent rename; no recursive delete or overwriting is performed.
    require(staging.resolve().parent == PROGRAMS.resolve()
            and DESTINATION.resolve().parent == PROGRAMS.resolve()
            and not DESTINATION.exists(), 'Destination changed before publishing candidate')
    staging.rename(DESTINATION)
    result = verify()
    return {**result, 'compiler_output': compiler_output, 'runtime_verification': runtime_output}


def verify() -> dict:
    require(DESTINATION.is_dir(), 'Candidate bundle is missing')
    record = json.loads((DESTINATION / MANIFEST_NAME).read_text(encoding='utf-8'))
    require(record.get('schema_version') == 1 and Path(record['base']).resolve() == BASE.resolve()
            and Path(record['destination']).resolve() == DESTINATION.resolve(), 'Unexpected build manifest')
    backup = Path(record['backup']).resolve(strict=True)
    require(backup.is_relative_to(BACKUPS.resolve()) and backup != BACKUPS.resolve(), 'Unexpected backup path')
    require(json.loads((backup / 'manifest.json').read_text(encoding='utf-8')) == record,
            'Backup and candidate manifests differ')
    require(digest(BASE) == record['base_sha256'], 'Source bundle executable changed')
    require(digest(backup / 'ScholarFlow-original.exe') == record['base_sha256'], 'Base backup checksum mismatch')
    require(digest(backup / 'ScholarFlow-updated.exe') == record['candidate_sha256'], 'Candidate backup checksum mismatch')
    require(digest(DESTINATION / 'ScholarFlow.exe') == record['candidate_sha256'], 'Candidate executable checksum mismatch')
    modules = tuple(record['backend_modules'])
    configure_archive_tools(modules, set(record['new_modules']))
    compiled = {}
    for module in modules:
        require(MODULE_PATTERN.fullmatch(module) is not None, 'Invalid recorded backend module name')
        source_snapshot = backup / 'modules' / (module + '.py')
        compiled_path = backup / 'modules' / (module + '.updated.marshal')
        require(digest(source_snapshot) == record['sources'][module]['source_sha256'], 'Source snapshot checksum mismatch')
        require(digest(compiled_path) == record['sources'][module]['bytecode_sha256'], 'Compiled snapshot checksum mismatch')
        compiled[module] = compiled_path.read_bytes()
    actual_preservation = archive_tools.verify_preservation(
        archive_tools.Archive((backup / 'ScholarFlow-original.exe').read_bytes()),
        archive_tools.Archive((DESTINATION / 'ScholarFlow.exe').read_bytes()), compiled)
    require(actual_preservation == record['preservation'], 'Archive preservation report differs')
    require(hashes(DESTINATION / '_internal', exclude_frontend=True) == record['runtime'], 'Candidate runtime changed')
    require(hashes(BASE.parent / '_internal', exclude_frontend=True) == record['runtime'], 'Source runtime changed')
    require(hashes(DESTINATION / '_internal/frontend') == record['frontend'], 'Candidate frontend changed')
    return {'ok': True, 'executable': str(DESTINATION / 'ScholarFlow.exe'), 'backup': str(backup),
            'base': str(BASE), 'base_sha256': record['base_sha256'],
            'executable_sha256': record['candidate_sha256'], 'updated_modules': list(modules),
            'preserved_module_count': actual_preservation['unchanged_module_count'],
            'icon_preserved': True, 'runtime_file_count': len(record['runtime']),
            'frontend_file_count': len(record['frontend']), 'source_bundle_unchanged': True,
            'shortcuts_changed': False, 'user_data_read_or_changed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight', action='store_true')
    mode.add_argument('--build', action='store_true')
    mode.add_argument('--verify', action='store_true')
    parser.add_argument('--module', action='append', default=[], dest='modules')
    parser.add_argument('--new-module', action='append', default=[], dest='new_modules')
    parser.add_argument('--frontend-ready', action='store_true')
    args = parser.parse_args()
    if args.verify:
        result = verify()
    elif args.build:
        result = build(tuple(args.modules), tuple(args.new_modules), args.frontend_ready)
    else:
        result = preflight(tuple(args.modules), tuple(args.new_modules))
    print(json.dumps(result, ensure_ascii=False))
