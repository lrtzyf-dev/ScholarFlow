"""生命周期钩子：查看/保存配置、列出支持事件、手动测试触发。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import hooks_store

router = APIRouter(prefix="/api/hooks", tags=["hooks"])


class HooksSave(BaseModel):
    config: dict


@router.get("/events")
def get_events() -> dict:
    return {"events": hooks_store.HOOK_EVENTS,
            "decision_events": sorted(hooks_store.DECISION_EVENTS)}


@router.get("")
def get_hooks() -> dict:
    return {"config": hooks_store.load_hooks()}


@router.put("")
def put_hooks(body: HooksSave) -> dict:
    config = body.config or {}
    bad = [e for e in config if e not in hooks_store.HOOK_EVENTS]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知事件：{', '.join(bad)}")
    hooks_store.save_hooks(config)
    return {"config": hooks_store.load_hooks()}


class HookTest(BaseModel):
    event: str
    payload: dict | None = None
    matcher_value: str | None = None


@router.post("/test")
def test_hook(body: HookTest) -> dict:
    if body.event not in hooks_store.HOOK_EVENTS:
        raise HTTPException(status_code=400, detail=f"未知事件：{body.event}")
    return hooks_store.run_hooks(
        body.event, payload=body.payload, matcher_value=body.matcher_value
    )
