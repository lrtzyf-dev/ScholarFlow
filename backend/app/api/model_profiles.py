"""Multi-model catalog and conversation-local composer options."""
from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import model_profiles as store, task_events
from ..credential_protection import CredentialProtectionError
from ..db import get_db
from ..model_connection import discover_models, network_error_message, normalize_base_url, probe_chat, single_line
from ..models import ModelCredential, ModelProfile
from ..timeutil import utcnow

class _ProtectedInputRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def protected(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default detail includes the original input. Never
                # echo a submitted API key, including malformed field types.
                errors = [{key: error[key] for key in ("loc", "msg", "type") if key in error}
                          for error in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": errors})
        return protected


router = APIRouter(tags=["model-profiles"], route_class=_ProtectedInputRoute)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileCreate(StrictInput):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    model_id: str = Field(min_length=1, max_length=256)
    api_key: SecretStr | None = None
    reasoning_effort: str = Field(default="", max_length=20)
    context_window_tokens: int = Field(default=131072, ge=1, le=16777216, strict=True)
    max_output_tokens: int = Field(default=8192, ge=1, le=1048576, strict=True)
    enabled: bool = True


class ProfileUpdate(StrictInput):
    expected_version: int = Field(ge=1, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model_id: str | None = Field(default=None, min_length=1, max_length=256)
    api_key: SecretStr | None = None
    reasoning_effort: str | None = Field(default=None, max_length=20)
    context_window_tokens: int | None = Field(default=None, ge=1, le=16777216, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1048576, strict=True)
    enabled: bool | None = None


class DefaultInput(StrictInput):
    model_profile_id: str | None = Field(default=None, max_length=32)


class CapabilityInput(StrictInput):
    capability: Literal["text", "streaming", "tools", "vision", "reasoning", "structured", "usage", "native_tools", "files"]
    expected_config: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    diagnostic_id: str | None = Field(default=None, pattern=r"^capability-[a-f0-9]{32}$")


class DiscoverInput(StrictInput):
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))


class OptionsInput(StrictInput):
    expected_revision: int = Field(ge=0, strict=True)
    model_profile_id: str | None = Field(default=None, max_length=32)
    approval_mode: Literal["always", "auto", "full_access"] | None = None
    reasoning_effort: str | None = Field(default=None, max_length=20)


def _data(payload: BaseModel) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if "api_key" in values and values["api_key"] is not None:
        values["api_key"] = values["api_key"].get_secret_value()
    return values


def _raise(exc: Exception):
    if isinstance(exc, store.ModelConfigurationError):
        raise HTTPException(exc.status_code, str(exc)) from exc
    if isinstance(exc, CredentialProtectionError):
        raise HTTPException(503, str(exc)) from exc
    raise HTTPException(422, str(exc)) from exc


def _failure(exc: Exception, *, models: bool = False) -> dict:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            message = "认证失败，请检查该模型的密钥和访问权限"
        elif 300 <= code < 400:
            message = "接口返回重定向，请填写最终地址"
        elif code == 429:
            message = "接口限流或额度不足，请稍后重试"
        else:
            message = f"模型接口返回HTTP {code}，请检查地址、模型ID、参数及服务额度"
    elif isinstance(exc, httpx.HTTPError):
        message = network_error_message(exc)
    else:
        # Validation helpers only produce authored errors, never response bodies.
        message = str(exc)
    result = {"ok": False, "message": message}
    if models:
        result["models"] = []
    return result


@router.get("/api/model-profiles")
def list_profiles(db: Session = Depends(get_db)):
    profiles = db.query(ModelProfile).filter(ModelProfile.deleted_at.is_(None)).order_by(
        ModelProfile.created_at, ModelProfile.id).all()
    return {"profiles": [store.legacy_profile(), *(store.profile_snapshot(row) for row in profiles), store.demo_profile()],
            "default_profile_id": store.default_profile_id(db)}


@router.post("/api/model-profiles")
def create_profile(payload: ProfileCreate, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        row = store.save_profile(db, _data(payload))
        result = store.profile_snapshot(row)
        db.commit()
        return result
    except (ValueError, CredentialProtectionError) as exc:
        db.rollback()
        _raise(exc)


@router.put("/api/model-profiles/default")
def change_default(payload: DefaultInput, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        store.set_default(db, payload.model_profile_id)
        db.commit()
        return {"default_profile_id": payload.model_profile_id or store.LEGACY_PROFILE_ID}
    except ValueError as exc:
        db.rollback()
        _raise(exc)


@router.post("/api/model-profiles/discover")
def discover_unsaved(payload: DiscoverInput):
    """New-provider discovery never falls back to the legacy saved credential."""
    try:
        base = normalize_base_url(payload.base_url)
        key = single_line(payload.api_key.get_secret_value(), "API Key")
        values = discover_models(base, key)
        return {"ok": bool(values), "message": f"已获取{len(values)}个模型" if values else "接口没有返回模型，可手动填写模型ID",
                "models": values}
    except (ValueError, httpx.HTTPError) as exc:
        return _failure(exc, models=True)


def _editable(db: Session, profile_id: str, *, include_deleted: bool = False) -> ModelProfile:
    if profile_id == store.DEMO_PROFILE_ID:
        raise store.ModelConfigurationError("离线演示是内置示例，请选择其他模型以使用真实服务", 422)
    if profile_id == store.LEGACY_PROFILE_ID:
        raise store.ModelConfigurationError("原有默认配置请在原设置表单管理，或添加独立模型档案", 422)
    # Soft deletion keeps admitted runs' credentials valid. Explicit revocation
    # must remain available for those runs even after the catalog entry is gone.
    if include_deleted:
        row = db.get(ModelProfile, profile_id)
        if row is not None:
            return row
    return store.get_profile(db, profile_id)


@router.put("/api/model-profiles/{profile_id}")
def update_profile(payload: ProfileUpdate, profile_id: str = Path(max_length=32), db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        row = _editable(db, profile_id)
        if row.version != payload.expected_version:
            raise HTTPException(409, {"message": "模型配置已改变，请刷新后重试", "current": store.profile_snapshot(row)})
        fields = {key: value for key, value in _data(payload).items() if key != "expected_version"}
        if any(value is None for key, value in fields.items() if key != "api_key"):
            raise store.ModelConfigurationError("模型配置字段不能为null", 422)
        row = store.save_profile(db, fields, row)
        result = store.profile_snapshot(row)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        _raise(exc)


@router.delete("/api/model-profiles/{profile_id}")
def delete_profile(profile_id: str = Path(max_length=32), expected_version: int = Query(ge=1),
                   db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        row = _editable(db, profile_id)
        if row.version != expected_version:
            raise HTTPException(409, "模型配置已改变，请刷新后重试")
        row.deleted_at, row.enabled, row.version = utcnow(), 0, row.version + 1
        db.commit()
        # Existing runs retain their version/key; future selections must be explicit.
        return {"deleted": profile_id, "default_profile_id": store.default_profile_id(db)}
    except ValueError as exc:
        db.rollback()
        _raise(exc)


@router.post("/api/model-profiles/{profile_id}/revoke")
def revoke_credentials(profile_id: str = Path(max_length=32), db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        row = _editable(db, profile_id, include_deleted=True)
        db.query(ModelCredential).filter(ModelCredential.profile_id == row.id, ModelCredential.revoked_at.is_(None)).update(
            {ModelCredential.revoked_at: utcnow()}, synchronize_session=False)
        row.enabled, row.version = 0, row.version + 1
        db.commit()
        return {"revoked": profile_id}
    except ValueError as exc:
        db.rollback()
        _raise(exc)


def _profile_values(db: Session, profile_id: str) -> dict:
    if profile_id == store.DEMO_PROFILE_ID:
        raise store.ModelConfigurationError("离线演示不连接远端模型，请选择真实模型后测试", 422)
    if profile_id == store.LEGACY_PROFILE_ID:
        from ..config import settings
        return {"base_url": settings.llm_base_url, "api_key": settings.llm_api_key,
                "model_id": settings.llm_model, "reasoning_effort": settings.reasoning_effort,
                **store.context_limits()}
    row = store.get_profile(db, profile_id, usable=True)
    return {"base_url": row.base_url, "api_key": store.read_credential(db, row.credential_ref),
            "model_id": row.model_id, "reasoning_effort": row.reasoning_effort,
            **store.context_limits({"context_window_tokens": row.context_window_tokens, "max_output_tokens": row.max_output_tokens})}


def _authorizer(profile_id, frozen):
    def check():
        from ..db import SessionLocal
        try:
            with SessionLocal() as current_db:
                return _profile_values(current_db, profile_id) == frozen
        except (ValueError, CredentialProtectionError):
            return False
    return check


@router.post("/api/model-profiles/{profile_id}/models")
def discover_saved(profile_id: str = Path(max_length=32), db: Session = Depends(get_db)):
    try:
        config = _profile_values(db, profile_id)
        values = discover_models(config["base_url"], config["api_key"], authorization_check=_authorizer(profile_id, config))
        return {"ok": bool(values), "message": f"已获取{len(values)}个模型" if values else "接口没有返回模型，可手动填写模型ID",
                "models": values}
    except (ValueError, httpx.HTTPError) as exc:
        return _failure(exc, models=True)


@router.post("/api/model-profiles/{profile_id}/test")
def test_saved(profile_id: str = Path(max_length=32), db: Session = Depends(get_db)):
    try:
        config = _profile_values(db, profile_id)
        version = db.get(ModelProfile, profile_id).version if profile_id != store.LEGACY_PROFILE_ID else None
        probe_chat(config["base_url"], config["api_key"], config["model_id"], config["reasoning_effort"],
                   context_window_tokens=config["context_window_tokens"], max_output_tokens=config["max_output_tokens"],
                   authorization_check=_authorizer(profile_id, config))
        capabilities = store.unknown_capabilities()
        if version is not None:
            # A text-only probe must not erase independently verified features.
            db.expire_all()
            current = db.get(ModelProfile, profile_id)
            if current is None or current.version != version:
                return {"ok": False, "message": "测试期间模型配置已变化，旧配置结果未写入当前模型", "capabilities": capabilities}
            capabilities.update(current.capabilities or {})
            capabilities["text"] = "supported"
            db.query(ModelProfile).filter(ModelProfile.id == profile_id, ModelProfile.version == version).update(
                {ModelProfile.capabilities: capabilities}, synchronize_session=False)
            db.commit()
        else:
            capabilities["text"] = "supported"
        return {"ok": True, "message": "基础对话连接成功；其他能力请查看独立诊断结果", "capabilities": capabilities}
    except (ValueError, httpx.HTTPError) as exc:
        return {**_failure(exc), "capabilities": store.unknown_capabilities()}


@router.get("/api/conversation-options/{conversation_id}")
def get_options(conversation_id: str = Path(min_length=1, max_length=64, pattern=r"^[\w-]+$"),
                db: Session = Depends(get_db)):
    try:
        return store.options_snapshot(db, conversation_id)
    except ValueError as exc:
        _raise(exc)


@router.get("/api/model-profiles/{profile_id}/capabilities")
def capability_preview(profile_id: str = Path(max_length=32)):
    from ..model_capabilities import preview
    try:
        return preview(profile_id)
    except (ValueError, CredentialProtectionError) as exc:
        _raise(exc)


@router.post("/api/model-profiles/{profile_id}/capabilities")
def capability_probe(payload: CapabilityInput, profile_id: str = Path(max_length=32)):
    from ..model_capabilities import run
    try:
        return run(profile_id, payload.capability, payload.expected_config, payload.diagnostic_id)
    except (ValueError, CredentialProtectionError) as exc:
        _raise(exc)


@router.post("/api/model-profiles/{profile_id}/capabilities/{diagnostic_id}/cancel")
def cancel_capability(profile_id: str = Path(max_length=32),
                      diagnostic_id: str = Path(pattern=r"^capability-[a-f0-9]{32}$")):
    from ..model_capabilities import cancel
    try:
        return cancel(profile_id, diagnostic_id)
    except (ValueError, CredentialProtectionError) as exc:
        _raise(exc)


@router.post("/api/model-profiles/{profile_id}/capabilities/{diagnostic_id}/cleanup")
def cleanup_capability(profile_id: str = Path(max_length=32),
                       diagnostic_id: str = Path(pattern=r"^capability-[a-f0-9]{32}$")):
    from ..model_capabilities import cleanup
    from ..providers.deepseek_files import FilesError
    try:
        return cleanup(profile_id, diagnostic_id)
    except FilesError:
        raise HTTPException(409, "远端清理未确认，未自动重试；请核对调用回执或供应商控制台") from None
    except (ValueError, CredentialProtectionError) as exc:
        _raise(exc)


@router.put("/api/conversation-options/{conversation_id}")
def put_options(payload: OptionsInput,
                conversation_id: str = Path(min_length=1, max_length=64, pattern=r"^[\w-]+$"),
                db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        fields = {key: value for key, value in _data(payload).items() if key != "expected_revision"}
        if "approval_mode" in fields and fields["approval_mode"] is None:
            raise store.ModelConfigurationError("请选择有效审批方式", 422)
        result = store.update_options(db, conversation_id, fields, payload.expected_revision)
        db.commit()
        task_events.notify_tasks_changed()
        return result
    except store.ModelConfigurationError as exc:
        db.rollback()
        if exc.status_code == 409:
            raise HTTPException(409, {"message": str(exc), "current": store.options_snapshot(db, conversation_id)}) from exc
        _raise(exc)
    except ValueError as exc:
        db.rollback()
        _raise(exc)
