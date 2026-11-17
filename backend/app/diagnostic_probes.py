"""Explicit diagnostic requests, bound to the configuration the user reviewed.

Local previews never send HTTP. A process-keyed digest covers private config
without exposing a reusable credential hash. Changed config requires a new
preview. Probe responses contain only authored errors and sanitized call facts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
import httpx

from . import config, model_profiles
from .call_ledger import _endpoint, list_calls
from .db import SessionLocal
from .llm import APIClient
from .model_connection import normalize_base_url
from .pipeline.embeddings import APIEmbedder, embedding_connection
from .runtime_context import bind_runtime_config

_SIGNING_KEY = secrets.token_bytes(32)
SERVICES = ("llm", "embedding")


class ProbeChanged(ValueError):
    pass


def _configuration(db, service, conversation_id):
    if service == "embedding":
        return {**embedding_connection(), "profile_id": None, "reasoning_effort": ""}
    options = model_profiles.options_snapshot(db, conversation_id) if conversation_id else None
    profile_id = options["model_profile_id"] if options else model_profiles.default_profile_id(db)
    profile = model_profiles.public_profile(db, profile_id, usable=True)
    if profile_id == model_profiles.LEGACY_PROFILE_ID:
        with config._settings_lock:
            config.require_available_credentials("llm_api_key")
            values = {"base_url": config.settings.llm_base_url, "model_id": config.settings.llm_model,
                      "api_key": config.settings.llm_api_key, "mode": config.settings.llm_mode,
                      "generation": config.settings.legacy_credential_generation,
                      "reasoning_effort": config.settings.reasoning_effort}
    else:
        row = None if profile_id == model_profiles.DEMO_PROFILE_ID else model_profiles.get_profile(db, profile_id, usable=True)
        values = {"base_url": profile["base_url"], "model_id": profile["model_id"],
                  "api_key": model_profiles.read_credential(db, row.credential_ref) if row else "",
                  "mode": profile["mode"], "reasoning_effort": profile["reasoning_effort"],
                  "version": profile["version"]}
    if options and options["reasoning_effort"] is not None:
        values["reasoning_effort"] = options["reasoning_effort"]
    return {**values, "profile_id": profile_id}


def _digest(service, conversation_id, values):
    payload = json.dumps([service, conversation_id, values], ensure_ascii=False, sort_keys=True).encode()
    return hmac.new(_SIGNING_KEY, payload, hashlib.sha256).hexdigest()


def _target(service, conversation_id, values):
    suffix = "embeddings" if service == "embedding" else "chat/completions"
    base = normalize_base_url(values["base_url"], suffix)
    enabled = values["mode"] == "api" and bool(base and values["model_id"])
    return {"service": service, "label": "模型基础对话" if service == "llm" else "Embedding",
            "model_id": values["model_id"], "profile_id": values["profile_id"],
            "endpoint": _endpoint(f"{base}/{suffix}") if base else "", "mode": values["mode"],
            "available": enabled, "expected_config": _digest(service, conversation_id, values),
            "detail": "主动测试发送固定测试文本，可能产生费用。" if enabled else
                      "当前使用本地演示/本地向量或模型尚未配置，未发起接口请求。"}


def probe_targets(conversation_id=None):
    targets = []
    with SessionLocal() as db:
        for service in SERVICES:
            try:
                targets.append(_target(service, conversation_id, _configuration(db, service, conversation_id)))
            except Exception:
                targets.append({"service": service, "label": "模型基础对话" if service == "llm" else "Embedding",
                                "model_id": "", "profile_id": None, "endpoint": "", "mode": "unavailable",
                                "available": False, "expected_config": "",
                                "detail": "无法读取此配置，请检查模型选择和凭证状态后重试。"})
    return targets


class _ProbeClient(APIClient):
    def __init__(self, connection, authorization_check=None):
        super().__init__(timeout=8, authorization_check=authorization_check)
        self._frozen_connection = dict(connection)

    def _connection(self, reasoning_effort):
        value = self._frozen_connection
        return value["base_url"], value["api_key"], value["model_id"], value["reasoning_effort"]

    def _authorize_request(self, base, key, model):
        # A user-initiated diagnostic has its own frozen configuration, not a
        # fabricated parent Goal. Do not weaken the ordinary run boundary.
        value = self._frozen_connection
        if ((base, key, model) != (value["base_url"], value["api_key"], value["model_id"])
                or self._authorization_check is None or self._authorization_check() is not True):
            raise ProbeChanged("配置或凭证已变化，诊断未继续派发")


def run_probe(service, expected_config, conversation_id=None):
    if service not in SERVICES:
        raise ValueError("未知诊断服务")
    with SessionLocal() as db:
        try:
            connection = _configuration(db, service, conversation_id)
            target = _target(service, conversation_id, connection)
        except Exception:
            raise ProbeChanged("配置不可用，请重新运行本地检查并选择测试目标") from None
    if not hmac.compare_digest(target["expected_config"], expected_config) or not target["available"]:
        raise ProbeChanged("配置已变化或不可测试，请重新运行本地检查并确认目标")
    diagnostic_id = "diagnostic-" + uuid.uuid4().hex
    status, detail = "ok", "基础对话响应有效；流式、工具和图像能力尚未测试。"
    failure_kind, http_status = "none", None
    def still_authorized():
        with SessionLocal() as check_db:
            current = _configuration(check_db, service, conversation_id)
        return hmac.compare_digest(_digest(service, conversation_id, current), expected_config)
    with bind_runtime_config({"run_id": diagnostic_id}):
        try:
            if service == "llm":
                response = _ProbeClient(connection, still_authorized).chat([{"role": "user", "content": "Reply with OK."}])
                if not response.content.strip() or response.tool_calls:
                    raise ValueError("BasicTextProbeFailed")
            else:
                vectors = APIEmbedder(timeout=8, connection=connection, authorization_check=still_authorized).encode(["diagnostic ping"])
                detail = f"Embedding 响应有效，向量维度 {vectors.shape[1]}；未修改索引。"
        except Exception as exc:
            # Never expose raw exception/response text, which may contain keys.
            failure_kind = "unknown"
            if isinstance(exc, httpx.HTTPStatusError):
                http_status = exc.response.status_code
                failure_kind = ("authentication" if http_status==401 else "permission" if http_status==403 else
                    "quota" if http_status==402 else "rate_limit" if http_status==429 else
                    "parameters" if http_status in (400,404,422) else "upstream" if http_status>=500 else "unknown")
            elif isinstance(exc, httpx.TimeoutException): failure_kind="timeout"
            elif isinstance(exc, httpx.ProxyError): failure_kind="certificate_or_proxy"
            elif isinstance(exc, httpx.RequestError): failure_kind="certificate_or_proxy" if "证书" in str(exc) else "network"
            elif isinstance(exc, ValueError): failure_kind="protocol"
            try:
                if not still_authorized(): failure_kind="configuration_changed"
            except Exception:
                failure_kind="configuration_changed"
            from .diagnostic_records import FAILURES
            status, detail = "error", FAILURES[failure_kind] + "。请先核对调用回执，再检查配置或主动重试。"
    calls = list_calls(run_id=diagnostic_id, include_synthetic=True)["items"]
    return {"diagnostic_id": diagnostic_id, "service": service, "status": status,
            "failure_kind": failure_kind, "http_status": http_status,
            "detail": detail, "target": target, "calls": calls,
            "recovery": {"action": "refresh_then_retry", "label": "重新检查配置后可再次测试"}}
