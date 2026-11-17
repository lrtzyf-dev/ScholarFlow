"""
LLM 统一抽象。
- 配置了 LLM_API_KEY：httpx 直连 OpenAI 兼容接口（百炼/硅基/DeepSeek/vLLM 都支持）
- 显式选择离线演示时使用 MockLLM；云模型未配置时给出配置错误
"""
from __future__ import annotations

import json
import re
import hashlib
import math
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit
from typing import Any

import httpx

from .config import settings
from .model_connection import chat_message, chat_request, network_error_message, normalize_effort
from .rag.citation import split_sentences
from .runtime_context import current_runtime_config
from .providers.chat import adapter_for, assistant_message, stream_extensions


class LLMResponse:
    def __init__(self, content: str, tool_calls: list[dict] | None = None,
                 reasoning_content: str = "", usage: dict | None = None,
                 provider_payload: dict | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = reasoning_content  # 推理模型（Qwen3/QwQ/DeepSeek-R1）的思考链
        # token 用量：{prompt_tokens, completion_tokens, total_tokens}
        self.usage = usage or None
        # Internal-only replay state; to_dict remains the public projection.
        from copy import deepcopy
        self.provider_payload = deepcopy(provider_payload)

    def assistant_message(self, tool_calls=None) -> dict:
        return assistant_message(self.content, self.tool_calls, self.reasoning_content,
                                 self.provider_payload, calls=tool_calls)

    def persisted_context(self) -> dict:
        return {"version": 1, "assistant": self.assistant_message()}

    def to_dict(self) -> dict:
        return {"content": self.content, "tool_calls": self.tool_calls,
                "reasoning_content": self.reasoning_content, "usage": self.usage}


def estimate_tokens(text: str) -> int:
    """离线估算 token 数：中文按字计，英文按约 4 字符 1 token，取两者之和。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(re.sub(r"[\u4e00-\u9fff\s]", "", text))
    return cjk + (max(1, round(other / 4)) if other else 0)


def _messages_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
    return "\n".join(parts)


def estimate_usage(messages: list[dict], output: LLMResponse) -> dict:
    """离线模式下估算一次调用的用量。"""
    prompt_tokens = estimate_tokens(_messages_text(messages))
    completion = estimate_tokens(
        (output.content or "") + (output.reasoning_content or "")
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion,
        "total_tokens": prompt_tokens + completion,
    }


class BaseLLM:
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.2, reasoning_effort: str | None = None) -> LLMResponse:
        raise NotImplementedError

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                    reasoning_effort: str | None = None):
        """生成器：yield ("delta", "thinking"|"answer", text)…，最后 yield ("final", LLMResponse)。"""
        raise NotImplementedError


class ModelResponseError(ValueError):
    """A provider response failed validation; messages never contain raw bodies."""


class IncompleteModelResponse(ModelResponseError):
    """Transport EOF or a provider limit is not a successfully finished answer."""


def _check_finish_reason(reason: object) -> None:
    if reason is None:
        return  # Some compatible non-streaming gateways omit this field.
    if reason in ("length", "max_tokens"):
        raise IncompleteModelResponse("模型达到输出长度限制，回答尚未完整结束；已生成内容保留，请继续补齐")
    if reason == "content_filter":
        raise IncompleteModelResponse("模型服务中止了本次输出，不能视为完整回答")
    if not isinstance(reason, str) or reason not in ("stop", "tool_calls", "function_call"):
        raise ModelResponseError("模型接口返回了无法识别的结束状态")


def _tool_arguments(raw: object) -> dict:
    if not isinstance(raw, str):
        raise ModelResponseError("模型返回的工具参数格式无效，未执行该工具")

    def reject_constant(value):
        raise ValueError("nonstandard JSON constant")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    try:
        # Python's default decoder accepts NaN/Infinity and overflows 1e999 to
        # infinity. Those are not valid tool JSON and can bypass schema bounds.
        args = json.loads(raw or "{}", parse_constant=reject_constant, parse_float=finite_float)
    except (ValueError, RecursionError):
        raise ModelResponseError("模型返回的工具参数不完整，未执行该工具") from None
    if not isinstance(args, dict):
        raise ModelResponseError("模型返回的工具参数必须是对象，未执行该工具")
    return args


def _safe_http_error(exc: httpx.HTTPError) -> httpx.HTTPError:
    """Preserve HTTPX categories/retry signals without exposing upstream text."""
    if isinstance(exc, httpx.HTTPStatusError):
        # str(HTTPStatusError) normally includes provider-controlled reason
        # phrases and the URL. Keep objects for internal inspection, not text.
        return httpx.HTTPStatusError(
            f"模型接口请求失败（HTTP {exc.response.status_code}），请检查配置或稍后重试",
            request=exc.request, response=exc.response)
    message = network_error_message(exc)
    if isinstance(exc, httpx.RequestError):
        try:
            request = exc.request
        except RuntimeError:
            request = None
        return type(exc)(message, request=request)
    return httpx.HTTPError(message)


def _response_text(message: dict, field: str) -> str:
    value = message.get(field)
    if value is not None and not isinstance(value, str):
        raise ModelResponseError("模型接口返回的文本格式无效")
    return value or ""


def _resolve_effort(reasoning_effort: str | None) -> str:
    """None 时回落到全局设置；返回最终要发送的 effort（空串表示不发送）。"""
    if reasoning_effort is None:
        return normalize_effort(settings.reasoning_effort or "")
    return normalize_effort(reasoning_effort or "")


def _chunk(text: str, size: int = 16):
    """把文本切成小块，模拟逐字流式（Mock 用）。"""
    import re

    pieces = re.split(r"(?<=[。！？，,.!?\s])", text)
    for p in pieces:
        for i in range(0, len(p), size):
            seg = p[i:i + size]
            if seg:
                yield seg


class APIClient(BaseLLM):
    """真实大模型，OpenAI 兼容 Chat Completions，httpx 直连。"""

    def __init__(self, timeout: float = 120.0, *, runtime_config: dict | None = None,
                 synthetic: bool = False, provider_options: dict | None = None,
                 authorization_check=None, native_activity=False, context_limits: dict | None = None) -> None:
        self._timeout = timeout
        from copy import deepcopy
        self._runtime_config = deepcopy(runtime_config)
        self._call_context = threading.local()
        self._synthetic = synthetic
        self._provider_options = deepcopy(provider_options or {})
        self._authorization_check = authorization_check
        self._native_activity = native_activity
        from .model_context_limits import context_limits as normalize_limits
        self._context_limits = normalize_limits(context_limits if context_limits is not None else self._runtime_config)

    def _build_request(self, adapter, key, messages, **options):
        from .model_context_limits import ensure_request_capacity
        # Per-run limits always win over provider options. Do not reread a
        # mutable profile or process setting after this client was admitted.
        options = {**self._provider_options, **options, "max_output_tokens": self._context_limits["max_output_tokens"]}
        from .model_capability_policy import require_request
        require_request(self._runtime_config, messages, options)
        url, headers, body = adapter.request(key, messages, **options)
        ensure_request_capacity(body, self._context_limits)
        return url, headers, body

    def context_identity(self):
        """Public non-secret binding for retiring incompatible private stages."""
        base, key, model, _ = self._connection(None)
        return adapter_for(base, model, key).identity()

    def _authorize_request(self, base, key, model):
        from .memory_governance import assert_request_memory
        binding = {**(self._runtime_config or {}), **(current_runtime_config() or {})}
        assert_request_memory(binding)
        from .goal_policy import parent_snapshot
        parent_snapshot(binding)
        from .model_profiles import ModelConfigurationError, resolve_model_config
        if self._authorization_check is not None and self._authorization_check() is not True:
            raise ModelConfigurationError("本次供应商请求授权已失效，未继续发送请求")
        if self._runtime_config is not None:
            value = resolve_model_config(self._runtime_config)
            if (value["base_url"], value["api_key"], value["model_id"]) != (base, key, model):
                raise ModelConfigurationError("排队期间模型连接已变化，请重新核对配置")
        elif type(self) is APIClient:
            from .config import require_available_credentials
            require_available_credentials("llm_api_key")
            if (settings.llm_base_url, settings.llm_api_key, settings.llm_model) != (base, key, model):
                raise ModelConfigurationError("排队期间默认模型连接已变化，请重新核对配置")

    @contextmanager
    def _meter(self, url, model, body, *, streaming=False, credential_fingerprint=None):
        """One durable call per HTTP attempt, without recording prompts or keys."""
        from .call_ledger import begin_call, finish_call
        from .request_resources import request_admission
        binding = current_runtime_config() or self._runtime_config or {}
        fingerprint = hashlib.sha256(json.dumps(
            [url, body, binding.get("run_id"), binding.get("attempt_id"), binding.get("subtask_id"),
             binding.get("subtask_attempt"), credential_fingerprint],
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        previous = getattr(self._call_context, "failed_call", None)
        retry_of = previous[1] if previous and previous[0] == fingerprint else None
        try:
            with request_admission(provider=urlsplit(url).hostname or "configured", endpoint=url,
                    credential_fingerprint=credential_fingerprint,
                    service="chat_stream" if streaming else "chat", runtime_config=binding) as admission:
                call_id = begin_call(provider=urlsplit(url).hostname or "configured",
                                     service="chat_stream" if streaming else "chat",
                                     endpoint=url, model_id=model,
                                     run_id=binding.get("run_id"), attempt_id=binding.get("attempt_id"),
                                     retry_of_call_id=retry_of, synthetic=self._synthetic)
                # Auxiliary review recovery needs to distinguish admission
                # rejection from a successful charged call followed by a local
                # callback failure. Keep this same-thread attempt identity even
                # after success; failed_call alone resets to None on success.
                self._call_context.last_started_call_id = call_id
                receipt = {"call_id": call_id, "usage": None, "provider_request_id": None,
                           "admission": admission}
                try:
                    # Register the diagnostic attempt now, but bind the budget
                    # only at the HTTP boundary, after authorization succeeds.
                    yield receipt
                except BaseException as exc:
                    self._call_context.failed_call = fingerprint, call_id
                    cancelled = (isinstance(exc, (GeneratorExit, KeyboardInterrupt)) or type(exc).__name__ == "CancelledError"
                                 or getattr(exc, "request_cancelled", False) is True)
                    finish_call(call_id, state="cancelled" if cancelled else "failed", usage=receipt["usage"],
                                error_kind=type(exc).__name__, provider_request_id=receipt["provider_request_id"])
                    raise
                else:
                    self._call_context.failed_call = None
                    finish_call(call_id, usage=receipt["usage"], provider_request_id=receipt["provider_request_id"])
        except httpx.HTTPError as exc:
            # Admission observes the original status/Retry-After before errors
            # are sanitized for UI/logging.
            raise _safe_http_error(exc) from None

    def _connection(self, reasoning_effort):
        if self._runtime_config is not None:
            from .model_profiles import resolve_model_config
            config = resolve_model_config(self._runtime_config)
            effort = config["reasoning_effort"] if reasoning_effort is None else reasoning_effort
            return config["base_url"], config["api_key"], config["model_id"], normalize_effort(effort or "")
        from .config import require_available_credentials
        require_available_credentials("llm_api_key")
        return settings.llm_base_url, settings.llm_api_key, settings.llm_model, _resolve_effort(reasoning_effort)

    @staticmethod
    def _record_usage(receipt, usage):
        # Validate/persist before replacing the last accepted snapshot. Invalid
        # provider counts must not prevent the meter from committing failure.
        if usage is not None:
            from .call_ledger import record_usage
            record_usage(receipt["call_id"], usage)
            receipt["usage"] = usage

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.2, reasoning_effort: str | None = None) -> LLMResponse:
        base, key, model, effort = self._connection(reasoning_effort)
        adapter = adapter_for(base, model, key)
        url, headers, body = self._build_request(adapter, key, messages,
            reasoning_effort=effort, temperature=temperature, tools=tools)
        with self._meter(url, model, body, credential_fingerprint=hashlib.sha256(key.encode()).hexdigest()) as receipt:
            self._authorize_request(base, key, model)
            with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                receipt["admission"].bind_call(receipt["call_id"])
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict):
                self._record_usage(receipt, data.get("usage"))
                if data.get("error") is not None:
                    raise ModelResponseError("模型接口返回服务错误，本次请求未完成")
            msg = chat_message(data)
            _check_finish_reason(data["choices"][0].get("finish_reason"))
            raw_calls = msg.get("tool_calls")
            if raw_calls is None:
                raw_calls = []
            if not isinstance(raw_calls, list):
                raise ModelResponseError("模型返回的工具调用格式无效，未执行工具")
            tool_calls: list[dict] = []
            for tc in raw_calls:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
                    raise ModelResponseError("模型返回的工具名称无效，未执行工具")
                if not isinstance(tc.get("id", ""), str):
                    raise ModelResponseError("模型返回的工具身份无效，未执行工具")
                tool_calls.append({"id": tc.get("id", ""), "name": fn["name"],
                                   "arguments": _tool_arguments(fn.get("arguments", ""))})
            result = LLMResponse(
                content=_response_text(msg, "content"), tool_calls=tool_calls,
                reasoning_content=_response_text(msg, "reasoning_content") or _response_text(msg, "reasoning"),
                usage=receipt["usage"],
                provider_payload=adapter.envelope(msg),
            )
            if not result.content and not tool_calls:
                raise IncompleteModelResponse("模型未返回可交付的回答或工具调用，本次请求未完成")
            if data["choices"][0].get("finish_reason") in ("tool_calls", "function_call") and not tool_calls:
                raise ModelResponseError("模型声称调用工具但没有返回工具调用，本次请求未完成")
            if tool_calls:
                from .model_capability_policy import capability, CapabilityUnavailable
                if capability(self._runtime_config, "tools") == "unsupported":
                    raise CapabilityUnavailable("tools")
        return result

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                    reasoning_effort: str | None = None):
        """OpenAI 兼容流式：逐块 yield 思考/答案增量，最后 yield 汇总 LLMResponse。"""
        from .model_capability_policy import capability
        if capability(self._runtime_config, "streaming") == "unsupported":
            # Keep the same exact model, inputs, tools and meter. The executor
            # displays this frozen route explicitly; a single final delta is
            # complete text, never a fabricated incremental stream.
            result = self.chat(messages, tools=tools, reasoning_effort=reasoning_effort)
            if result.content:
                yield ("delta", "answer", result.content)
            yield ("final", result)
            return
        base, key, model, effort = self._connection(reasoning_effort)
        adapter = adapter_for(base, model, key)
        url, headers, body = self._build_request(adapter, key, messages,
            reasoning_effort=effort, tools=tools, stream=True)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        stream_usage: dict | None = None
        saw_done = False
        finish_reason = None
        private_extensions = []
        final_fields = {}
        reasoning_field = "reasoning_content"
        reasoning_seen = False
        last_step_reasoning_start = 0
        activity_projector = None

        with self._meter(url, model, body, streaming=True,
                         credential_fingerprint=hashlib.sha256(key.encode()).hexdigest()) as receipt, httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
            self._authorize_request(base, key, model)
            receipt["admission"].bind_call(receipt["call_id"])
            with client.stream("POST", url, headers=headers, json=body) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    response_authorization = getattr(self._authorization_check, "during_response", self._authorization_check)
                    if response_authorization is not None and response_authorization() is not True:
                        raise ModelResponseError("供应商请求授权已撤销，已停止本地读取；远端执行或计费状态待核对")
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        saw_done = True
                        break
                    try:
                        obj = json.loads(data)
                    except (ValueError, RecursionError):
                        raise ModelResponseError("模型流包含无效数据，本次输出未完成") from None
                    # 部分供应商在带 usage 的 chunk 里 choices 为空
                    if not isinstance(obj, dict):
                        raise ModelResponseError("模型流返回格式无效，本次输出未完成")
                    if obj.get("usage") is not None:
                        self._record_usage(receipt, obj["usage"])
                        stream_usage = obj["usage"]
                    if obj.get("error") is not None:
                        raise ModelResponseError("模型流返回服务错误，本次输出未完成")
                    choices = obj.get("choices", [])
                    if not isinstance(choices, list):
                        raise ModelResponseError("模型流的choices格式无效，本次输出未完成")
                    if not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        raise ModelResponseError("模型流的选择项格式无效，本次输出未完成")
                    choice_index = choice.get("index", 0)
                    if type(choice_index) is not int or choice_index != 0:
                        raise ModelResponseError("模型流返回了非当前回答的选择项，本次输出未完成")
                    delta = choice.get("delta")
                    if delta is None:
                        delta = {}
                    if not isinstance(delta, dict):
                        raise ModelResponseError("模型流的增量格式无效，本次输出未完成")
                    extension = stream_extensions(choice, delta)
                    if extension:
                        private_extensions.append(extension)
                    event = choice.get("server_tool_event")
                    if isinstance(event, dict) and event.get("type") == "step_reasoning_done":
                        last_step_reasoning_start = len(reasoning_parts)
                    if self._native_activity and adapter.provider == "deepseek" and event is not None:
                        from .providers.deepseek_research import ActivityProjector
                        if activity_projector is None:
                            activity_projector = ActivityProjector()
                        activity = activity_projector.feed(event)
                        if activity is not None:
                            yield ("provider_activity", activity)
                    for field in ("server_tool_calls", "annotations", "title", "refusal"):
                        if field in delta:
                            if field in final_fields:
                                raise ModelResponseError("模型流重复返回最终供应商上下文，本次响应协议无效")
                            final_fields[field] = delta[field]
                    if delta.get("reasoning") is not None and delta.get("reasoning_content") is None:
                        reasoning_field = "reasoning"
                    if delta.get("reasoning_content") is not None or delta.get("reasoning") is not None:
                        reasoning_seen = True
                    if finish_reason is not None and any(delta.get(field) for field in
                            ("content", "reasoning_content", "reasoning", "tool_calls")):
                        raise ModelResponseError("模型在结束状态后继续输出内容，本次响应协议无效")
                    rc = _response_text(delta, "reasoning_content") or _response_text(delta, "reasoning")
                    if rc:
                        reasoning_parts.append(rc)
                        yield ("delta", "thinking", rc)
                    content = _response_text(delta, "content")
                    if content:
                        content_parts.append(content)
                        yield ("delta", "answer", content)
                    raw_calls = delta.get("tool_calls")
                    if raw_calls is None:
                        raw_calls = []
                    if not isinstance(raw_calls, list):
                        raise ModelResponseError("模型流的工具调用格式无效，未执行工具")
                    for tc in raw_calls:
                        if not isinstance(tc, dict):
                            raise ModelResponseError("模型流的工具调用格式无效，未执行工具")
                        idx = tc.get("index", 0)
                        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                            raise ModelResponseError("模型流的工具序号无效，未执行工具")
                        slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            if not isinstance(tc["id"], str):
                                raise ModelResponseError("模型流的工具身份无效，未执行工具")
                            if slot["id"] and tc["id"] != slot["id"]:
                                raise ModelResponseError("模型流的工具身份发生冲突，未执行工具")
                            slot["id"] = tc["id"]
                        fn = tc.get("function")
                        if fn is None:
                            fn = {}
                        if not isinstance(fn, dict):
                            raise ModelResponseError("模型流的工具函数格式无效，未执行工具")
                        if fn.get("name"):
                            if not isinstance(fn["name"], str):
                                raise ModelResponseError("模型流的工具名称无效，未执行工具")
                            if slot["name"] and fn["name"] != slot["name"]:
                                raise ModelResponseError("模型流的工具名称发生冲突，未执行工具")
                            slot["name"] = fn["name"]
                        if fn.get("arguments") is not None:
                            if not isinstance(fn["arguments"], str):
                                raise ModelResponseError("模型流的工具参数格式无效，未执行工具")
                            slot["arguments"] += fn["arguments"]
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if finish_reason is not None and reason != finish_reason:
                            raise ModelResponseError("模型流返回了互相冲突的结束状态")
                        finish_reason = reason
                if not saw_done and finish_reason is None:
                    raise IncompleteModelResponse("模型连接在完整结束前中断，已生成内容保留，本次任务尚未完成")
                _check_finish_reason(finish_reason)
                tool_calls: list[dict] = []
                for idx in sorted(tool_acc):
                    slot = tool_acc[idx]
                    if not slot["name"]:
                        raise ModelResponseError("模型返回的工具名称不完整，未执行该工具")
                    tool_calls.append({"id": slot["id"] or f"call_{idx}",
                                       "name": slot["name"], "arguments": _tool_arguments(slot["arguments"])})
                if not content_parts and not tool_calls:
                    raise IncompleteModelResponse("模型未返回可交付的回答或工具调用，本次请求未完成")
                if finish_reason in ("tool_calls", "function_call") and not tool_calls:
                    raise ModelResponseError("模型声称调用工具但没有返回工具调用，本次请求未完成")
                if tool_calls:
                    from .model_capability_policy import CapabilityUnavailable
                    if capability(self._runtime_config, "tools") == "unsupported":
                        raise CapabilityUnavailable("tools")
                provider_payload = adapter.envelope({
                    "role": "assistant", "content": "".join(content_parts),
                    **({reasoning_field: "".join(reasoning_parts[last_step_reasoning_start:])} if reasoning_seen else {}),
                    **({"tool_calls": [{"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": tool_acc[index]["arguments"]}}
                        for index, call in zip(sorted(tool_acc), tool_calls)]} if tool_calls else {}),
                    **final_fields,
                }, stream_extensions=private_extensions)
        final = LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts),
            usage=stream_usage,
            provider_payload=provider_payload,
        )
        yield ("final", final)


# 向后兼容旧名称
LiteLLMClient = APIClient


def _safe_citation_title(title: str | None) -> str:
    """清洗嵌入《》的论文标题：移除会切断句子的句末标点与换行，
    避免标题里的“。！？.！?”破坏引用切句，导致半句无引用标记。"""
    t = (title or "该论文").strip()
    t = re.sub(r"[。.！？!?\n\r]", "", t).strip()
    return t or "该论文"


class MockLLM(BaseLLM):
    """
    离线脚本模型：
    1. 看到工具且尚未检索 -> 发起 retrieve_evidence 工具调用
    2. 看到工具返回的证据 -> 用证据原文拼接带 [^eN] 引用的答案
    3. 无证据 -> 拒答（真实走拒答路径，保证前端可演示）
    """

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.2, reasoning_effort: str | None = None) -> LLMResponse:
        last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
        tool_msgs = [m for m in messages if m["role"] == "tool"]

        # This is a scripted OFFLINE demo, not a classifier for the live agent.
        # Real API models all receive the same conversation/action loop.
        question = last_user.get("content", "") if last_user else ""
        plain = re.sub(r"[\s，,。.!！?？~～]+", "", question).casefold() if isinstance(question, str) else ""
        def ordinary_response(text):
            from .execution_mode import PLAN_RESPONSE_TOOL_NAME
            if (any(m["role"] == "system" and "[SCHOLARFLOW_PLAN_MODE]" in str(m.get("content", "")) for m in messages)
                    and any(spec.get("name") == PLAN_RESPONSE_TOOL_NAME for spec in tools or [])):
                return LLMResponse("", tool_calls=[{"id": "call_mock_direct_response",
                    "name": PLAN_RESPONSE_TOOL_NAME, "arguments": {"kind": "direct_response", "answer": text}}])
            return LLMResponse(text)
        if re.fullmatch(r"你好(?:呀|啊)?|您好|嗨|哈喽|hello|hi|hey", plain):
            return ordinary_response("你好！我是研序 ScholarFlow。当前为离线演示模式；配置在线模型后，可以正常对话并处理研究任务。")
        if re.fullmatch(r"谢谢(?:你|啦|了)?|多谢|感谢|thanks|thankyou", plain):
            return ordinary_response("不客气！有其他问题，随时告诉我。")
        if re.fullmatch(r"(?:你好(?:呀|啊)?|hello|hi)?你(?:用的?是|是)(?:什么|哪个)模型", plain):
            return ordinary_response("我是研序 ScholarFlow。当前使用离线演示模型 MockLLM，没有调用在线大模型。")

        if any(m["role"] == "system" and "[SCHOLARFLOW_PLAN_MODE]" in str(m.get("content", "")) for m in messages):
            plan_call_ids = {call.get("id") for message in messages if message["role"] == "assistant"
                             for call in message.get("tool_calls", [])
                             if call.get("function", {}).get("name") == "update_plan"}
            if (not any(m.get("tool_call_id") in plan_call_ids for m in tool_msgs)
                    and any(spec.get("name") == "update_plan" for spec in tools or [])):
                return LLMResponse("", tool_calls=[{
                    "id": "call_mock_plan", "name": "update_plan", "arguments": {"steps": [
                        {"title": "确认目标、输入与预期产出", "phase": "analyze"},
                        {"title": "读取当前任务确实需要的资料", "phase": "read"},
                        {"title": "按已审阅范围完成产出", "phase": "write"},
                        {"title": "核对产出与未解决问题", "phase": "audit"},
                    ]}}])
            return LLMResponse(content=(
                "（离线模式）待审阅计划\n\n"
                "1. 确认任务目标、输入材料与预期产出。\n"
                "2. 读取当前任务需要的资料，核对来源与缺失信息。\n"
                "3. 按审阅后的范围执行，遇到写入或外部操作时遵循审批设置。\n"
                "4. 检查产出、引用和未解决问题。\n\n"
                "上述步骤尚未执行。当前为离线示例，具体产出、修改范围和验证方式需接入模型后结合任务细化。"
            ))

        demo_retrieval = (isinstance(question, str) and re.search(
            r"论文|文献|原文|\b(?:paper|literature|transformer|attention|bleu)\b", question, re.I)) or any(
                m["role"] == "system" and "用户选择的论文标题：" in str(m.get("content", "")) for m in messages)
        if not demo_retrieval and not tool_msgs:
            return LLMResponse(content=(
                "当前使用离线演示模式，没有调用在线大模型，无法理解并完成任意请求。"
                "请在会话模型选择器中配置并选择在线模型后继续。"))

        if tools and not tool_msgs and last_user is not None:
            # 第一步：调用检索
            q = last_user["content"]
            return LLMResponse(
                content="",
                reasoning_content=(
                    f"先理解问题：用户想知道「{q}」。这是一个事实性问题，"
                    "我不能凭记忆作答，必须先在文献库里检索原文证据。"
                    "我将调用检索工具，按相关度取回最相关的段落，再据此组织带引用的答案。"
                ),
                tool_calls=[
                    {
                        "id": "call_mock_1",
                        "name": "retrieve_evidence",
                        "arguments": {"query": q, "k": settings.retrieve_topk},
                    }
                ],
            )

        # 第二步：基于证据作答
        evidences: list[dict] = []
        for tm in tool_msgs:
            try:
                payload = json.loads(tm["content"]) if isinstance(tm["content"], str) else tm["content"]
                evidences.extend(payload.get("evidence", []))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        if not evidences:
            return LLMResponse(
                content="（离线模式）知识库中没有检索到支持该问题的原文证据，按抗幻觉规则拒绝回答。请先上传相关论文，或在「模型与设置」中配置大模型接口后重试。"
            )

        lines = ["（离线模式，未配置大模型 API Key；以下答案由检索到的原文证据直接拼接）"]
        for i, ev in enumerate(evidences[:3], 1):
            raw = ev["quote"].strip().replace("\n", " ")
            # 每条证据只引一句，引用标记逐句附加（与真实 LLM 的规范输出一致）
            sentences = split_sentences(raw)
            quote = sentences[0] if sentences else raw
            if len(quote) > 200:
                quote = quote[:200] + "…"
            safe_title = _safe_citation_title(ev.get("paper_title"))
            lines.append(f"根据《{safe_title}》第 {ev['page']} 页原文：{quote}[^e{i}]")
        lines.append("配置真实大模型后，这里会输出由模型基于证据组织的自然语言答案与跨论文对比。")
        titles = sorted({_safe_citation_title(e.get("paper_title")) for e in evidences[:3]})
        return LLMResponse(
            content="\n\n".join(lines),
            reasoning_content=(
                f"检索完成，命中 {len(evidences)} 条相关段落，来自《{('》、《').join(titles)}》。"
                "我逐句核对原文，只保留能直接支撑问题的内容，并为每个论断标注证据编号；"
                "没有原文支撑的内容一律不写。现在整理最终答案。"
            ),
        )

    def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                    reasoning_effort: str | None = None):
        """离线流式：复用 chat 的结果，把思考/答案切块逐字吐出，再 yield 汇总。"""
        import os
        import time

        # 测试/演示节流：MOCK_STREAM_DELAY 为每个切块之间的秒数（默认 0，生产无影响）
        delay = float(os.environ.get("MOCK_STREAM_DELAY", "0") or 0)
        resp = self.chat(messages, tools=tools, reasoning_effort=reasoning_effort)
        # 离线模式没有真实用量，做一次估算以便演示与统计
        resp.usage = resp.usage or estimate_usage(messages, resp)

        def _emit(kind, text):
            for piece in _chunk(text):
                yield ("delta", kind, piece)
                if delay:
                    time.sleep(delay)

        if resp.reasoning_content:
            yield from _emit("thinking", resp.reasoning_content)
        # 工具调用轮没有正文；最终轮才逐字输出答案
        if resp.content and not resp.tool_calls:
            yield from _emit("answer", resp.content)
        yield ("final", resp)


_llm = None


def get_llm() -> BaseLLM:
    global _llm
    from .model_profiles import ModelConfigurationError
    from .model_readiness import MISSING_CLOUD_KEY
    binding = current_runtime_config()
    if binding is not None:
        if binding.get("mode") == "api":
            return APIClient(runtime_config=binding)
        if binding.get("mode") == "mock":
            return MockLLM()
        raise ModelConfigurationError("任务模型模式尚未配置，请重新选择模型")
    # Standalone writing/translation/caption entry points do not have a run
    # binding. Respect the selected named default rather than silently reading
    # the legacy form. Named/default-demo capture is read-only; legacy capture
    # would create a credential snapshot, so it stays on the existing path.
    from .db import SessionLocal
    from .model_profiles import LEGACY_PROFILE_ID, default_profile_id, capture_run_config
    with SessionLocal() as db:
        selected_profile_id = default_profile_id(db)
        if selected_profile_id != LEGACY_PROFILE_ID:
            selected = capture_run_config(db, None, selected_profile_id=selected_profile_id)
            return APIClient(runtime_config=selected) if selected["mode"] == "api" else MockLLM()
    from .config import require_available_credentials
    require_available_credentials("llm_api_key")
    if _llm is not None:
        return _llm
    if settings.llm_mode in ("api", "litellm"):
        _llm = APIClient()
    elif settings.llm_mode == "mock":
        _llm = MockLLM()
    else:
        raise ModelConfigurationError(MISSING_CLOUD_KEY)
    return _llm


def reset_llm() -> None:
    """配置变更后调用，丢弃缓存的客户端，下次 get_llm 重新创建。"""
    global _llm
    _llm = None
