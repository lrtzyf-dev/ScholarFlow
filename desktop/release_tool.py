"""Local release engineering CLI. Does not build, download, install or migrate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))

from app.release_info import ReleaseError, read_json, validate_definition
from app.release_manifest import check_versions, prepare, verify
from app.release_schema import compatibility_preflight


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('check-versions', 'sync-versions'):
        commands.add_parser(name)
    p = commands.add_parser('prepare')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--wheelhouse', type=Path)
    p = commands.add_parser('inventory-dependencies')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--wheelhouse', type=Path)
    p = commands.add_parser('verify')
    p.add_argument('folder', type=Path)
    p.add_argument('--source-root', type=Path)
    p.add_argument('--trusted-keys', type=Path, help='External trust store; never use one shipped by the candidate package')
    p.add_argument('--public', action='store_true', dest='public_release')
    p.add_argument('--environment', action='store_true', help='Compare this interpreter and installed distributions against prepared evidence')
    p.add_argument('--wheelhouse', type=Path)
    p = commands.add_parser('lock-wheels')
    p.add_argument('wheelhouse', type=Path)
    p.add_argument('--lock', required=True, type=Path)
    p.add_argument('--manifest', required=True, type=Path)
    p = commands.add_parser('verify-wheel-lock')
    p.add_argument('wheelhouse', type=Path)
    p.add_argument('--lock', required=True, type=Path)
    p = commands.add_parser('compatibility-preflight')
    p.add_argument('folder', type=Path)
    p.add_argument('--database', required=True, type=Path)
    p.add_argument('--current-definition', required=True, type=Path)
    p.add_argument('--direction', choices=['upgrade', 'rollback'], default='upgrade')
    p.add_argument('--backup', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'check-versions':
            result = check_versions(ROOT)
        elif args.command == 'sync-versions':
            version = validate_definition(read_json(ROOT / 'release.json'))['version']
            for file in ('package.json', 'package-lock.json'):
                path = ROOT / 'frontend' / file
                value = read_json(path)
                value['version'] = version
                if file == 'package-lock.json':
                    value['packages']['']['version'] = version
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            result = {'version': version, 'derived_files_updated': True}
        elif args.command == 'inventory-dependencies':
            from app.release_dependencies import collect_dependencies, write_json
            from app.release_files import no_link, walk_regular, file_record
            output = no_link(args.output)
            if output.exists():
                raise ReleaseError('依赖证据输出目录必须不存在')
            output.mkdir(parents=True)
            result = collect_dependencies(ROOT, output, args.wheelhouse)
            write_json(output / 'dependency-evidence-manifest.json', {'format': 'scholarflow-dependency-evidence',
                'version': 1, 'files': [file_record(output, p) for p in walk_regular(output)]})
        elif args.command == 'prepare':
            manifest = prepare(ROOT, args.output, wheelhouse=args.wheelhouse)
            result = {'build_id': manifest['build_id'], 'output': str(args.output), 'artifact_state': manifest['artifact_state'],
                      'release_blockers': manifest['release_blockers']}
        elif args.command == 'verify-wheel-lock':
            from app.release_wheels import wheel_lock
            from app.release_files import no_link
            lock, manifest = wheel_lock(args.wheelhouse)
            if no_link(args.lock).read_text(encoding='utf-8') != lock:
                raise ReleaseError('依赖锁与 wheel 文件及 SHA-256 不一致')
            result = {'integrity': 'verified', 'package_count': manifest['package_count']}
        elif args.command == 'lock-wheels':
            from app.release_wheels import wheel_lock
            from app.release_dependencies import write_json
            if args.lock.exists() or args.manifest.exists():
                raise ReleaseError('依赖锁与 wheel 清单不能覆盖，请指定新路径')
            lock, manifest = wheel_lock(args.wheelhouse)
            args.lock.parent.mkdir(parents=True, exist_ok=True)
            args.lock.write_text(lock, encoding='utf-8')
            write_json(args.manifest, manifest)
            result = {'package_count': manifest['package_count'], 'lock': str(args.lock), 'manifest': str(args.manifest)}
        elif args.command == 'verify':
            from app.release_install import load_external_trust
            result = verify(args.folder, source_root=args.source_root,
                trusted_keys=load_external_trust(args.trusted_keys, untrusted_roots=[args.folder]) if args.trusted_keys else {}, require_public=args.public_release,
                check_environment=args.environment, wheelhouse=args.wheelhouse)
        else:
            verify(args.folder)
            definition = validate_definition(read_json(args.folder / 'release.json'))
            current = validate_definition(read_json(args.current_definition))
            result = compatibility_preflight(args.database, read_json(args.folder / 'schema.json'), definition['data_contract'],
                direction=args.direction, current_contract=current['data_contract'], backup=args.backup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ReleaseError, OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
