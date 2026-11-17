"""Versioned settings for dedicated DeepSeek Search / Fetch / OCR services."""
from __future__ import annotations

from .credential_protection import protect, unprotect
from .providers.deepseek_tools import ENDPOINTS
from .service_tool_models import ServiceToolConfiguration, ServiceToolCredential
from .timeutil import utcnow

SERVICE_ENDPOINTS = {**ENDPOINTS, "research": "https://api.deepseek.com/v1/chat/completions", "files": "https://api.deepseek.com/files"}
SERVICES = frozenset(SERVICE_ENDPOINTS)


class ServiceConfigurationError(ValueError):
    def __init__(self, message, status_code=422):
        self.status_code = status_code
        super().__init__(message)


def _key(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or not value.strip().isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value.strip())):
        raise ServiceConfigurationError("独立工具密钥必须是非空的单行凭据；清除密钥请使用撤销操作")
    return value.strip()


def configuration_snapshot(db):
    row = db.get(ServiceToolConfiguration, "deepseek", populate_existing=True)
    credential = db.get(ServiceToolCredential, row.credential_ref, populate_existing=True) if row and row.credential_ref else None
    usable = credential is not None and credential.provider == "deepseek" and credential.revoked_at is None
    return {"provider": "deepseek", "protocol": "independent_tool_api", "version": row.version if row else 0,
            "enabled_services": sorted(row.enabled_services) if row else [], "api_key_set": usable,
            "endpoints": dict(SERVICE_ENDPOINTS), "automatic_retry": False}


def save_configuration(db, fields):
    expected = fields.get("expected_version")
    if type(expected) is not int or expected < 0:
        raise ServiceConfigurationError("必须提供当前独立工具配置版本")
    enabled = fields.get("enabled_services")
    if (not isinstance(enabled, list) or any(not isinstance(item, str) or item not in SERVICES for item in enabled)
            or len(enabled) != len(set(enabled))):
        raise ServiceConfigurationError("请选择有效且不重复的 Search、Fetch、OCR 或原生研究服务")
    row = db.get(ServiceToolConfiguration, "deepseek", populate_existing=True)
    if expected != (row.version if row else 0):
        raise ServiceConfigurationError("独立工具配置已变化，请刷新后重新保存", 409)
    credential = db.get(ServiceToolCredential, row.credential_ref, populate_existing=True) if row and row.credential_ref else None
    if "api_key" in fields:
        protected_value = protect(_key(fields["api_key"]))
        if credential is not None and credential.revoked_at is None:
            credential.revoked_at = utcnow()
        credential = ServiceToolCredential(provider="deepseek", protected_value=protected_value)
        db.add(credential)
        db.flush()
    if enabled and (credential is None or credential.provider != "deepseek" or credential.revoked_at is not None):
        raise ServiceConfigurationError("启用独立服务前，请先填写该服务的专用 API 密钥；不会复用聊天模型密钥")
    if row is None:
        row = ServiceToolConfiguration(provider="deepseek", version=1, enabled_services=sorted(enabled),
            credential_ref=credential.id if credential else None)
        db.add(row)
    else:
        row.version += 1
        row.enabled_services = sorted(enabled)
        row.credential_ref = credential.id if credential else None
        row.updated_at = utcnow()
    db.flush()
    return configuration_snapshot(db)


def revoke_credential(db, expected_version):
    row = db.get(ServiceToolConfiguration, "deepseek", populate_existing=True)
    if type(expected_version) is not int or expected_version != (row.version if row else 0):
        raise ServiceConfigurationError("独立工具配置已变化，请刷新后重试", 409)
    if row is None:
        return configuration_snapshot(db)
    credential = db.get(ServiceToolCredential, row.credential_ref, populate_existing=True) if row.credential_ref else None
    if credential is not None and credential.revoked_at is None:
        credential.revoked_at = utcnow()
    row.credential_ref = None
    row.enabled_services = []
    row.version += 1
    row.updated_at = utcnow()
    db.flush()
    return configuration_snapshot(db)


def require_configuration(db, service, *, version=None, credential_ref=None, reveal_key=False):
    row = db.get(ServiceToolConfiguration, "deepseek", populate_existing=True)
    if row is None or service not in row.enabled_services:
        raise ServiceConfigurationError("此 DeepSeek 独立服务尚未启用，请在设置中配置", 409)
    if (version is not None and version != row.version) or (credential_ref is not None and credential_ref != row.credential_ref):
        raise ServiceConfigurationError("独立服务配置或凭据在审批后发生变化，旧授权已失效", 409)
    credential = db.get(ServiceToolCredential, row.credential_ref, populate_existing=True) if row.credential_ref else None
    if credential is None or credential.provider != "deepseek" or credential.revoked_at is not None:
        raise ServiceConfigurationError("独立服务凭据已撤销，请重新配置并授权", 409)
    result = {"version": row.version, "credential_ref": credential.id}
    if reveal_key:
        result["api_key"] = unprotect(credential.protected_value)
    return result
