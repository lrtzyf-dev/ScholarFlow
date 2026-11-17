"""No-cost configuration API for independent, explicitly authorized services."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..credential_protection import CredentialProtectionError
from .. import service_tool_config as store


class _ProtectedInputRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def protected(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = [{key: item[key] for key in ("loc", "msg", "type") if key in item} for item in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": errors})
        return protected


router = APIRouter(tags=["service-tools"], route_class=_ProtectedInputRoute)


class ConfigurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    enabled_services: list[Literal["search", "fetch", "ocr", "research", "files"]] = Field(max_length=5)
    api_key: SecretStr | None = None


def _raise(exc):
    if isinstance(exc, CredentialProtectionError):
        raise HTTPException(503, "系统凭证保护不可用，请检查当前 Windows 用户环境") from exc
    raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/api/service-tools/deepseek")
def get_configuration(db: Session = Depends(get_db)):
    return store.configuration_snapshot(db)


@router.put("/api/service-tools/deepseek")
def put_configuration(payload: ConfigurationInput, db: Session = Depends(get_db)):
    values = payload.model_dump(exclude_unset=True)
    if "api_key" in values:
        values["api_key"] = values["api_key"].get_secret_value() if values["api_key"] is not None else None
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        result = store.save_configuration(db, values)
        db.commit()
        return result
    except (store.ServiceConfigurationError, CredentialProtectionError) as exc:
        db.rollback()
        _raise(exc)


@router.delete("/api/service-tools/deepseek/credential")
def delete_credential(expected_version: int = Query(ge=0), db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        result = store.revoke_credential(db, expected_version)
        db.commit()
        return result
    except store.ServiceConfigurationError as exc:
        db.rollback()
        _raise(exc)
