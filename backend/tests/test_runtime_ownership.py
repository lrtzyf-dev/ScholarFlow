"""Data-directory ownership, authenticated activation and real process takeover."""
import asyncio
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, run_store, task_runner
from app.api import runtime
from app.runtime_owner import RuntimeInUse, RuntimeOwner, claim_or_activate, read_descriptor


@pytest.fixture
def owned(tmp_path):
    instance = RuntimeOwner(tmp_path / 'owned')
    assert instance.reserve_desktop()
    try:
        yield instance
    finally:
        instance.finish_shutdown(idle=True)


def test_lock_is_per_directory_and_stale_discovery_does_not_hold_it(owned, tmp_path):
    original = read_descriptor(owned.directory)
    same = RuntimeOwner(owned.directory / '..' / 'owned')
    assert not same.reserve_desktop()
    assert read_descriptor(owned.directory) == original
    independent = RuntimeOwner(tmp_path / 'independent')
    assert independent.reserve_desktop()
    independent.finish_shutdown(idle=True)
    owned.finish_shutdown(idle=True)
    (owned.directory / '.runtime.json').write_text(original.model_dump_json())
    assert same.reserve_desktop(), 'a stale discovery document must not prevent takeover'
    assert read_descriptor(same.directory).instance_id != original.instance_id
    same.finish_shutdown(idle=True)


@pytest.mark.parametrize('payload', ['not json', '[]', '{"version":999}', 'x' * 9000])
def test_invalid_discovery_is_untrusted_and_never_authorizes_takeover(owned, payload):
    (owned.directory / '.runtime.json').write_text(payload)
    assert read_descriptor(owned.directory) is None
    contender = RuntimeOwner(owned.directory)
    assert claim_or_activate(contender, timeout=0) == 'busy'
    assert not contender.reserve_desktop()


def test_activation_is_authenticated_and_does_not_run_during_startup_or_shutdown(owned, monkeypatch):
    monkeypatch.setattr(runtime, 'owner', owned)
    client = TestClient(main.app)
    descriptor = read_descriptor(owned.directory)
    headers = {'X-ScholarFlow-Instance': descriptor.token}
    calls = []
    owned.set_activation(lambda: calls.append('show') or True)
    assert client.post('/api/runtime/activate').status_code == 403
    assert client.post('/api/runtime/activate', headers={'X-ScholarFlow-Instance': 'invalid'}).status_code == 403
    assert client.post('/api/runtime/activate', headers=headers).status_code == 503
    owned.begin_service()
    owned.ready()
    result = client.post('/api/runtime/activate', headers=headers)
    assert result.json() == {'activated': True, 'instance_id': descriptor.instance_id}
    assert descriptor.token not in result.text
    owned.stopping()
    assert client.post('/api/runtime/activate', headers=headers).status_code == 503
    assert calls == ['show']


@pytest.mark.parametrize('scenario', ['startup', 'mismatched_instance', 'closing'])
def test_claim_uses_real_loopback_protocol_without_starting_another_backend(owned, scenario):
    calls = []
    def show():
        calls.append('show')
        if scenario == 'closing':
            owned.stopping()
        return True
    class ActivationHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == '/api/runtime/activate'
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            instance_id = owned.activate(self.headers.get('X-ScholarFlow-Instance', ''))
            self.send_response(200 if instance_id else 503)
            self.end_headers()
            body = {'activated': bool(instance_id), 'instance_id': instance_id}
            if scenario == 'mismatched_instance':
                body['instance_id'] = 'f' * 32
            self.wfile.write(json.dumps(body).encode())
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), ActivationHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    owned.set_desktop_port(server.server_port)
    owned.begin_service()
    owned.set_activation(show)
    original = read_descriptor(owned.directory)
    timer = None
    if scenario == 'startup':
        timer = threading.Timer(0.15, owned.ready)
        timer.start()
    else:
        owned.ready()
    contender = RuntimeOwner(owned.directory)
    try:
        result = claim_or_activate(contender, timeout=0.6)
        assert result == ('activated' if scenario == 'startup' else 'busy')
        assert calls
        assert not contender.reserve_desktop()
        assert read_descriptor(owned.directory).instance_id == original.instance_id
    finally:
        if timer is not None:
            timer.join()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_claim_waits_for_lock_release_and_ready_service_is_not_a_desktop(tmp_path):
    service = RuntimeOwner(tmp_path / 'shared')
    service.begin_service()
    service.ready()
    contender = RuntimeOwner(service.directory)
    try:
        assert claim_or_activate(contender, timeout=0) == 'service_busy'
        service.stopping()
        timer = threading.Timer(0.15, lambda: service.finish_shutdown(idle=True))
        timer.start()
        try:
            assert claim_or_activate(contender, timeout=1) == 'primary'
            assert read_descriptor(contender.directory).kind == 'desktop'
        finally:
            timer.join()
    finally:
        contender.finish_shutdown(idle=True)
        service.finish_shutdown(idle=True)


def test_service_guard_precedes_migration_and_recovery(owned, monkeypatch):
    contender = RuntimeOwner(owned.directory)
    monkeypatch.setattr(main, 'owner', contender)
    monkeypatch.setattr(main, 'init_db', lambda: pytest.fail('contender migrated a live database'))
    monkeypatch.setattr(task_runner, 'recover_interrupted_tasks', lambda *a: pytest.fail('contender recovered live tasks'))
    async def scenario():
        with pytest.raises(RuntimeInUse):
            async with main.lifespan(main.app):
                pytest.fail('a second service started')
    asyncio.run(scenario())
    assert read_descriptor(owned.directory).state == 'starting'


def test_startup_failure_releases_directory_and_stopped_workers_keep_lock(tmp_path, monkeypatch):
    instance = RuntimeOwner(tmp_path / 'startup')
    monkeypatch.setattr(main, 'owner', instance)
    monkeypatch.setattr(task_runner, 'shutdown_background_tasks', lambda: None)
    monkeypatch.setattr(task_runner, 'running_task_ids', lambda: [])
    monkeypatch.setattr(run_store, 'active_runs', lambda: [])
    monkeypatch.setattr(run_store, 'cancel_all', lambda: None)
    def failed():
        raise OSError('disk failed')
    monkeypatch.setattr(main, 'init_db', failed)
    async def failure():
        with pytest.raises(OSError, match='disk failed'):
            async with main.lifespan(main.app):
                pass
    asyncio.run(failure())
    replacement = RuntimeOwner(instance.directory)
    assert replacement.reserve_desktop()
    replacement.finish_shutdown(idle=True)
    monkeypatch.setattr(main, 'init_db', lambda: None)
    monkeypatch.setattr(task_runner, 'recover_interrupted_tasks', lambda *a: None)
    monkeypatch.setattr(task_runner, 'running_task_ids', lambda: ['still-executing'])
    async def linger():
        async with main.lifespan(main.app):
            assert read_descriptor(instance.directory).state == 'ready'
    asyncio.run(linger())
    assert read_descriptor(instance.directory).state == 'stopping'
    assert not replacement.reserve_desktop()
    instance.finish_shutdown(idle=True)


def test_desktop_window_activation_preserves_maximized_state_and_waits_until_shown():
    launcher_path = Path(__file__).resolve().parents[2] / 'desktop' / 'launcher.py'
    spec = importlib.util.spec_from_file_location('ownership_launcher', launcher_path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    from types import SimpleNamespace
    class Event:
        def __init__(self): self.handlers, self.shown = [], False
        def __iadd__(self, handler): self.handlers.append(handler); return self
        def is_set(self): return self.shown
        def emit(self):
            for handler in self.handlers: handler()
    calls = []
    events = SimpleNamespace(**{name: Event() for name in ['shown', 'minimized', 'maximized', 'restored']})
    window = SimpleNamespace(events=events, restore=lambda: calls.append('restore'),
                             maximize=lambda: calls.append('maximize'), show=lambda: calls.append('show'))
    activate = launcher.WindowActivation(window)
    assert not activate() and calls == []
    events.shown.shown = True
    assert activate() and calls == ['show']
    calls.clear()
    events.maximized.emit()
    assert activate() and calls == ['show'], 'an already maximized window must not be normalized'
    calls.clear()
    events.minimized.emit()
    assert activate() and calls == ['restore', 'maximize', 'show']
    exited_server = SimpleNamespace(started=False, should_exit=True)
    assert launcher._wait_ready(1, exited_server, timeout=0.1) is False
    dead_thread = SimpleNamespace(is_alive=lambda: False)
    assert launcher._wait_ready(1, SimpleNamespace(started=False, should_exit=False), timeout=0.1, thread=dead_thread) is False


def wait_for(fn, timeout=18):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value: return value
        time.sleep(0.03)
    pytest.fail('process verification timed out')


@pytest.mark.parametrize('stop_mode', ['crash', 'linger'])
def test_real_process_excludes_contender_and_holds_ownership_through_pending_write(tmp_path, stop_mode):
    probe = Path(__file__).with_name('runtime_process_probe.py')
    environment = dict(os.environ, DATA_DIR=str(tmp_path / 'data'), LLM_API_KEY='', EMBEDDING_API_KEY='',
                       REDIS_URL='', DESKTOP_NOTIFICATIONS='false')
    data = Path(environment['DATA_DIR'])
    children, logs = [], []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(port, route, body=None):
        req = urllib.request.Request(f'http://127.0.0.1:{port}{route}',
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=2) as response:
            return json.loads(response.read())
    def start(mode='normal', *, independent=False, ready=True):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        target_env = {**environment, 'DATA_DIR': str(tmp_path / 'other')} if independent else environment
        log_path = tmp_path / f'process-{len(children)}.log'
        output = log_path.open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(probe), str(port), mode],
                                   env=target_env, stdout=output, stderr=subprocess.STDOUT)
        children.append(process)
        logs.append(output)
        if ready:
            def health():
                assert process.poll() is None, log_path.read_text(encoding='utf-8')
                try: return request(port, '/api/health')['status'] == 'ok'
                except OSError: return False
            wait_for(health)
        return process, port, log_path
    try:
        first, port, _ = start('linger' if stop_mode == 'linger' else 'normal')
        task = request(port, '/api/tasks/start', {'question': 'Ownership verification', 'conversation_id': 'c_owner'})
        wait_for(lambda: (data / 'effects.log').exists())
        current = request(port, f'/api/tasks/{task["id"]}')
        assert current['status'] == 'running' and current['recovery']['pending_operation']['kind'] == 'tool'
        descriptor = read_descriptor(data)
        second, _, second_log = start(ready=False)
        second.wait(timeout=15)
        assert second.returncode != 0
        assert 'RuntimeInUse' in second_log.read_text(encoding='utf-8')
        assert read_descriptor(data).instance_id == descriptor.instance_id
        current = request(port, f'/api/tasks/{task["id"]}')
        assert current['status'] == 'running'
        assert not any(e.get('kind') == 'interrupted' for e in current['events'])
        assert (data / 'effects.log').read_text() == 'write\n'
        # Independent data directories remain usable in parallel.
        other, other_port, _ = start(independent=True)
        assert request(other_port, '/api/health')['status'] == 'ok'
        other.terminate()
        other.wait(timeout=5)
        if stop_mode == 'linger':
            request(port, '/_verification/stop', {})
            wait_for(lambda: (data / 'service-stopped').exists(), timeout=12)
            assert first.poll() is None, 'the process must still own its running tool thread'
            assert read_descriptor(data).state == 'stopping'
            third, _, third_log = start(ready=False)
            third.wait(timeout=15)
            assert third.returncode != 0 and 'RuntimeInUse' in third_log.read_text(encoding='utf-8')
        first.terminate()
        first.wait(timeout=5)
        successor, new_port, _ = start()
        assert read_descriptor(data).instance_id != descriptor.instance_id
        recovered = request(new_port, f'/api/tasks/{task["id"]}')
        assert recovered['status'] == 'interrupted'
        assert recovered['recovery']['pending_operation']['kind'] == 'tool'
        assert (data / 'effects.log').read_text() == 'write\n', 'startup must not repeat the uncertain write'
        with pytest.raises(urllib.error.HTTPError) as failure:
            request(new_port, f'/api/tasks/{task["id"]}/resume', {})
        assert failure.value.code == 409
        request(new_port, f'/api/tasks/{task["id"]}/resume', {'resolution': 'skip'})
        finished = wait_for(lambda: (item if item['status'] == 'done' and not item['executing'] else None)
                            if (item := request(new_port, f'/api/tasks/{task["id"]}')) else None)
        assert finished['recovery']['completed_tools'] == 1
        with sqlite3.connect(data / 'scholarflow.db') as connection:
            saved = json.loads(connection.execute('SELECT checkpoint FROM tasks WHERE id = ?', (task['id'],)).fetchone()[0])
        tool_results = [value for key, value in saved['operations'].items() if key.startswith('tool/')]
        assert len(tool_results) == 1
        assert json.loads(tool_results[0]['result'])['execution_unknown'] is True
        assert json.loads(tool_results[0]['result'])['skipped_by_user'] is True
        assert (data / 'effects.log').read_text() == 'write\n'
        assert len(request(new_port, '/api/conversations/c_owner')['messages']) == 1
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        for output in logs:
            output.close()
