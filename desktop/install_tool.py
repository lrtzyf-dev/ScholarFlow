"""Explicit offline candidate install/recovery. Never downloads or publishes."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.release_install import describe_candidate, verify_candidate, install, recover, load_external_trust


def load_action_trust(trusted_keys, *, candidate=None, target=None, data=None):
    """Keep authority outside program, data, backup and transaction directories."""
    from app.release_files import no_link
    from app.data_backup import recovery_root
    excluded = [candidate] if candidate is not None else []
    if target is not None:
        root = no_link(no_link(target).resolve())
        excluded += [root, root.parent / ('.' + root.name + '-updates')]
    if data is not None:
        root = no_link(no_link(data).resolve())
        excluded += [root, recovery_root(root)]
    return load_external_trust(trusted_keys, untrusted_roots=excluded)


def run_offline_action(action, *, trusted_keys, candidate=None, target=None, data=None,
                       direction='upgrade', expected_candidate_id=None):
    """Shared CLI/GUI adapter; the existing transaction remains the authority."""
    from app.release_files import no_link
    if action not in {'verify', 'install', 'recover'}:
        raise ValueError('未知离线操作')
    if action != 'recover' and candidate is None:
        raise ValueError('必须明确选择候选目录')
    if action != 'verify':
        if target is None or data is None:
            raise ValueError('必须明确选择安装及数据目录')
        root = no_link(no_link(target).resolve())
        if Path(__file__).resolve().is_relative_to(root):
            raise ValueError('独立安装/恢复工具必须放在安装目标目录之外运行')
    trust = load_action_trust(trusted_keys, candidate=candidate, target=target, data=data)
    if action == 'verify':
        manifest = verify_candidate(candidate, trust)
        return {'candidate_id': manifest['candidate_id'], 'integrity': 'verified',
                'signature': 'trusted', 'public_release_ready': False}
    if action == 'recover':
        return recover(target, data, trusted_keys=trust)
    if expected_candidate_id is not None:
        if verify_candidate(candidate, trust)['candidate_id'] != expected_candidate_id:
            raise ValueError('候选身份已变化，请重新校验后执行')
    result = install(candidate, target, data, trusted_keys=trust, direction=direction,
                     expected_candidate_id=expected_candidate_id)
    if expected_candidate_id is not None and result.get('candidate_id') != expected_candidate_id:
        # Defensive reporting for an invalid adapter/backend receipt. The
        # production transaction binds expected identity under its own lock.
        # Never suggest retrying an already committed installation.
        result = dict(result, candidate_identity_changed=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser('gui', help='独立离线安装/更新/恢复窗口；只预填，不自动执行')
    for name in ('candidate', 'target', 'data', 'trusted-keys', 'shortcut-directory'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--direction', choices=['upgrade', 'rollback'], default='upgrade')
    p = commands.add_parser("describe-candidate")
    p.add_argument("folder", type=Path)
    p.add_argument("--prepared-inputs", type=Path, required=True)
    p.add_argument("--entrypoint", default="ScholarFlow.exe")
    for command in ("verify", "install", "recover"):
        p = commands.add_parser(command)
        p.add_argument("--trusted-keys", type=Path, required=True, help="External trust store; never from candidate")
        if command != "recover": p.add_argument("candidate", type=Path)
        if command != "verify":
            p.add_argument("--target", type=Path, required=True)
            p.add_argument("--data", type=Path, required=True)
        if command == "install":
            p.add_argument("--direction", choices=["upgrade", "rollback"], default="upgrade")
            p.add_argument('--expected-candidate-id', help='将安装绑定到已审核的64位小写十六进制候选ID')
    for command in ('inspect-legacy', 'prepare-legacy-adoption', 'verify-legacy-preparation'):
        p = commands.add_parser(command, help='旧平铺识别或准备；不会安装、执行旧程序或迁移数据')
        p.add_argument('--legacy', type=Path, required=True)
        p.add_argument('--data', type=Path, required=True)
        p.add_argument('--target', type=Path, required=True)
        p.add_argument('--trusted-keys', type=Path, required=True)
        if command == 'verify-legacy-preparation':
            p.add_argument('--preparation', type=Path, required=True)
        else:
            p.add_argument('candidate', type=Path)
            p.add_argument('--entrypoint', default='ScholarFlow.exe')
        if command == 'prepare-legacy-adoption':
            p.add_argument('--destination', type=Path, required=True)
            p.add_argument('--observation-sha256', required=True)
            p.add_argument('--confirm-legacy-closed', action='store_true')
    for command in ('review-legacy-import', 'import-legacy-data', 'recover-legacy-import'):
        p = commands.add_parser(command, help='已审核精确结构的分离数据导入；不安装或执行候选程序')
        for name in ('preparation', 'legacy', 'data', 'target', 'output', 'trusted-keys'):
            p.add_argument('--' + name, type=Path, required=True)
        p.add_argument('--migration-id', required=True)
        p.add_argument('--preparation-sha256', required=True)
        if command != 'review-legacy-import':
            p.add_argument('--review-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'gui':
            if __package__:
                from .install_gui import main as gui_main
            else:
                from install_gui import main as gui_main
            return gui_main({name: getattr(args, name) for name in
                ('candidate', 'target', 'data', 'trusted_keys', 'shortcut_directory', 'direction')})
        if args.command == "describe-candidate":
            value = describe_candidate(args.folder, args.prepared_inputs, entrypoint=args.entrypoint)
        elif args.command in {'review-legacy-import', 'import-legacy-data', 'recover-legacy-import'}:
            from app.legacy_import import review_import, import_data, recover_import
            from app.data_backup import recovery_root
            from app.release_files import no_link
            # Normalize before using .name/.parent. A dot-dot alias must not
            # cause trust exclusion to cover a different transaction directory.
            output_root = no_link(no_link(args.output).resolve())
            program_root = no_link(no_link(args.target).resolve())
            data_root = no_link(no_link(args.data).resolve())
            excluded = [args.preparation, args.legacy, data_root, program_root, output_root,
                recovery_root(data_root), output_root.parent / ('.' + output_root.name + '-imports'),
                program_root.parent / ('.' + program_root.name + '-updates')]
            trust = load_external_trust(args.trusted_keys, untrusted_roots=excluded)
            operation = {'review-legacy-import': review_import, 'import-legacy-data': import_data,
                         'recover-legacy-import': recover_import}[args.command]
            options = {'trusted_keys': trust, 'migration_id': args.migration_id,
                       'expected_preparation_sha256': args.preparation_sha256}
            if args.command != 'review-legacy-import':
                options['expected_review_sha256'] = args.review_sha256
            value = operation(args.preparation, args.legacy, args.data, args.target, args.output, **options)
        elif args.command in {'inspect-legacy', 'prepare-legacy-adoption', 'verify-legacy-preparation'}:
            from app.legacy_install import inspect_legacy, prepare_legacy, verify_preparation
            excluded = [args.legacy, args.data, args.target]
            from app.data_backup import recovery_root
            from app.release_files import no_link
            target_root = no_link(no_link(args.target).resolve())
            excluded += [recovery_root(args.data), target_root.parent / ('.' + target_root.name + '-updates')]
            if args.command == 'verify-legacy-preparation':
                excluded.append(args.preparation)
            else:
                excluded.append(args.candidate)
            if args.command == 'prepare-legacy-adoption': excluded.append(args.destination)
            trust = load_external_trust(args.trusted_keys, untrusted_roots=excluded)
            if args.command == 'inspect-legacy':
                value = inspect_legacy(args.legacy, args.data, args.target, args.candidate,
                    trusted_keys=trust, entrypoint=args.entrypoint)
            elif args.command == 'prepare-legacy-adoption':
                value = prepare_legacy(args.legacy, args.data, args.target, args.candidate, args.destination,
                    trusted_keys=trust, expected_observation_sha256=args.observation_sha256,
                    confirm_legacy_closed=args.confirm_legacy_closed, entrypoint=args.entrypoint)
            else:
                value = verify_preparation(args.preparation, args.legacy, args.data, args.target, trusted_keys=trust)
        else:
            value = run_offline_action(args.command, trusted_keys=args.trusted_keys,
                candidate=getattr(args, 'candidate', None), target=getattr(args, 'target', None),
                data=getattr(args, 'data', None), direction=getattr(args, 'direction', 'upgrade'),
                expected_candidate_id=getattr(args, 'expected_candidate_id', None))
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Do not echo candidate subprocess output or credentials.
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
if __name__ == "__main__": raise SystemExit(main())
