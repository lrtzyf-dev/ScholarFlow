"""Explicit per-file cloud review and lifecycle actions from the local UI."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from .. import provider_files as store, service_tool_config, source_store
from ..credential_protection import CredentialProtectionError

router = APIRouter(prefix="/api/provider-files", tags=["provider-files"])


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    source_revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    purpose: Literal["user_data", "text_recognition", "rich_layout_recognition"]
    expires_seconds: int = Field(ge=3600, le=2592000, strict=True)
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[\w-]+$")


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")
    expected_version: int = Field(ge=1, strict=True)
    action: Literal["upload", "refresh", "reconcile", "adopt_for_cleanup", "delete", "acknowledge_manual_cleanup"]
    cursor: str | None = Field(default=None, pattern=r"^file-[A-Za-z0-9_-]{1,200}$")
    candidate_id: str | None = Field(default=None, pattern=r"^file-[A-Za-z0-9_-]{1,200}$")
    confirm_manual_cleanup: bool = False


def _call(action):
    try:
        return action()
    except (store.FileBindingError, service_tool_config.ServiceConfigurationError) as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except source_store.SourceError:
        raise HTTPException(404, "当前会话或固定原件不可访问") from None
    except CredentialProtectionError:
        raise HTTPException(503, "系统凭据保护不可用") from None
    except Exception as exc:
        from ..resource_budget import ResourceBudgetExceeded
        from ..request_resources import ResourceRequestCancelled
        if isinstance(exc, ResourceBudgetExceeded):
            raise HTTPException(409, "当前资源预算要求暂停，未继续发送请求") from None
        if isinstance(exc, ResourceRequestCancelled):
            raise HTTPException(409, "当前请求已取消") from None
        raise


@router.get("")
def list_files(conversation_id: str = Query(pattern=r"^[\w-]{1,64}$")):
    return _call(lambda: store.list_bindings(conversation_id))


@router.post("/prepare")
def prepare_file(payload: Prepare):
    return _call(lambda: store.prepare(payload.conversation_id, payload.source_revision_id,
        payload.purpose, payload.expires_seconds, payload.request_id))


@router.post("/{file_id}/actions")
def file_action(file_id: str, payload: Action):
    if payload.action == "upload":
        return _call(lambda: store.upload(payload.conversation_id, file_id, payload.expected_version))
    return _call(lambda: store.operate(payload.conversation_id, file_id, payload.expected_version,
        payload.action, cursor=payload.cursor, candidate_id=payload.candidate_id,
        confirm_manual_cleanup=payload.confirm_manual_cleanup))
