"""Chat protocol boundary: private provider state and public text stay separate.

Contracts reviewed against DeepSeek's thinking_mode, web_tools and Chat API
references on 2026-11-15. Messages/Responses are intentionally not advertised.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit
from typing import Protocol

from ..model_connection import chat_request, normalize_base_url, single_line

PROTOCOL = "openai_chat"
ENVELOPE_KEY = "_provider"
MAX_ENVELOPE_BYTES = 16 * 1024 * 1024
_MESSAGE_FIELDS = ("role", "content", "name", "tool_call_id", "tool_calls")
_ROUNDTRIP_FIELDS = ("reasoning_content", "reasoning")


class ProviderContractError(ValueError):
    pass


class ProviderAdapter(Protocol):
    protocol: str
    provider: str

    def identity(self) -> dict: ...
    def envelope(self, message, *, stream_extensions=None) -> dict: ...
    def messages(self, messages) -> list[dict]: ...
    def request(self, api_key, messages, **options) -> tuple[str, dict, dict]: ...


def _copy_json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError()
        return json.loads(encoded)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ProviderContractError("供应商上下文格式无效或超过保存上限，本次响应未完成") from None


class ChatAdapter:
    protocol = PROTOCOL
    provider = "openai_compatible"
    roundtrip_fields = _ROUNDTRIP_FIELDS

    def __init__(self, base_url, model_id, api_key):
        self.base_url = normalize_base_url(base_url)
        self.model_id = single_line(model_id, "模型名称")
        # A signature or opaque result must not cross endpoints, models or keys.
        self.scope = hashlib.sha256(json.dumps(
            [self.protocol, self.base_url, self.model_id, single_line(api_key, "API Key")],
            ensure_ascii=False).encode("utf-8")).hexdigest()

    def identity(self):
        return {"version": 1, "provider": self.provider, "protocol": self.protocol,
                "endpoint": f"{self.base_url}/chat/completions", "model_id": self.model_id,
                "scope": self.scope}

    def envelope(self, message, *, stream_extensions=None):
        value = {**self.identity(), "assistant": _copy_json(message)}
        if stream_extensions:
            value["stream_extensions"] = _copy_json(stream_extensions)
        return value

    def messages(self, messages):
        result = []
        for message in messages:
            canonical = {key: deepcopy(message[key]) for key in _MESSAGE_FIELDS if key in message}
            opaque = message.get(ENVELOPE_KEY)
            if opaque is not None:
                if not isinstance(opaque, dict) or opaque.get("version") != 1:
                    raise ProviderContractError("供应商上下文版本不兼容，请从已保存资料重新开始本轮")
                if opaque.get("scope") == self.scope and opaque.get("protocol") == self.protocol:
                    raw = opaque.get("assistant")
                    if not isinstance(raw, dict) or canonical.get("role") != "assistant":
                        raise ProviderContractError("供应商上下文与消息角色不一致")
                    # Original function arguments are retained, not parsed and
                    # re-serialized on each turn. The executor can repair IDs.
                    for key in ("tool_calls", *self.roundtrip_fields):
                        if key in raw:
                            canonical[key] = deepcopy(raw[key])
                # Cross-model histories keep authorized canonical facts only.
            elif canonical.get("role") == "assistant":
                # Legacy checkpoints and explicit synthetic clients predate
                # envelopes. They have no provider signatures to forward.
                for key in _ROUNDTRIP_FIELDS:
                    if key in message:
                        canonical[key] = deepcopy(message[key])
            result.append(canonical)
        return result

    def request(self, api_key, messages, *, reasoning_effort="", temperature=0.2, tools=None, stream=False,
                native_tools=None, max_output_tokens=None, response_format=None):
        if native_tools:
            raise ProviderContractError("此接口没有声明可用的原生联网协议，请使用已配置的独立工具")
        url, headers, body = chat_request(self.base_url, api_key, self.model_id, self.messages(messages),
            reasoning_effort=reasoning_effort, temperature=temperature, tools=tools, stream=stream,
            max_output_tokens=max_output_tokens)
        if response_format is not None:
            # This adapter advertises JSON object mode only. A successful object
            # response does not prove strict JSON Schema or typed tool output.
            if response_format != {"type": "json_object"}:
                raise ProviderContractError("当前结构化协议仅支持 JSON 对象模式，未发送未知 schema 参数")
            body["response_format"] = {"type": "json_object"}
        parts = urlsplit(self.base_url)
        if (max_output_tokens is not None and parts.scheme == "https" and parts.hostname == "api.openai.com"
                and parts.port in (None, 443)):
            # Official Chat Completions limits visible and reasoning tokens.
            # Other compatible services keep their documented max_tokens field;
            # a model ID is never treated as protocol or capacity evidence.
            body["max_completion_tokens"] = body.pop("max_tokens")
        return url, headers, body


class DeepSeekChatAdapter(ChatAdapter):
    provider = "deepseek"
    roundtrip_fields = ("reasoning_content", "server_tool_calls")

    def request(self, api_key, messages, **options):
        native_tools = options.pop("native_tools", None)
        if native_tools:
            if (not isinstance(native_tools, (list, tuple)) or len(native_tools) != len(set(native_tools))
                    or any(name not in ("deepseek_web_search", "deepseek_read_url") for name in native_tools)):
                raise ProviderContractError("原生联网工具声明无效")
            if options.get("tools"):
                raise ProviderContractError("本次原生联网阶段不能混入本地工具；请分阶段请求")
        url, headers, body = super().request(api_key, messages, **options)
        if native_tools:
            body["tools"] = [{"type": name} for name in native_tools]
        effort = options.get("reasoning_effort", "")
        if effort == "none":
            body.pop("reasoning_effort", None)
            body["thinking"] = {"type": "disabled"}
        else:
            # DeepSeek defaults to thinking enabled. Temperature has no effect
            # there and must not be presented as a working generation control.
            body.pop("temperature", None)
        return url, headers, body


def adapter_for(base_url, model_id, api_key):
    normalized = normalize_base_url(base_url)
    parts = urlsplit(normalized)
    official = (parts.scheme == "https" and parts.hostname == "api.deepseek.com"
                and parts.port in (None, 443) and parts.path in ("", "/v1", "/beta"))
    return (DeepSeekChatAdapter if official else ChatAdapter)(normalized, model_id, api_key)


def assistant_message(content, tool_calls, reasoning_content="", provider_payload=None, *, calls=None):
    chosen = tool_calls if calls is None else calls
    message = {"role": "assistant", "content": content or None}
    if chosen:
        message["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False)}}
            for call in chosen]
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if provider_payload:
        payload = _copy_json(provider_payload)
        raw = payload.get("assistant")
        if isinstance(raw, dict) and calls is not None:
            original = raw.get("tool_calls")
            if isinstance(original, list) and len(original) == len(chosen):
                for original_call, selected in zip(original, chosen):
                    original_call["id"] = selected["id"]
            else:
                raw["tool_calls"] = deepcopy(message.get("tool_calls", []))
        message[ENVELOPE_KEY] = payload
    return message


def stream_extensions(choice, delta):
    """Keep unrecognized fields as structured private data, never display text."""
    extra_delta = {key: deepcopy(value) for key, value in delta.items()
                   if key not in ("role", "content", "reasoning_content", "reasoning", "tool_calls")}
    extra_choice = {key: deepcopy(value) for key, value in choice.items()
                    if key not in ("index", "delta", "finish_reason", "logprobs")}
    return {"delta": extra_delta, "choice": extra_choice} if extra_delta or extra_choice else None


def stored_assistant(context, content):
    """Restore private replay data while keeping the accepted answer authoritative."""
    if not context:
        return {"role": "assistant", "content": content}
    if not isinstance(context, dict) or context.get("version") != 1 or not isinstance(context.get("assistant"), dict):
        raise ProviderContractError("历史供应商上下文格式不兼容，请从保留资料重新建立任务")
    message = _copy_json(context["assistant"])
    if message.get("role") != "assistant" or message.get("tool_calls"):
        raise ProviderContractError("历史最终答复含有未完成工具调用，不能作为正常对话恢复")
    message["content"] = content
    return message
