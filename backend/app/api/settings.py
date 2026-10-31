"""模型与设置：运行时读取/修改大模型接口配置，持久化到 config.json。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from ..config import (
    RUNTIME_BOOL_FIELDS,
    RUNTIME_FIELDS,
    runtime_settings,
    settings,
    update_settings,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsIn(BaseModel):
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    approval_mode: str | None = None
    work_rules: str | None = None
    reasoning_effort: str | None = None
    # 布尔开关
    desktop_notifications: bool | None = None
    memory_recall: bool | None = None
    memory_capture: bool | None = None


class TestIn(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None


@router.get("")
def get_settings() -> dict:
    return runtime_settings()


@router.put("")
def put_settings(payload: SettingsIn) -> dict:
    data = payload.model_dump()
    updates = {k: v for k, v in data.items() if v is not None and k in RUNTIME_FIELDS}
    updates.update(
        {k: v for k, v in data.items() if v is not None and k in RUNTIME_BOOL_FIELDS}
    )
    return update_settings(updates)


@router.post("/test")
def test_connection(payload: TestIn) -> dict:
    """用表单中（或已保存的）配置发起一次最小请求，验证连通性与鉴权。"""
    base = (payload.llm_base_url or settings.llm_base_url or "").rstrip("/")
    key = payload.llm_api_key or settings.llm_api_key or ""
    model = payload.llm_model or settings.llm_model or ""
    if not base:
        return {"ok": False, "message": "请先填写接口地址"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(f"{base}/chat/completions", headers=headers, json=body)
        if resp.status_code == 401 or resp.status_code == 403:
            return {"ok": False, "message": "鉴权失败：API Key 无效或已过期"}
        if resp.status_code == 404:
            return {"ok": False, "message": "模型不存在或接口地址错误"}
        if resp.status_code >= 400:
            return {"ok": False, "message": f"接口返回错误（{resp.status_code}）：{resp.text[:120]}"}
        return {"ok": True, "message": "连接成功，模型接口可用"}
    except httpx.ConnectError:
        return {"ok": False, "message": "无法连接到接口地址，请检查网络或地址"}
    except httpx.TimeoutException:
        return {"ok": False, "message": "连接超时，请稍后重试"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"连接失败：{exc}"}
