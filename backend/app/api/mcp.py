"""MCP 服务器管理：增删改查、启停、连接状态、工具发现。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..mcp_bridge import bridge

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get('/servers/{name}/authorization')
def auth_status(name: str):
    try:
        return bridge.user_authorization(name)
    except KeyError:
        raise HTTPException(404, '连接不存在')
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post('/servers/{name}/authorization/{action}')
def auth_action(name: str, action: str):
    try:
        return bridge.user_authorization(name, action)
    except KeyError:
        raise HTTPException(404, '连接不存在')
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class ServerIn(BaseModel):
    name: str
    transport: str = "stdio"  # stdio | http
    enabled: bool = True
    # stdio
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str = ""
    # http
    url: str = ""
    headers: dict[str, str] | None = None
    bearer_token: str | None = None
    oauth: dict[str, str] | None = None


class NameIn(BaseModel):
    name: str


class EnabledIn(BaseModel):
    name: str
    enabled: bool


@router.get("/servers")
def list_servers() -> dict:
    return {"servers": bridge.list_servers()}


@router.put("/servers")
def upsert_server(payload: ServerIn) -> dict:
    # Keep omitted credentials distinct from explicit empty values. An edit
    # that does not include auth fields must preserve the saved credential;
    # an explicit empty value clears it.
    cfg = payload.model_dump(exclude_unset=True)
    try:
        return bridge.upsert_server(cfg)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/servers/connect")
def connect(payload: NameIn) -> dict:
    try:
        return bridge.connect(payload.name)
    except KeyError:
        raise HTTPException(status_code=404, detail="服务器不存在")


@router.post("/servers/disconnect")
def disconnect(payload: NameIn) -> dict:
    try:
        return bridge.disconnect(payload.name)
    except KeyError:
        raise HTTPException(status_code=404, detail="服务器不存在")


@router.post("/servers/enabled")
def set_enabled(payload: EnabledIn) -> dict:
    bridge.set_enabled(payload.name, payload.enabled)
    return {"ok": True}


@router.delete("/servers/{name}")
def remove_server(name: str) -> dict:
    bridge.remove_server(name)
    return {"ok": True}


# ---- 工具级管控策略 ----
class PolicyIn(BaseModel):
    policy: dict


@router.get("/discovered-tools")
def discovered_tools() -> dict:
    """列出当前已连接服务器的全部 MCP 工具（不做策略过滤），供工具级管控界面枚举。"""
    specs = bridge.get_tool_specs()
    return {
        "tools": [
            {"name": s["name"], "description": s["description"],
             "server": s.get("_mcp_server", "")}
            for s in specs
        ]
    }


@router.get("/policy")
def get_policy() -> dict:
    from ..mcp_policy import load_policy

    return {"policy": load_policy()}


@router.put("/policy")
def put_policy(payload: PolicyIn) -> dict:
    from ..mcp_policy import save_policy

    return {"policy": save_policy(payload.policy or {})}
