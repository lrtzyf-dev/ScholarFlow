"""Shared validation and request construction for OpenAI-compatible connections.

Only scheme/hostname and enumerated options are case-insensitive. Provider paths,
model IDs, and credentials must retain their spelling.
"""
from __future__ import annotations

import errno
import hashlib
import re
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

import httpx

VALID_EFFORT = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def network_error_message(exc: httpx.HTTPError) -> str:
    """Describe a transport failure without exposing request or exception text.

    HTTPX wraps native DNS/TLS/socket errors. Inspect only their types and known
    numeric error codes: upstream messages may contain credentials or proxy URLs.
    """
    causes: list[BaseException] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending and len(causes) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        causes.append(current)
        for chained in (current.__cause__, current.__context__):
            if chained is not None:
                pending.append(chained)

    if any(isinstance(cause, ssl.SSLCertVerificationError) for cause in causes):
        return "接口 HTTPS 证书验证失败，请检查系统时间、代理证书和服务商证书"
    if any(isinstance(cause, ssl.SSLError) for cause in causes):
        return "接口 TLS 安全连接失败，请检查服务商 HTTPS 配置和系统代理"
    if any(isinstance(cause, socket.gaierror) for cause in causes):
        return "接口域名解析失败，请检查接口地址、DNS 和网络"
    if any(isinstance(cause, httpx.ProxyError) for cause in causes):
        return "代理连接失败，请检查系统代理设置后重试"
    if isinstance(exc, httpx.ConnectTimeout):
        return "连接模型接口超时，请检查网络和系统代理后重试"
    if isinstance(exc, httpx.TimeoutException):
        return "模型接口响应超时，服务可能繁忙，请稍后重试"
    if any(isinstance(cause, ConnectionRefusedError)
           or isinstance(cause, OSError) and cause.errno in (errno.ECONNREFUSED, 10061)
           for cause in causes):
        return "接口拒绝连接，请检查地址、端口、服务状态和系统代理"
    if any(isinstance(cause, (ConnectionResetError, ConnectionAbortedError)) for cause in causes):
        return "模型接口连接意外中断，请重试并检查网络或系统代理"
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.LocalProtocolError)):
        return "模型接口通信协议异常，请检查接口地址、服务状态和系统代理"
    return "无法连接到接口地址，请检查网络、系统代理和地址后重试"


def single_line(value: str, label: str) -> str:
    value = value.strip()
    if _CONTROL.search(value):
        raise ValueError(f"{label}不能包含换行或控制字符")
    return value


def normalize_base_url(value: str, endpoint: str = "chat/completions") -> str:
    value = single_line(value, "接口地址")
    if not value:
        return ""
    if any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("接口地址不能包含空白或反斜杠")
    if "?" in value or "#" in value:
        raise ValueError("接口地址不能包含查询参数或片段（? 或 #）")
    if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.IGNORECASE):
        raise ValueError("接口地址不能包含编码后的控制字符")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise ValueError("接口地址必须是完整的 http:// 或 https:// 地址")
        if parts.username is not None or parts.password is not None:
            raise ValueError("接口地址不能包含用户名或密码，请将密钥填入 API Key")
        host = parts.hostname
        if not host:
            raise ValueError("接口地址缺少主机名")
        port = parts.port
        # HTTPX validates the authority and normalizes IDNA/IPv6 without changing path case.
        parsed = httpx.URL(value)
        host = parsed.raw_host.decode("ascii").lower()
        if not host or "%" in host:
            raise ValueError("接口地址主机名无效")
        scheme = parts.scheme.lower()
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if scheme == "https" else 80):
            authority += f":{port}"
        path = parts.path.rstrip("/")
        suffix = "/" + endpoint
        if path.endswith(suffix):
            path = path[:-len(suffix)].rstrip("/")
        elif path.lower().endswith(suffix):
            raise ValueError(f"接口路径区分大小写，请使用标准小写 {suffix} 尾缀或填写 Base URL")
        if endpoint == "chat/completions" and path.lower().endswith("/responses"):
            raise ValueError("当前支持 Chat Completions 接口，请填写其 Base URL；暂不支持 Responses 接口")
        if endpoint == "chat/completions" and path.lower().endswith("/messages"):
            raise ValueError("当前支持 Chat Completions 接口，请填写其 Base URL；暂不支持 Messages 接口")
        return urlunsplit((scheme, authority, path, "", ""))
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise ValueError("接口地址格式无效") from exc


def origin(base: str) -> tuple[str, str, int | None] | None:
    if not base:
        return None
    parts = urlsplit(base)
    return (parts.scheme, parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))


def require_key_for_origin_change(old_base: str, new_base: str, old_key: str,
                                  supplied_key: str | None, label: str = "模型") -> None:
    if old_key and supplied_key is None and origin(old_base) != origin(new_base):
        raise ValueError(f"{label}接口地址已更换来源，请重新填写 API Key，或明确清空密钥后保存")


def normalize_effort(value: str) -> str:
    value = single_line(value, "推理强度").lower()
    if value not in VALID_EFFORT:
        raise ValueError("推理强度应为默认、none、minimal、low、medium、high、xhigh 或 max")
    return value


def normalize_setting(field: str, value: str) -> str:
    if field.endswith("_base_url"):
        return normalize_base_url(value, "embeddings" if field.startswith("embedding_") else "chat/completions")
    if field == "reasoning_effort":
        return normalize_effort(value)
    if field.endswith("_api_key"):
        return single_line(value, "API Key")
    if field.endswith("_model"):
        return single_line(value, "模型名称")
    if field == "approval_mode":
        return single_line(value, "审批模式").lower()
    return value.strip()


def chat_request(base: str, key: str, model: str, messages: list[dict], *,
                 reasoning_effort: str = "", temperature: float = 0.2,
                 tools: list[dict] | None = None, stream: bool = False,
                 max_output_tokens: int | None = None) -> tuple[str, dict, dict]:
    base = normalize_base_url(base)
    key = single_line(key, "API Key")
    model = single_line(model, "模型名称")
    if not base:
        raise ValueError("请先填写接口地址")
    if not model:
        raise ValueError("请先填写模型名称")
    body = {"model": model, "messages": messages}
    if max_output_tokens is not None:
        from .model_context_limits import MAX_OUTPUT_TOKENS
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError("最大输出须为有效的正整数 token 数")
        body["max_tokens"] = max_output_tokens
    effort = normalize_effort(reasoning_effort)
    if effort:
        body["reasoning_effort"] = effort
    else:
        body["temperature"] = temperature
    if tools:
        body["tools"] = [{"type": "function", "function": tool} for tool in tools]
    if stream:
        body.update(stream=True, stream_options={"include_usage": True})
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return f"{base}/chat/completions", headers, body


def chat_message(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise ValueError("接口未返回有效的 Chat Completions 响应（缺少 choices）")
    choice = data["choices"][0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise ValueError("接口未返回有效的 Chat Completions 响应（缺少 message）")
    if not (isinstance(message.get("content"), str)
            or isinstance(message.get("reasoning_content"), str)
            or isinstance(message.get("reasoning"), str)
            or isinstance(message.get("tool_calls"), list) and message["tool_calls"]):
        raise ValueError("接口未返回有效的 Chat Completions 响应（缺少内容）")
    return message


def probe_chat(base: str, key: str, model: str, effort: str, timeout: float = 15, *, authorization_check=None,
               context_window_tokens=None, max_output_tokens=None) -> None:
    from .call_ledger import tracked_call
    from .providers.chat import adapter_for
    from .request_resources import request_admission
    from .model_context_limits import context_limits, ensure_request_capacity
    from urllib.parse import urlsplit
    limits = context_limits({"context_window_tokens": context_window_tokens, "max_output_tokens": max_output_tokens})
    url, headers, body = adapter_for(base, model, key).request(key,
                                      [{"role": "user", "content": "Reply only with OK."}],
                                      reasoning_effort=effort, max_output_tokens=limits["max_output_tokens"])
    ensure_request_capacity(body, limits)
    # Never follow a redirect while carrying a saved credential.
    with request_admission(provider=urlsplit(url).hostname or "configured", endpoint=url,
                           credential_fingerprint=hashlib.sha256(single_line(key, "API Key").encode()).hexdigest(),
                           service="model_probe") as admission, tracked_call(provider=urlsplit(url).hostname or "configured", service="model_probe",
                      endpoint=url, model_id=body["model"]) as tracker, httpx.Client(timeout=timeout, follow_redirects=False) as client:
        admission.bind_call(tracker.id)
        if authorization_check is not None and authorization_check() is not True:
            raise ValueError("模型配置或凭据已变化，测试请求未发送")
        response = client.post(url, headers=headers, json=body)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError("接口返回的不是 JSON 模型响应，请检查接口地址") from exc
        chat_message(data)
        tracker.usage(data.get("usage"))


def discover_models(base: str, key: str, timeout: float = 15, *, authorization_check=None) -> list[dict[str, str]]:
    """Read a provider's model IDs without sending a completion or changing settings."""
    from .call_ledger import tracked_call
    from .request_resources import request_admission
    from urllib.parse import urlsplit
    base = normalize_base_url(base)
    key = single_line(key, "API Key")
    if not base:
        raise ValueError("请先填写接口地址")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # Credentials may only reach the exact origin chosen by the user.
    url = f"{base}/models"
    with request_admission(provider=urlsplit(url).hostname or "configured", endpoint=url,
                           credential_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
                           service="model_discovery") as admission, tracked_call(provider=urlsplit(url).hostname or "configured", service="model_discovery",
                      endpoint=url) as tracker, httpx.Client(timeout=timeout, follow_redirects=False) as client:
        admission.bind_call(tracker.id)
        if authorization_check is not None and authorization_check() is not True:
            raise ValueError("模型配置或凭据已变化，列表请求未发送")
        response = client.get(url, headers=headers)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError("接口返回的不是 JSON 模型列表，请检查接口地址，或手动填写模型名称") from exc
        if not isinstance(data, dict) or "error" in data or not isinstance(data.get("data"), list):
            raise ValueError("接口未返回兼容的模型列表，请检查接口地址，或手动填写模型名称")
        models: dict[str, dict[str, str]] = {}
        for item in data["data"]:
            if not isinstance(item, dict):
                raise ValueError("接口返回的模型列表格式无效，请手动填写模型名称")
            model_id = item.get("id")
            if (not isinstance(model_id, str) or not model_id.strip()
                    or model_id != model_id.strip() or _CONTROL.search(model_id)
                    or key and key in model_id):
                raise ValueError("接口返回的模型列表格式无效，请手动填写模型名称")
            if model_id in models:
                continue
            model = {"id": model_id}
            owner = item.get("owned_by")
            if (isinstance(owner, str) and owner.strip() and not _CONTROL.search(owner)
                    and not (key and key in owner)):
                model["owned_by"] = owner
            models[model_id] = model
        return [models[model_id] for model_id in sorted(models, key=lambda value: (value.casefold(), value))]
