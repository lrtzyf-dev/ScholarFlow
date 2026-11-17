"""Offline entry/controller boundaries; only synthetic paths and injected workers."""
import json
from pathlib import Path
import sys
import threading
import time
import socket

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from desktop import install_gui as gui
from desktop import install_shortcuts as shortcuts
from desktop import install_tool as cli


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network is forbidden in offline entry tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def request(tmp_path, **kwargs):
    values = dict(candidate=tmp_path / 'candidate', target=tmp_path / 'target',
        data=tmp_path / 'data', trusted_keys=tmp_path / 'external.json', closed_confirmed=True)
    values.update(kwargs)
    return gui.InstallRequest(**values)


def complete(controller):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        event = controller.poll()
        if event is not None:
            return event
        time.sleep(.005)
    raise AssertionError('controller did not complete')


def worker(calls):
    def run(action, **kwargs):
        calls.append((action, kwargs))
        if action == 'verify':
            return dict(candidate_id='a' * 64, integrity='verified', signature='trusted')
        return dict(state='committed', candidate_id='a' * 64, data_modified=False)
    return run


def test_requires_matching_verification_and_explicit_closed_confirmation(tmp_path):
    calls = []
    controller = gui.InstallController(action_runner=worker(calls))
    req = request(tmp_path)
    with pytest.raises(ValueError, match='校验'):
        controller.submit('install', req)
    controller.submit('verify', req)
    assert complete(controller).ok
    with pytest.raises(ValueError, match='关闭'):
        controller.submit('install', request(tmp_path, closed_confirmed=False))
    with pytest.raises(ValueError, match='校验'):
        controller.submit('install', request(tmp_path, direction='rollback'))
    controller.submit('install', req)
    event = complete(controller)
    assert event.ok and event.value['state'] == 'committed'
    assert [c[0] for c in calls] == ['verify', 'install']
    controller.close()


def test_form_change_invalidates_prior_receipt(tmp_path):
    controller = gui.InstallController(action_runner=worker([]))
    req = request(tmp_path)
    controller.submit('verify', req)
    complete(controller)
    controller.invalidate()
    with pytest.raises(ValueError, match='校验'):
        controller.submit('install', req)
    controller.close()


def test_invalidation_while_verifying_cannot_restore_stale_receipt(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def blocked(action, **kwargs):
        entered.set()
        assert release.wait(5)
        return dict(candidate_id='a' * 64, integrity='verified', signature='trusted')
    controller = gui.InstallController(action_runner=blocked)
    req = request(tmp_path)
    controller.submit('verify', req)
    assert entered.wait(2)
    controller.invalidate()
    release.set()
    complete(controller)
    with pytest.raises(ValueError, match='校验'):
        controller.submit('install', req)
    controller.close()


def test_busy_rejects_duplicate_and_close_until_main_thread_polls(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def blocking(action, **kwargs):
        entered.set()
        assert release.wait(5)
        return dict(candidate_id='a' * 64, integrity='verified', signature='trusted')
    controller = gui.InstallController(action_runner=blocking)
    controller.submit('verify', request(tmp_path))
    assert entered.wait(2)
    with pytest.raises(ValueError, match='运行'):
        controller.submit('verify', request(tmp_path))
    assert not controller.can_close
    with pytest.raises(ValueError, match='运行'):
        controller.close()
    release.set()
    assert complete(controller).ok
    assert controller.can_close
    controller.close()


@pytest.mark.parametrize('action', ['install', 'recover'])
def test_failures_are_not_success_or_retried(tmp_path, action):
    calls = []
    def failing(command, **kwargs):
        calls.append(command)
        if command == 'verify':
            return dict(candidate_id='a' * 64, integrity='verified', signature='trusted')
        raise ValueError('运行锁占用，请关闭研序后恢复')
    controller = gui.InstallController(action_runner=failing)
    req = request(tmp_path)
    if action == 'install':
        controller.submit('verify', req)
        complete(controller)
    controller.submit(action, req)
    event = complete(controller)
    assert not event.ok and '运行锁' in event.error
    assert calls.count(action) == 1
    assert event.value is None
    controller.close()


def test_unknown_worker_error_does_not_echo_sensitive_exception(tmp_path):
    def failing(*args, **kwargs):
        raise RuntimeError('secret-synthetic-token')
    controller = gui.InstallController(action_runner=failing)
    controller.submit('recover', request(tmp_path))
    event = complete(controller)
    assert not event.ok and 'secret-synthetic-token' not in event.error
    controller.close()


def test_shortcuts_are_opt_in_and_failure_does_not_erase_commit(tmp_path):
    calls, links = [], []
    def link(*args, **kwargs):
        links.append((args, kwargs))
        raise OSError('synthetic shortcut denied')
    controller = gui.InstallController(action_runner=worker(calls), shortcut_writer=link)
    req = request(tmp_path)
    controller.submit('verify', req)
    complete(controller)
    controller.submit('install', req)
    assert complete(controller).ok
    assert not links
    req = request(tmp_path, shortcut_directory=tmp_path / 'chosen')
    controller.submit('verify', req)
    complete(controller)
    controller.submit('install', req)
    event = complete(controller)
    assert event.ok and event.value['state'] == 'committed'
    assert event.value['shortcut']['state'] == 'failed'
    assert len(links) == 1
    controller.close()


def test_recover_requires_closed_confirmation(tmp_path):
    controller = gui.InstallController(action_runner=worker([]))
    with pytest.raises(ValueError, match='关闭'):
        controller.submit('recover', request(tmp_path, closed_confirmed=False))
    controller.close()


def test_cli_gui_dispatch_is_lazy_and_does_not_execute_install(monkeypatch):
    calls = []
    monkeypatch.setattr(gui, 'main', lambda initial=None: calls.append(initial) or 0)
    monkeypatch.setattr(cli, 'install', lambda *a, **k: pytest.fail('unexpected install'))
    assert cli.main(['gui', '--target', 'chosen-target']) == 0
    assert calls[0]['target'] == Path('chosen-target')


def test_shared_runner_uses_production_trust_and_transactions(tmp_path, monkeypatch):
    calls = []
    trust = {'synthetic': 'key'}
    monkeypatch.setattr(cli, 'load_external_trust', lambda p, **k: calls.append(('trust', p, k)) or trust)
    monkeypatch.setattr(cli, 'verify_candidate', lambda p, t: calls.append(('verify', p, t)) or {'candidate_id': 'a' * 64})
    monkeypatch.setattr(cli, 'install', lambda *a, **k: calls.append(('install', a, k)) or {'state': 'committed', 'candidate_id': 'a' * 64})
    monkeypatch.setattr(cli, 'recover', lambda *a, **k: calls.append(('recover', a, k)) or {'state': 'nothing_pending'})
    req = request(tmp_path)
    options = req.action_options()
    assert cli.run_offline_action('verify', **options)['integrity'] == 'verified'
    assert cli.run_offline_action('install', **options, expected_candidate_id='a' * 64)['state'] == 'committed'
    assert cli.run_offline_action('recover', **options)['state'] == 'nothing_pending'
    assert [c[0] for c in calls] == ['trust', 'verify', 'trust', 'verify', 'install', 'trust', 'recover']
    assert all(c[-1]['trusted_keys'] is trust for c in calls if c[0] in {'install', 'recover'})
    assert next(c[-1] for c in calls if c[0] == 'install')['expected_candidate_id'] == 'a' * 64
    excluded = calls[0][2]['untrusted_roots']
    assert req.data in excluded and req.target in excluded and req.candidate in excluded


def test_runner_refuses_identity_changed_before_install(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'load_external_trust', lambda *a, **k: {})
    monkeypatch.setattr(cli, 'verify_candidate', lambda *a, **k: {'candidate_id': 'b' * 64})
    monkeypatch.setattr(cli, 'install', lambda *a, **k: pytest.fail('unexpected install'))
    with pytest.raises(ValueError, match='变化'):
        cli.run_offline_action('install', **request(tmp_path).action_options(), expected_candidate_id='a' * 64)


def test_updater_inside_install_target_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'target' / 'install_tool.py'))
    monkeypatch.setattr(cli, 'install', lambda *a, **k: pytest.fail('unexpected install'))
    with pytest.raises(ValueError, match='外'):
        cli.run_offline_action('install', **request(tmp_path).action_options())


def test_cli_install_forwards_explicit_expected_candidate_id(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, 'run_offline_action', lambda action, **kwargs: calls.append((action, kwargs)) or {'state': 'committed'})
    assert cli.main(['install', 'candidate', '--target', 'target', '--data', 'data',
        '--trusted-keys', 'trust.json', '--expected-candidate-id', 'a' * 64]) == 0
    assert calls[0][1]['expected_candidate_id'] == 'a' * 64
    assert json.loads(capsys.readouterr().out)['state'] == 'committed'


def prepare_shortcut(tmp_path, monkeypatch):
    target = tmp_path / '安装 空间 `$()'
    (target / 'payload').mkdir(parents=True)
    (target / 'payload' / 'ScholarFlow.exe').write_bytes(b'synthetic')
    directory = tmp_path / '选定 目录 `$()'
    directory.mkdir()
    monkeypatch.setattr(shortcuts, 'verify_candidate', lambda *a, **k: {'candidate_id': 'a' * 64, 'entrypoint': 'ScholarFlow.exe'})
    receipt = dict(state='committed', candidate_id='a' * 64)
    return target, directory, receipt


def test_shortcut_fixed_name_signed_target_and_no_overwrite(tmp_path, monkeypatch):
    target, directory, receipt = prepare_shortcut(tmp_path, monkeypatch)
    written = []
    def writer(link, executable, working):
        written.append((link, executable, working))
        link.write_bytes(b'synthetic-link')
    result = shortcuts.create_shortcut(target, directory, {}, receipt, native_writer=writer)
    assert result['state'] == 'created'
    assert (directory / 'ScholarFlow.lnk').read_bytes() == b'synthetic-link'
    assert written[0][1] == target / 'payload' / 'ScholarFlow.exe'
    with pytest.raises(ValueError, match='存在'):
        shortcuts.create_shortcut(target, directory, {}, receipt, native_writer=writer)
    assert len(written) == 1


@pytest.mark.parametrize('receipt', [{'state': 'rolled_back', 'candidate_id': 'a' * 64}, {'state': 'committed', 'candidate_id': 'b' * 64}])
def test_shortcut_requires_committed_matching_identity(tmp_path, monkeypatch, receipt):
    target, directory, _ = prepare_shortcut(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        shortcuts.create_shortcut(target, directory, {}, receipt, native_writer=lambda *a: pytest.fail('unexpected native call'))
    assert not list(directory.iterdir())


def test_shortcut_rejects_install_or_forbidden_directory(tmp_path, monkeypatch):
    target, directory, receipt = prepare_shortcut(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        shortcuts.create_shortcut(target, target / 'payload', {}, receipt, native_writer=lambda *a: None)
    with pytest.raises(ValueError):
        shortcuts.create_shortcut(target, directory, {}, receipt, forbidden_roots=[directory], native_writer=lambda *a: None)


def test_shortcut_can_be_a_safe_sibling_of_candidate_and_install(tmp_path, monkeypatch):
    target, _, receipt = prepare_shortcut(tmp_path, monkeypatch)
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    result = shortcuts.create_shortcut(target, tmp_path, {}, receipt, forbidden_roots=[candidate],
        native_writer=lambda link, *args: link.write_bytes(b'synthetic-link'))
    assert result['path'] == str(tmp_path / 'ScholarFlow.lnk')


def test_powershell_script_never_interpolates_paths(tmp_path, monkeypatch):
    target, directory, _ = prepare_shortcut(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(shortcuts.subprocess, 'run', lambda args, **kwargs: calls.append((args, kwargs)))
    shortcuts._native_writer(directory / 'ScholarFlow.lnk', target / 'payload' / 'ScholarFlow.exe', target / 'payload')
    args, options = calls[0]
    assert str(target) not in args[-1] and str(directory) not in args[-1]
    assert '$env:SF_SHORTCUT_TARGET' in args[-1]
    assert options['env']['SF_SHORTCUT_TARGET'] == str(target / 'payload' / 'ScholarFlow.exe')
    assert options.get('shell', False) is False


def test_shortcut_native_failure_cleans_only_owned_temporary_file(tmp_path, monkeypatch):
    target, directory, receipt = prepare_shortcut(tmp_path, monkeypatch)
    unrelated = directory / 'keep.txt'
    unrelated.write_text('keep')
    def fail(*args):
        raise OSError('synthetic failure')
    with pytest.raises(OSError):
        shortcuts.create_shortcut(target, directory, {}, receipt, native_writer=fail)
    assert list(directory.iterdir()) == [unrelated]


def test_shortcut_publish_race_does_not_overwrite(tmp_path, monkeypatch):
    target, directory, receipt = prepare_shortcut(tmp_path, monkeypatch)
    def racing(link, *args):
        link.write_bytes(b'ours')
        (directory / 'ScholarFlow.lnk').write_bytes(b'other')
    with pytest.raises(FileExistsError):
        shortcuts.create_shortcut(target, directory, {}, receipt, native_writer=racing)
    assert (directory / 'ScholarFlow.lnk').read_bytes() == b'other'
    assert len(list(directory.iterdir())) == 1


def test_custom_data_directory_does_not_create_misleading_shortcut(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, 'load_action_trust', lambda *a, **k: pytest.fail('must reject before trust/shortcut'))
    with pytest.raises(ValueError, match='自定义'):
        gui._shortcut(request(tmp_path, shortcut_directory=tmp_path / 'chosen'), {'state': 'committed', 'candidate_id': 'a' * 64})


@pytest.fixture
def signed_paths(tmp_path):
    import base64
    from contextlib import closing
    import sqlite3
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from app.atomic_files import atomic_write_json
    from app.release_files import digest
    from app.release_info import canonical_json
    from app.release_schema import schema_snapshot
    from app import release_install
    private = Ed25519PrivateKey.generate()
    trust = {'entry-test': base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}
    data = tmp_path / 'data'
    data.mkdir()
    with closing(sqlite3.connect(data / 'scholarflow.db')) as db:
        db.executescript("CREATE TABLE samples(value TEXT); INSERT INTO samples VALUES ('unchanged');")
    (data / 'note.md').write_text('真实临时文件，合成资料', encoding='utf-8')
    candidate = tmp_path / 'candidate'
    (candidate / 'payload').mkdir(parents=True)
    (candidate / 'payload/ScholarFlow.exe').write_bytes(b'synthetic-never-execute')
    manifest = {'format': release_install.FORMAT, 'schema_version': 1,
        'definition': json.loads((ROOT / 'release.json').read_text(encoding='utf-8')),
        'input_build_id': 'a' * 64, 'target_schema': schema_snapshot(data / 'scholarflow.db'),
        'entrypoint': 'ScholarFlow.exe', 'files': release_install._files(candidate / 'payload'),
        'qualification': 'assembled_candidate_not_public_release'}
    manifest['candidate_id'] = digest(manifest)
    atomic_write_json(candidate / release_install.MANIFEST, manifest)
    atomic_write_json(candidate / 'release-signature.json', {'algorithm': 'Ed25519', 'key_id': 'entry-test',
        'signature': base64.b64encode(private.sign(canonical_json(manifest))).decode()})
    trust_file = tmp_path / 'trust.json'
    atomic_write_json(trust_file, trust)
    return request(tmp_path, trusted_keys=trust_file), manifest, trust


def test_real_signed_candidate_through_adapter_and_transaction_preserves_data(signed_paths, monkeypatch):
    from app import release_install
    req, manifest, trust = signed_paths
    before = {p.name: p.read_bytes() for p in req.data.iterdir()}
    health = []
    monkeypatch.setattr(release_install, '_health', lambda program, value, stage, receipt: health.append(stage))
    verified = cli.run_offline_action('verify', **req.action_options())
    assert verified['candidate_id'] == manifest['candidate_id']
    installed = cli.run_offline_action('install', **req.action_options(), expected_candidate_id=verified['candidate_id'])
    assert installed['state'] == 'committed' and installed['data_modified'] is False
    assert len(health) == 1 and health[0] != req.data
    assert {p.name: p.read_bytes() for p in req.data.iterdir() if p.name in before} == before
    assert release_install.verify_candidate(req.target, trust)['candidate_id'] == manifest['candidate_id']
    chosen = req.target.parent / 'chosen-shortcuts'
    chosen.mkdir()
    shortcut = shortcuts.create_shortcut(req.target, chosen, trust, installed,
        native_writer=lambda link, *args: link.write_bytes(b'synthetic-link'))
    assert shortcut['state'] == 'created'
    assert cli.run_offline_action('recover', **req.action_options())['state'] == 'committed'


def test_real_signed_candidate_tampering_never_installs(signed_paths):
    req, _, _ = signed_paths
    (req.candidate / 'payload/ScholarFlow.exe').write_bytes(b'tampered')
    with pytest.raises(ValueError):
        cli.run_offline_action('verify', **req.action_options())
    assert not req.target.exists()
