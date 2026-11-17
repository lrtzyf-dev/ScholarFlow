"""Container admission contracts. Fake daemon replies are never OS isolation proof."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import execution_environments as env, project_commands as commands, workspace
from app.db import SessionLocal
from app.main import app
from app.models import Conversation, DeletedConversation, ProjectCommand
from app.timeutil import utcnow

IMAGE = 'example/research@sha256:' + 'a' * 64
POLICY = {'mode': 'container', 'image': IMAGE, 'write_path': 'output'}


@pytest.fixture
def scope(db, tmp_path):
    root = tmp_path / '容器项目'; root.mkdir(); (root / 'output').mkdir()
    item = workspace.register(db, str(root))
    db.add(Conversation(id='container-chat', project_id=item.id)); db.commit()
    return workspace.project_scope(item)


def test_policy_defaults_cas_and_invalid_values(db, scope):
    assert env.snapshot_policy(db, 'container-chat') == {'revision': 0, **env.DEFAULT}
    first = env.save_policy(db, 'container-chat', POLICY, 0)
    assert first == {'revision': 1, **POLICY}
    with pytest.raises(HTTPException) as error:
        env.save_policy(db, 'container-chat', {'mode': 'host'}, 0)
    assert error.value.status_code == 409
    for bad in ({'mode': 'container', 'image': 'ubuntu:latest'}, POLICY | {'write_path': '../bad'},
                POLICY | {'network': 'host'}, POLICY | {'mode': 3}):
        with pytest.raises(HTTPException): env.validate(bad)
    second = env.save_policy(db, 'container-chat', {'mode': 'host'}, 1)
    with pytest.raises(HTTPException): env.verify_current(db, 'container-chat', first)
    assert second['mode'] == 'host'


def test_policy_http_deleted_and_archived(db, scope):
    client=TestClient(app)
    saved=client.put('/api/conversation-execution-environments/container-chat', json={**POLICY,'expected_revision':0})
    assert saved.status_code == 200, saved.text
    db.get(Conversation,'container-chat').archived_at=utcnow(); db.commit()
    assert client.put('/api/conversation-execution-environments/container-chat', json={**POLICY,'expected_revision':1}).status_code == 409
    db.add(DeletedConversation(id='deleted-container')); db.commit()
    assert client.get('/api/conversation-execution-environments/deleted-container').status_code == 410


def test_fixed_boundary_no_download_network_or_credentials(scope, monkeypatch):
    monkeypatch.setattr(env,'binary',lambda:'verified-docker.exe')
    monkeypatch.setattr(env,'diagnostics',lambda _: {'available':True,'image_ready':True})
    argv=env.command_argv(POLICY,scope,'','echo hello','b'*32)
    assert argv[:3] == ['verified-docker.exe','--host',env.DAEMON]
    for flag,value in [('--pull','never'),('--network','none'),('--user','65534:65534'),
                       ('--cap-drop','ALL'),('--security-opt','no-new-privileges=true'),
                       ('--memory','1g'),('--pids-limit','128'),('--log-driver','none')]:
        assert argv[argv.index(flag)+1] == value
    assert '--read-only' in argv and '--privileged' not in argv and '--env-file' not in argv
    mounts=[argv[n+1] for n,x in enumerate(argv) if x=='--mount']
    assert len(mounts)==2 and mounts[0].endswith('target=/workspace,readonly,bind-recursive=readonly')
    assert mounts[1].endswith('target=/outputs,bind-recursive=disabled')
    assert argv[-5:] == ['--entrypoint','/bin/sh',IMAGE,'-lc','echo hello']
    assert 'HTTP_PROXY=' in argv and 'https_proxy=' in argv
    monkeypatch.setattr(env,'diagnostics',lambda _: {'available':False,'image_ready':False,'message':'not ready'})
    with pytest.raises(HTTPException): env.command_argv(POLICY,scope,'','echo hello','b'*32)


def test_unavailable_environment_never_launches_host(db,scope,monkeypatch):
    env.save_policy(db,'container-chat',POLICY,0)
    row=commands.prepare(scope,'container-chat',{'command':'touch /outputs/must-not-exist','summary':'isolated'},
        run_id='container-run',operation_key='container-op')
    monkeypatch.setattr(env,'diagnostics',lambda _: {'available':False,'image_ready':False,'message':'not ready'})
    monkeypatch.setattr(commands,'_powershell',lambda _: pytest.fail('must not fall back to host'))
    monkeypatch.setattr(commands.subprocess,'Popen',lambda *a,**k: pytest.fail('must not start a process'))
    result=commands.run(row['id'],run_id='container-run')
    assert result['status']=='conflict' and result['pid'] is None
    assert not (Path(scope['root_path'])/'output/must-not-exist').exists()


def test_changed_policy_stops_unsent_command(db,scope,monkeypatch):
    row=commands.prepare(scope,'container-chat',{'command':'echo no','summary':'stale'},run_id='container-run',operation_key='stale')
    env.save_policy(db,'container-chat',POLICY,0)
    monkeypatch.setattr(commands.subprocess,'Popen',lambda *a,**k: pytest.fail('must not start stale command'))
    result=commands.run(row['id'],run_id='container-run')
    assert result['status']=='conflict' and '环境已改变' in result['error']


def test_cleanup_exact_owner_and_daemon_absence(monkeypatch):
    calls=[]
    def cli(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout=json.dumps({'scholarflow.command':'b'*32,'scholarflow.project':'project'}),stderr='')
    monkeypatch.setattr(env,'_cli',cli)
    assert env.cleanup('b'*32,'project') and calls[-1][:3]==['container','rm','--force']
    calls.clear()
    assert not env.cleanup('b'*32,'another') and len(calls)==1
    monkeypatch.setattr(env,'_cli',lambda *a,**k: SimpleNamespace(returncode=1,stdout='',stderr='engine unavailable'))
    assert not env.cleanup('b'*32,'project')
    def absent(args,**kwargs):
        if args[0]=='info': return SimpleNamespace(returncode=0,stdout='linux\n',stderr='')
        return SimpleNamespace(returncode=1,stdout='',stderr='Error: No such container: sf-command-'+ 'b'*32)
    monkeypatch.setattr(env,'_cli',absent)
    assert env.cleanup('b'*32,'project')
    assert not env.cleanup('../not-owned','project')


def test_interrupted_container_recovery_and_retry_require_cleanup(db,scope,monkeypatch):
    env.save_policy(db,'container-chat',POLICY,0)
    args={'command':'echo one','summary':'one'}
    row=commands.prepare(scope,'container-chat',args,run_id='container-run',operation_key='once')
    saved=db.get(ProjectCommand,row['id']); saved.status='running'; db.commit()
    monkeypatch.setattr(env,'cleanup',lambda *a:False)
    commands.recover_interrupted()
    db.expire_all()
    assert db.get(ProjectCommand,row['id']).status=='interrupted'
    with pytest.raises(HTTPException):
        commands.prepare(scope,'container-chat',args,run_id='container-run',operation_key='once',retry=True)
    monkeypatch.setattr(env,'cleanup',lambda *a:True)
    retry=commands.prepare(scope,'container-chat',args,run_id='container-run',operation_key='once',retry=True)
    assert retry['attempt']==2 and retry['previous_id']==row['id']


def test_diagnostics_is_local_and_never_pulls(monkeypatch):
    calls=[]
    def cli(args,**kwargs):
        calls.append(args)
        value={'Os':'linux','Version':'29.0'} if args[0]=='version' else {'Os':'linux','RepoDigests':[IMAGE]}
        return SimpleNamespace(returncode=0,stdout=json.dumps(value),stderr='')
    monkeypatch.setattr(env,'_cli',cli)
    value=env.diagnostics(IMAGE)
    assert value['available'] and value['image_ready'] and not value['image_pulled'] and not value['host_fallback']
    assert [c[0] for c in calls]==['version','image']
