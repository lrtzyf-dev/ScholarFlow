"""Opt-in local Linux containers, separate from approval and host execution.

No image is pulled and no fallback launches a host shell. Only a fixed local
Docker Desktop daemon is accepted; model arguments cannot choose an image,
mount, socket, network, capability or user identity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from fastapi import HTTPException
from sqlalchemy import Integer, JSON, String, update
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class ExecutionEnvironmentPolicy(Base):
    __tablename__ = 'conversation_execution_environments'
    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    policy: Mapped[dict] = mapped_column(JSON)


DEFAULT = {'mode':'host','image':'','write_path':''}
DAEMON = 'npipe:////./pipe/dockerDesktopLinuxEngine'
IMAGE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9./:_-]{0,230}@sha256:[a-f0-9]{64}$')
NAME = re.compile(r'^[a-f0-9]{32}$')


def validate(policy):
    if not isinstance(policy, dict) or set(policy) - set(DEFAULT):
        raise HTTPException(422,'执行环境配置不正确')
    value = {**DEFAULT, **policy}
    if value['mode'] not in {'host','container'} or any(not isinstance(v,str) for v in value.values()):
        raise HTTPException(422,'请选择本机或受限容器环境')
    if value['mode'] == 'container' and not IMAGE.fullmatch(value['image']):
        raise HTTPException(422,'受限环境须指定已在本机准备的镜像及 sha256 摘要，例如 registry/image@sha256:…')
    from .workspace import relative_path
    value['write_path'] = relative_path(value['write_path'])
    return value


def snapshot_policy(db, cid):
    row = db.get(ExecutionEnvironmentPolicy,cid) if cid else None
    return {'revision':row.revision,**row.policy} if row else {'revision':0,**DEFAULT}


def save_policy(db,cid,policy,expected_revision):
    from .conversation_store import check_conversation_available, ConversationDeleted, ConversationArchived
    try: check_conversation_available(db,cid)
    except ConversationDeleted as exc: raise HTTPException(410,str(exc)) from None
    except ConversationArchived as exc: raise HTTPException(409,str(exc)) from None
    value=validate(policy)
    current=snapshot_policy(db,cid)
    if current['revision'] != expected_revision:
        raise HTTPException(409,'执行环境已在其他窗口更新，请重新加载')
    row=db.get(ExecutionEnvironmentPolicy,cid)
    if row is None:
        db.add(ExecutionEnvironmentPolicy(conversation_id=cid,revision=1,policy=value))
    else:
        changed=db.execute(update(ExecutionEnvironmentPolicy).where(
            ExecutionEnvironmentPolicy.conversation_id==cid,ExecutionEnvironmentPolicy.revision==expected_revision)
            .values(revision=expected_revision+1,policy=value)).rowcount
        if changed != 1: raise HTTPException(409,'执行环境已更新，请重新加载')
    db.commit()
    return snapshot_policy(db,cid)


def verify_current(db,cid,frozen):
    actual=snapshot_policy(db,cid)
    if actual != (frozen or {'revision':0,**DEFAULT}):
        raise HTTPException(409,'会话执行环境已改变，此命令未启动；请重新审阅后运行')
    validate({key:actual[key] for key in DEFAULT})
    return actual


def binary():
    if os.name != 'nt':
        raise HTTPException(409,'当前发行版的受限环境使用 Windows Docker Desktop Linux 引擎')
    path=Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Docker/Docker/resources/bin/docker.exe'
    if not path.is_file() or path.is_symlink():
        raise HTTPException(409,'未找到 Docker Desktop。请安装并启动 Linux 引擎，或在会话中明确选择本机执行')
    return str(path)


def _cli(arguments,*,timeout=5):
    return subprocess.run([binary(),'--host',DAEMON,*arguments],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x08000000,timeout=timeout,
        encoding='utf-8',errors='replace')


def diagnostics(image=''):
    if image and not IMAGE.fullmatch(image): raise HTTPException(422,'镜像须包含完整 sha256 摘要')
    result={'available':False,'engine':'docker_desktop_linux','network_access':False,'image_pulled':False,
            'shell':'Linux /bin/sh','project_mount':'readonly','writable_output':'configured_subdirectory_only',
            'host_fallback':False,'image_ready':False}
    try:
        response=_cli(['version','--format','{{json .Server}}'])
        if response.returncode: raise ValueError('engine_not_ready')
        server=json.loads(response.stdout)
        if server.get('Os') != 'linux': raise ValueError('linux_engine_required')
        result.update(available=True,server_version=server.get('Version',''))
        if image:
            inspected=_cli(['image','inspect','--format','{{json .}}',image])
            if inspected.returncode==0:
                item=json.loads(inspected.stdout)
                result['image_ready']=item.get('Os')=='linux' and image in (item.get('RepoDigests') or [])
        result['message']='本机 Linux 容器引擎可用。镜像须另行准备，本程序不会自动下载。' if not image or result['image_ready'] else '引擎可用，但指定摘要镜像尚未在本机准备。'
    except (HTTPException,OSError,ValueError,subprocess.TimeoutExpired) as exc:
        result['message']=exc.detail if isinstance(exc,HTTPException) else 'Docker Desktop Linux 引擎尚未就绪，请启动后重新检查。'
    return result


def boundary(policy):
    if (policy or {}).get('mode') != 'container':
        return '当前用户权限的本机 PowerShell；初始目录受项目校验，进程不受项目沙箱限制'
    return '本机 Linux 容器 /bin/sh；网络关闭、项目只读、非 root、移除全部 capabilities；仅所选输出目录可写，资源受限'


def command_argv(policy,scope,path,command,command_id):
    from . import workspace
    if not NAME.fullmatch(command_id): raise HTTPException(422,'命令身份无效')
    validate({key:policy.get(key,DEFAULT[key]) for key in DEFAULT})
    if policy.get('mode') != 'container': raise HTTPException(422,'未选择容器执行环境')
    state=diagnostics(policy['image'])
    if not state['available'] or not state['image_ready']:
        raise HTTPException(409,state['message'])
    root=workspace.root_path(scope)
    workspace.scoped_path(scope,path)
    if ',' in str(root): raise HTTPException(409,'容器挂载暂不支持带逗号的项目路径，请选择其他项目目录')
    argv=[binary(),'--host',DAEMON,'run','--rm','--pull','never','--name','sf-command-'+command_id,
        '--label','scholarflow.command='+command_id,'--label','scholarflow.project='+scope['project_id'],
        '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges=true',
        '--log-driver','none',
        '--pids-limit','128','--memory','1g','--memory-swap','1g','--cpus','2',
        '--user','65534:65534','--ipc','private','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=67108864',
        '--mount','type=bind,source='+str(root)+',target=/workspace,readonly,bind-recursive=readonly',
        '--workdir','/workspace'+('/'+path if path else ''),'--env','HOME=/tmp','--env','PYTHONIOENCODING=utf-8',
        '--env','PYTHONUNBUFFERED=1']
    # Docker client proxy defaults must not inject host credentials into images.
    for name in ('HTTP_PROXY','HTTPS_PROXY','FTP_PROXY','NO_PROXY','ALL_PROXY',
                 'http_proxy','https_proxy','ftp_proxy','no_proxy','all_proxy'):
        argv.extend(['--env',name+'='])
    if policy.get('write_path'):
        output=workspace.scoped_path(scope,policy['write_path'])
        if not output.is_dir() or ',' in str(output): raise HTTPException(409,'容器输出目录须为项目中已存在、无逗号的独立目录')
        argv.extend(['--mount','type=bind,source='+str(output)+',target=/outputs,bind-recursive=disabled'])
    argv.extend(['--entrypoint','/bin/sh',policy['image'],'-lc',command])
    return argv


def cleanup(command_id,project_id):
    """Never kill a container unless its exact owner labels match this journal."""
    if not NAME.fullmatch(command_id): return False
    name='sf-command-'+command_id
    try:
        response=_cli(['container','inspect','--format','{{json .Config.Labels}}',name])
        if response.returncode:
            # Daemon failure is not proof of absence.
            alive=_cli(['info','--format','{{.OSType}}'])
            return alive.returncode==0 and alive.stdout.strip()=='linux' and 'No such' in response.stderr
        labels=json.loads(response.stdout)
        if labels.get('scholarflow.command')!=command_id or labels.get('scholarflow.project')!=project_id:
            return False
        deleted=_cli(['container','rm','--force',name],timeout=8)
        return deleted.returncode==0
    except (HTTPException,OSError,ValueError,subprocess.TimeoutExpired):
        return False
