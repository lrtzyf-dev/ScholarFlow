"""分层 AGENTS.md：查看/保存全局规范、查看合并结果、一键生成脚手架。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import agents_md
from ..config import settings

router = APIRouter(prefix="/api/agents-md", tags=["agents-md"])


def _meta() -> dict:
    global_rules = agents_md.load_global_rules()
    project_rules = settings.work_rules or ""
    merged = agents_md.merge_rules(global_rules, project_rules)
    return {
        "global_rules": global_rules,
        "project_rules": project_rules,
        "merged": merged,
        "global_exists": agents_md.GLOBAL_AGENTS_PATH.exists(),
        "global_path": str(agents_md.GLOBAL_AGENTS_PATH),
        "max_bytes": agents_md.MAX_AGENTS_BYTES,
        "global_bytes": agents_md._byte_size(global_rules),
        "project_bytes": agents_md._byte_size(project_rules),
    }


@router.get("")
def get_agents_md() -> dict:
    return _meta()


class GlobalSave(BaseModel):
    global_rules: str


@router.put("")
def put_global(body: GlobalSave) -> dict:
    try:
        agents_md.write_global_rules(body.global_rules)
    except agents_md.AgentsMdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _meta()


class InitRequest(BaseModel):
    overwrite: bool = False


@router.post("/init")
def init(body: InitRequest) -> dict:
    try:
        content, created = agents_md.init_agents_md(overwrite=body.overwrite)
    except agents_md.AgentsMdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"created": created, "global_rules": content, **_meta()}
