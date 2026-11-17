"""模型与设置：运行时读取/修改大模型接口配置，持久化到 config.json。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import (
    RUNTIME_BOOL_FIELDS,
    RUNTIME_FIELDS,
    RUNTIME_INT_FIELDS,
    runtime_settings,
    settings,
    update_settings,
)
from ..model_connection import (
    discover_models, network_error_message, normalize_setting, probe_chat, require_key_for_origin_change,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsIn(BaseModel):
    llm_demo_mode: bool | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    approval_mode: str | None = None
    work_rules: str | None = None
    reasoning_effort: str | None = None
    max_agent_steps: int | None = Field(default=None, strict=True, ge=1, le=128)
    # 布尔开关
    desktop_notifications: bool | None = None
    memory_recall: bool | None = None
    memory_capture: bool | None = None


class TestIn(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    reasoning_effort: str | None = None


class ModelsIn(BaseModel):
    llm_base_url: str | None = None
    llm_api_key: str | None = None


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
    updates.update(
        {k: v for k, v in data.items() if v is not None and k in RUNTIME_INT_FIELDS}
    )
    try:
        return update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/models")
def get_models(payload: ModelsIn) -> dict:
    """读取表单地址的模型列表；不会保存配置或消耗对话 token。"""
    try:
        values = {field: normalize_setting(field, value if value is not None else getattr(settings, field))
                  for field, value in payload.model_dump().items()}
        if not values["llm_base_url"]:
            raise ValueError("请先填写接口地址")
        require_key_for_origin_change(settings.llm_base_url, values["llm_base_url"],
                                      settings.llm_api_key, payload.llm_api_key)
        models = discover_models(values["llm_base_url"], values["llm_api_key"])
        if not models:
            return {"ok": False, "message": "接口未返回可用模型，请确认账户权限，或手动填写模型名称", "models": []}
        return {"ok": True, "message": f"已获取 {len(models)} 个模型，请选择后测试连接", "models": models}
    except ValueError as exc:
        message = str(exc)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            message = "获取模型失败：API Key 无效、已过期或无访问权限"
        elif code in (404, 405, 501):
            message = "该接口不支持获取模型列表，或接口地址有误；可手动填写模型名称后测试连接"
        elif code == 402:
            message = "获取模型失败：服务商提示账户余额不足，请检查额度"
        elif code == 429:
            message = "获取模型过于频繁或额度不足，请稍后重试"
        elif 300 <= code < 400:
            message = "接口返回了重定向，请填写最终的模型接口地址后重试"
        else:
            message = f"获取模型失败（HTTP {code}），请检查接口地址或稍后重试；也可手动填写模型名称"
    except httpx.HTTPError as exc:
        message = network_error_message(exc)
    return {"ok": False, "message": message, "models": []}


@router.post("/test")
def test_connection(payload: TestIn) -> dict:
    """用表单中（或已保存的）配置发起一次最小请求，验证连通性与鉴权。"""
    try:
        values = {field: normalize_setting(field, value if value is not None else getattr(settings, field))
                  for field, value in payload.model_dump().items()}
        require_key_for_origin_change(settings.llm_base_url, values["llm_base_url"],
                                      settings.llm_api_key, payload.llm_api_key)
        probe_chat(values["llm_base_url"], values["llm_api_key"], values["llm_model"],
                   values["reasoning_effort"])
        return {"ok": True, "message": "连接成功，模型接口可用"}
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return {"ok": False, "message": "鉴权失败：API Key 无效或已过期"}
        if code == 404:
            return {"ok": False, "message": "模型不存在或接口地址错误"}
        if 300 <= code < 400:
            return {"ok": False, "message": "接口返回了重定向，请填写最终的模型接口地址"}
        return {"ok": False, "message": f"模型接口返回 HTTP {code}，请检查模型名称、参数或服务额度"}
    except httpx.HTTPError as exc:
        return {"ok": False, "message": network_error_message(exc)}
