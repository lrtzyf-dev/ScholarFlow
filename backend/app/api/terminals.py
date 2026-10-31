"""Same-origin terminal admission and flow-controlled live PTY transport."""
import asyncio
import json
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from .. import terminal_sessions as terminals, workspace
from ..db import get_db, SessionLocal

router = APIRouter(prefix='/api')


def local_origin(connection):
    url = connection.url
    if url.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise HTTPException(403, '终端只接受本机访问')
    origin = connection.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        expected_scheme = 'https' if url.scheme in ('https', 'wss') else 'http'
        if not (parsed.scheme == expected_scheme and parsed.netloc == url.netloc and parsed.path in ('', '/')):
            if origin not in ('http://127.0.0.1:5173', 'http://localhost:5173'):
                raise HTTPException(403, '终端拒绝跨站连接')


class CreateTerminal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,80}$')
    shell: Literal['powershell', 'cmd'] = 'powershell'
    path: str = Field(default='', max_length=1024)
    cols: int = Field(default=100, ge=20, le=300)
    rows: int = Field(default=24, ge=5, le=100)

    @field_validator('path')
    @classmethod
    def scoped_path(cls, value):
        return workspace.relative_path(value)


@router.get('/projects/{project_id}/terminals')
def listing(project_id: str, request: Request, db: Session = Depends(get_db)):
    local_origin(request); workspace.get_project(db, project_id)
    return {'sessions': terminals.listing(project_id)}


@router.post('/projects/{project_id}/terminals')
def create(project_id: str, body: CreateTerminal, request: Request, db: Session = Depends(get_db)):
    local_origin(request)
    return terminals.create(workspace.project_scope(workspace.get_project(db, project_id)), body)


@router.post('/projects/{project_id}/terminals/{session_id}/connect')
def connect(project_id: str, session_id: str, request: Request, db: Session = Depends(get_db)):
    local_origin(request); workspace.get_project(db, project_id)
    session = terminals.get(project_id, session_id)
    return {'token': session.token}


@router.post('/projects/{project_id}/terminals/{session_id}/stop')
def stop(project_id: str, session_id: str, request: Request, db: Session = Depends(get_db)):
    local_origin(request); workspace.get_project(db, project_id)
    session = terminals.get(project_id, session_id)
    session.stop()
    return session.info()


@router.delete('/projects/{project_id}/terminals/{session_id}')
def remove(project_id: str, session_id: str, request: Request, db: Session = Depends(get_db)):
    local_origin(request); workspace.get_project(db, project_id)
    terminals.remove(project_id, session_id)
    return {'removed': True}


class Attach(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(max_length=100)
    after: int = Field(default=0, ge=0)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['input']
    data: str = Field(min_length=1, max_length=4096)

    @field_validator('data')
    @classmethod
    def valid_utf8(cls, value):
        value.encode('utf-8')
        return value


class Resize(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['resize']
    cols: int = Field(ge=20, le=300)
    rows: int = Field(ge=5, le=100)


class Ack(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['ack']
    sequence: int = Field(ge=0)
    response: str = Field(default='', max_length=65536)

    @field_validator('response')
    @classmethod
    def valid_utf8(cls, value):
        value.encode('utf-8')
        return value


@router.websocket('/projects/{project_id}/terminals/{session_id}/stream')
async def stream(ws: WebSocket, project_id: str, session_id: str):
    attached = False
    session = None
    try:
        local_origin(ws)
        with SessionLocal() as db:
            workspace.get_project(db, project_id)
        session = terminals.get(project_id, session_id)
        await ws.accept()
        first = await asyncio.wait_for(ws.receive_text(), timeout=5)
        if len(first) > 512:
            raise ValueError('连接参数过长')
        admission = Attach.model_validate_json(first)
        session.attach(admission.token); attached = True
        replay_until = session.acknowledged
        await ws.send_json({'type': 'replay', 'sequence': replay_until})
        sent = acknowledged = admission.after
        ready = False
        last_status = None
        awaiting_since = None
        while True:
            reset, base, chunks, info = session.snapshot(sent, None if ready else replay_until)
            state = (info['status'], info['exit_code'], info['error'], info['rows'], info['cols'])
            if state != last_status:
                await ws.send_json({'type': 'state', 'session': info}); last_status = state
            if reset and sent == acknowledged:
                await ws.send_json({'type': 'reset', 'sequence': base, 'truncated': True})
                sent = acknowledged = base
                reset, base, chunks, info = session.snapshot(sent, None if ready else replay_until)
            if not ready and acknowledged >= replay_until:
                ready = True
                await ws.send_json({'type': 'ready'})
                reset, base, chunks, info = session.snapshot(sent)
            if sent == acknowledged and chunks:
                sent = chunks[-1][0]
                await ws.send_json({'type': 'output', 'sequence': sent, 'data': ''.join(t for _, t in chunks)})
                awaiting_since = time.monotonic()
            if awaiting_since and time.monotonic() - awaiting_since > 15:
                raise ValueError('终端输出确认超时，请重新连接')
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=.04)
            except asyncio.TimeoutError:
                continue
            if len(raw) > 524288:
                raise ValueError('终端消息过长')
            message = json.loads(raw)
            kind = message.get('type') if isinstance(message, dict) else None
            if kind == 'ack':
                ack = Ack.model_validate(message)
                if ack.sequence != sent or awaiting_since is None:
                    raise ValueError('终端输出确认序号无效')
                if not ready and ack.response:
                    raise ValueError('历史回放不能写入终端')
                session.acknowledge(ack.sequence, ack.response)
                acknowledged = ack.sequence; awaiting_since = None
            elif kind == 'input':
                if not ready:
                    raise ValueError('终端正在恢复输出，请稍后输入')
                session.enqueue('input', Input.model_validate(message).data)
            elif kind == 'resize':
                resize = Resize.model_validate(message)
                session.enqueue('resize', (resize.cols, resize.rows))
            else:
                raise ValueError('终端消息类型无效')
    except WebSocketDisconnect:
        pass
    except (HTTPException, ValidationError, ValueError, asyncio.TimeoutError) as exc:
        try:
            if ws.application_state.name == 'CONNECTED':
                await ws.send_json({'type': 'error', 'message': exc.detail if isinstance(exc, HTTPException) else str(exc)[:200]})
            await ws.close(code=1008)
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        if attached:
            session.detach()
