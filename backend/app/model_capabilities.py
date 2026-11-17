"""User-initiated, configuration-bound probes through the production adapter."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import hmac
import io
import json
import secrets
import re
from uuid import uuid4

import httpx
from sqlalchemy import text

from . import model_profiles as profiles
from .call_ledger import list_calls, summarize_calls
from .db import SessionLocal
from .llm import APIClient, ModelResponseError
from .models import DesktopState
from .runtime_context import bind_runtime_config
from .timeutil import utcnow

_KEY = secrets.token_bytes(32)
_OWNER = uuid4().hex
PROBES = {
    "text": ("文本回答", 1), "streaming": ("流式输出", 1),
    "tools": ("三轮工具往返", 4), "vision": ("图片理解", 1), "reasoning": ("推理字段", 1),
    "structured": ("JSON 对象输出", 1), "usage": ("聊天用量", 1),
    "native_tools": ("原生搜索与链接阅读", 2), "files": ("原生文件理解", 3),
}


class ConfigurationChanged(profiles.ModelConfigurationError):
    pass


class ProbeNotConfirmed(ValueError):
    pass


class ProbeCancelled(ValueError):
    request_cancelled = True


class ProtocolUnavailable(ValueError):
    pass


def _operation_key(diagnostic_id):
    if not isinstance(diagnostic_id, str) or not re.fullmatch(r"capability-[a-f0-9]{32}", diagnostic_id):
        raise profiles.ModelConfigurationError("诊断编号无效", 422)
    return "model-capability-operation:" + diagnostic_id


def cancel(profile_id, diagnostic_id):
    """A tombstone also fences cancellation which arrives before the POST."""
    key = _operation_key(diagnostic_id)
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(DesktopState, key)
        value = deepcopy(row.value or {}) if row else {"profile_id": profile_id}
        if value.get("profile_id") != profile_id:
            raise profiles.ModelConfigurationError("此诊断不属于当前模型", 404)
        if value.get("state") != "finished":
            value["state"] = "cancelled"
            if row:
                row.value, row.revision = value, row.revision + 1
            else:
                db.add(DesktopState(key=key, value=value, revision=1))
        db.commit()
    from . import cancel_store
    cancel_store.request_cancel(diagnostic_id)
    return {"diagnostic_id": diagnostic_id, "cancel_requested": value.get("state") == "cancelled",
            "detail": "已请求取消后续测试；已经发送的请求可能仍由供应商执行和计费。"}


def _start(profile_id, capability, diagnostic_id):
    key = _operation_key(diagnostic_id)
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(DesktopState, key)
        if row:
            if (row.value or {}).get("profile_id") == profile_id and (row.value or {}).get("state") == "cancelled":
                raise ProbeCancelled("本项测试已取消，未开始新的请求")
            raise profiles.ModelConfigurationError("该诊断已开始或结束，不会重复发送请求")
        values = _values(db, profile_id)
        db.add(DesktopState(key=key, revision=1, value={"profile_id": profile_id, "capability": capability,
            "configuration": _public_binding(values), "state": "running", "owner": _OWNER,
            "started_at": utcnow().isoformat()}))
        db.commit()


def _public_binding(values):
    return {key: value for key, value in values.items() if key != "api_key"}


def cleanup(profile_id, diagnostic_id):
    key = _operation_key(diagnostic_id)
    with SessionLocal() as db:
        row = db.get(DesktopState, key)
        operation = deepcopy(row.value or {}) if row else {}
        values = _values(db, profile_id)
    if operation.get("profile_id") != profile_id or operation.get("state") != "finished":
        raise profiles.ModelConfigurationError("请等待本项诊断结束后清理对应测试文件")
    remote = operation.get("remote_file") or {}
    if remote.get("state") == "deleted":
        return {"remote_file": remote, "calls": [], "request_count": 0}
    if not remote.get("id"):
        raise profiles.ModelConfigurationError("远端文件身份未确认，不能猜测并删除；请在供应商控制台核对或等待所请求的过期时间")
    def authorized():
        with SessionLocal() as db:
            return _public_binding(_values(db, profile_id)) == operation.get("configuration")
    if not authorized():
        raise ConfigurationChanged("原测试配置或密钥已变化；请在原供应商控制台核对清理，未使用旧密钥")
    from .providers.deepseek_files import FilesClient
    cleanup_id = diagnostic_id + "-cleanup-" + uuid4().hex[:8]
    with bind_runtime_config({"run_id": cleanup_id}):
        FilesClient(values["api_key"], authorization_check=authorized, synthetic=bool(operation.get("synthetic"))).delete(remote["id"])
    remote = {**remote, "state": "deleted"}
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(DesktopState, key)
        row.value, row.revision = {**row.value, "remote_file": remote}, row.revision + 1
        db.commit()
    calls = list_calls(run_id=cleanup_id, include_synthetic=True)["items"]
    return {"diagnostic_id": diagnostic_id, "cleanup_diagnostic_id": cleanup_id,
            "remote_file": remote, "calls": calls, "request_count": len(calls),
            "cost_summary": summarize_calls(run_id=cleanup_id, include_synthetic=True)}


def _record_remote(client, value):
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(DesktopState, _operation_key(client.diagnostic_id))
        row.value = {**(row.value or {}), "remote_file": value, "synthetic": bool(client._synthetic)}
        row.revision += 1
        db.commit()
    client.remote_file = deepcopy(value)


def _values(db, profile_id):
    if profile_id in (profiles.LEGACY_PROFILE_ID, profiles.DEMO_PROFILE_ID):
        raise profiles.ModelConfigurationError("请选择独立模型档案进行分能力诊断")
    row = profiles.get_profile(db, profile_id, usable=True)
    public = profiles.profile_snapshot(row)
    if public.get("configuration_error"):
        raise profiles.ModelConfigurationError(public["configuration_error"])
    return {"profile_id": row.id, "version": row.version, "base_url": row.base_url,
            "model_id": row.model_id, "reasoning_effort": row.reasoning_effort,
            **profiles.context_limits({"context_window_tokens": row.context_window_tokens, "max_output_tokens": row.max_output_tokens}),
            "context_limits_source": public["context_limits_source"],
            "credential_ref": row.credential_ref, "api_key": profiles.read_credential(db, row.credential_ref)}


def _digest(values):
    return hmac.new(_KEY, json.dumps(values, ensure_ascii=False, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def preview(profile_id):
    with SessionLocal() as db:
        values = _values(db, profile_id)
        row = profiles.get_profile(db, profile_id)
        state = db.get(DesktopState, f"model-capabilities:{profile_id}")
        results = deepcopy((state.value or {}).get("results", {})) if state else {}
        results = {name: result for name, result in results.items() if result.get("profile_version") == row.version}
        pending = []
        repaired = False
        for operation in db.query(DesktopState).filter(DesktopState.key.like("model-capability-operation:%")).order_by(DesktopState.updated_at.desc()).all():
            saved = operation.value or {}
            remote = saved.get("remote_file") or {}
            if (saved.get("profile_id") == profile_id and saved.get("state") in ("running", "cancelled")
                    and saved.get("started_at") and saved.get("owner") != _OWNER):
                # The desktop backend has one owner per data directory. After
                # its restart, an old diagnostic must not resume paid calls or
                # disappear together with a known remote test-file identity.
                if remote.get("id") and remote.get("state") != "deleted":
                    remote = {**remote, "state": "cleanup_pending"}
                saved = {**saved, "state": "finished", "interrupted": True, "remote_file": remote}
                operation.value, operation.revision = saved, operation.revision + 1
                repaired = True
            if saved.get("profile_id") == profile_id and saved.get("state") == "finished" and remote.get("state") in ("upload_pending", "available", "cleanup_pending"):
                pending.append({"diagnostic_id": operation.key.split(":", 1)[1], "remote_file": remote})
        if repaired:
            db.commit()
        return {"profile_id": profile_id, "model_id": values["model_id"], "profile_version": values["version"],
                "context_limits": profiles.public_context_limits(values),
                "protocol": "openai_chat", "expected_config": _digest(values), "remote_requests": 0,
                "capabilities": profiles.profile_snapshot(row)["capabilities"], "results": results,
                "cleanup_operations": pending[:20],
                "probes": [{"capability": name, "label": label, "max_requests": limit,
                            "sends": ("固定合成图片" if name == "vision" else
                                "合成 Markdown 测试文件，上传后读取并删除；未清理时请求 1 小时过期" if name == "files" else
                                "固定公开查询与文档链接；分别核对搜索和阅读活动" if name == "native_tools" else
                                "固定合成测试文本与工具结果")}
                           for name, (label, limit) in PROBES.items()]}


class _Client(APIClient):
    def __init__(self, values, expected, diagnostic_id):
        super().__init__(timeout=25, context_limits=values, authorization_check=self.authorized, native_activity=True)
        self.values, self.expected, self.requests = values, expected, 0
        self.diagnostic_id, self.remote_file, self.observations = diagnostic_id, None, {}

    def authorized(self):
        self.check()
        return True

    def check(self):
        with SessionLocal() as db:
            operation = db.get(DesktopState, _operation_key(self.diagnostic_id))
            if not operation or (operation.value or {}).get("state") != "running":
                raise ProbeCancelled("本项测试已取消，未继续发送或读取测试结果")
            try:
                current = _values(db, self.values["profile_id"])
            except Exception:
                raise ConfigurationChanged("模型配置或凭证已变化，请刷新后重新选择测试") from None
        if not hmac.compare_digest(_digest(current), self.expected):
            raise ConfigurationChanged("模型配置已变化，请刷新后重新选择测试")

    def _connection(self, reasoning_effort):
        self.check()
        self.requests += 1
        if self.requests > 4:
            raise ProbeNotConfirmed("ProbeRequestLimit")
        value = self.values
        return value["base_url"], value["api_key"], value["model_id"], (
            value["reasoning_effort"] if reasoning_effort is None else reasoning_effort)

    def _authorize_request(self, base, key, model):
        self.check()


def _official(client):
    from .providers.chat import adapter_for
    value = client.values
    if adapter_for(value["base_url"], value["model_id"], value["api_key"]).provider != "deepseek":
        raise ProtocolUnavailable("当前已实现的原生工具和文件协议仅适用于 DeepSeek 官方端点；独立搜索/OCR 服务不计为模型自带能力。")


def _native_probe(client):
    _official(client)
    completed = []
    diagnostics = []
    client.observations["native_activity_diagnostics"] = diagnostics
    for name, query in (("deepseek_web_search", "Use the native search tool to find the DeepSeek API documentation. This is a capability test."),
                        ("deepseek_read_url", "Use the native read URL tool to read https://api-docs.deepseek.com/zh-cn/guides/search . This is a capability test.")):
        client._provider_options = {"native_tools": [name]}
        final, confirmed = None, False
        observed = []
        for event in client.stream_chat([{"role": "user", "content": query}]):
            client.check()
            if event[0] == "provider_activity" and event[1].get("tool") == name and event[1].get("status") == "completed":
                confirmed = True
            if event[0] == "provider_activity":
                # Public enum-only diagnostics: no answer, provider thought,
                # page signature, URL body or unrecognized field values.
                activity = event[1]
                observed.append({"tool": activity.get("tool") if activity.get("tool") in
                    ("deepseek_web_search", "deepseek_read_url") else "other",
                    "status": activity.get("status") if activity.get("status") in
                    ("started", "completed", "failed", "unknown") else "other"})
            elif event[0] == "final":
                final = event[1]
        payload = (getattr(final, "provider_payload", None) or {})
        extensions = payload.get("stream_extensions") or []
        summaries = []
        for extension in extensions if isinstance(extensions, list) else []:
            choice = extension.get("choice") if isinstance(extension, dict) else None
            value = choice.get("server_tool_event") if isinstance(choice, dict) else None
            if not isinstance(value, dict): continue
            call = value.get("call")
            summaries.append({"type": value.get("type") if value.get("type") in
                ("step_reasoning_done", "call_started", "call_completed") else "other",
                "call_object": isinstance(call, dict),
                "call_has_type": isinstance(call, dict) and "type" in call,
                "call_has_status": isinstance(call, dict) and "status" in call,
                "event_has_status": "status" in value})
        raw_assistant = payload.get("assistant") or {}
        final_steps = raw_assistant.get("server_tool_calls")
        diagnostics.append({"tool": name, "public_activities": observed[:32], "activity_count": len(observed),
            "wire_event_shapes": summaries[:32], "wire_event_count": len(summaries),
            "final_has_content": bool(final and final.content.strip()),
            "final_has_local_calls": bool(final and final.tool_calls),
            "final_native_steps": len(final_steps) if isinstance(final_steps, list) else None})
        if not confirmed or final is None or not final.content.strip() or final.tool_calls:
            raise ProbeNotConfirmed("NativeExecutionNotConfirmed")
        completed.append(name)
        client.observations["confirmed_native_tools"] = completed[:]


def _file_probe(client):
    _official(client)
    from .providers.deepseek_files import FilesClient
    files = FilesClient(client.values["api_key"], authorization_check=client.authorized, synthetic=client._synthetic)
    token = "synthetic-" + secrets.token_hex(12)
    # The answer token is absent from the question and filename. Success must
    # come from this uploaded material, not from echoing the prompt.
    raw = ("# Synthetic capability test\nToken: " + token + "\n").encode()
    _record_remote(client, {"state": "upload_pending", "requested_ttl_seconds": 3600,
                            "cleanup_note": "上传若中断，远端状态未知；未自动重发。已请求一小时过期。"})
    try:
        uploaded = files.upload(raw, "capability-test.md", "user_data", 3600)
        _record_remote(client, {"state": "available", "id": uploaded["id"], "expires_at": uploaded.get("expires_at"),
            "requested_ttl_seconds": 3600, "cleanup_note": "测试后删除；取消或撤权时保留状态供主动清理，不继续使用旧授权。"})
        client.check()
        response = client.chat([{"role": "user", "content": [
            {"type": "text", "text": "Return only the Token value from the attached synthetic document."},
            {"type": "file", "file_id": uploaded["id"]}]}])
        if response.content.strip() != token or response.tool_calls:
            raise ProbeNotConfirmed("FileMeaningNotConfirmed")
    finally:
        if client.remote_file and client.remote_file.get("id"):
            try:
                client.check()
                files.delete(client.remote_file["id"])
                _record_remote(client, {**client.remote_file, "state": "deleted"})
            except Exception:
                # A revoked key is never reused merely for cleanup. Persist the
                # known ID; TTL is requested, not a promise of remote deletion.
                _record_remote(client, {**client.remote_file, "state": "cleanup_pending"})


def _probe(client, capability):
    if capability == "native_tools":
        return _native_probe(client)
    if capability == "files":
        return _file_probe(client)
    messages = [{"role": "user", "content": "Reply with OK. This is a synthetic capability test."}]
    if capability == "streaming":
        deltas, final = 0, None
        for event in client.stream_chat(messages):
            client.check()
            if event[0] == "delta" and event[1] == "answer" and event[2]:
                deltas += 1
            elif event[0] == "final":
                if final is not None:
                    raise ProbeNotConfirmed("RepeatedFinal")
                final = event[1]
        if not deltas or final is None or not final.content.strip() or final.tool_calls:
            raise ProbeNotConfirmed("StreamingNotConfirmed")
        return
    if capability == "tools":
        messages[0]["content"] = (
            "Synthetic tool protocol test. Call capability_echo exactly once with stage=1; "
            "after its result call it once with stage=2; then once with stage=3. "
            "After receiving stage 3, reply OK without another tool call. Do not combine stages.")
        tools = [{"name": "capability_echo", "description": "Returns the current synthetic test stage.",
                  "parameters": {"type": "object", "properties": {"stage": {"type": "integer", "minimum": 1, "maximum": 3}},
                                 "required": ["stage"], "additionalProperties": False}}]
        for stage in range(1, 4):
            result = client.chat(messages, tools=tools)
            if len(result.tool_calls) != 1:
                raise ProbeNotConfirmed("ToolRoundNotConfirmed")
            call = result.tool_calls[0]
            if call.get("name") != "capability_echo" or call.get("arguments") != {"stage": stage} or not call.get("id"):
                raise ProbeNotConfirmed("ToolArgumentsNotConfirmed")
            messages.extend([result.assistant_message(), {"role": "tool", "tool_call_id": call["id"],
                                                         "content": json.dumps({"stage": stage, "result": "synthetic"})}])
        result = client.chat(messages, tools=tools)
    elif capability == "vision":
        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (32, 32), (255, 0, 0)).save(image, format="PNG")
        messages[0]["content"] = [
            {"type": "text", "text": "What is the solid square's color? Reply with its English color name only."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()}}]
        result = client.chat(messages)
        if "red" not in result.content.casefold() and "红" not in result.content:
            raise ProbeNotConfirmed("ImageMeaningNotConfirmed")
    elif capability == "structured":
        client._provider_options = {"response_format": {"type": "json_object"}}
        messages[0]["content"] = 'Return a JSON object exactly {"answer":17,"ok":true}. This tests JSON object output, not JSON Schema.'
        result = client.chat(messages)
        value = json.loads(result.content)
        if (not isinstance(value, dict) or set(value) != {"answer", "ok"}
                or type(value["answer"]) is not int or value["answer"] != 17 or value["ok"] is not True):
            raise ProbeNotConfirmed("JSONMeaningNotConfirmed")
        client.observations["structured_scope"] = "json_object_sample_not_schema_or_enforcement_guarantee"
    elif capability == "usage":
        result = client.chat(messages)
        usage = result.usage
        if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
                or usage["prompt_tokens"] < 1 or usage["completion_tokens"] < 1
                or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
            raise ProbeNotConfirmed("UsageNotConfirmed")
        client.observations["usage_scope"] = "chat_nonstream_tokens"
    elif capability == "reasoning":
        messages[0]["content"] = "What is 17 * 23? Give the result. This is a synthetic reasoning-field test."
        result = client.chat(messages, reasoning_effort="high")
        if not result.reasoning_content.strip() or "391" not in result.content:
            raise ProbeNotConfirmed("ReasoningFieldNotConfirmed")
    else:
        result = client.chat(messages)
    if not result.content.strip() or result.tool_calls:
        raise ProbeNotConfirmed("TextNotConfirmed")


def _failure(exc, capability):
    if isinstance(exc, ProbeCancelled) or type(exc).__name__ == "ResourceRequestCancelled":
        return "unknown", "cancelled", "测试已取消；未继续发送后续测试。已发送的请求可能仍在远端执行和计费。"
    if isinstance(exc, ProtocolUnavailable):
        return "unsupported", "adapter_protocol", str(exc)
    if isinstance(exc, ConfigurationChanged):
        return "unknown", "configuration_changed", "配置已变化，旧测试结果不写入当前模型。"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "unknown", "authentication", "认证或访问权限失败，不能据此判断模型能力。"
        if status == 429:
            return "unknown", "rate_limit", "接口限流，本项能力尚未确认。"
        if status == 402:
            return "unknown", "balance", "服务额度不足，本项能力尚未确认。"
        try:
            error = exc.response.json().get("error", {})
            if error.get("code") in ("capability_not_supported", "unsupported_feature") and error.get("param") == capability:
                return "unsupported", "capability", "供应商明确报告不支持此能力。"
        except (ValueError, AttributeError):
            pass
        return "unknown", "provider", f"接口返回 HTTP {status}；未将请求失败推断为能力不支持。"
    if isinstance(exc, httpx.HTTPError):
        return "unknown", "transport", "网络或流式连接未正常结束，本项能力尚未确认。"
    if isinstance(exc, ModelResponseError):
        return "unknown", "protocol", "接口响应未满足协议完整性要求。"
    from .providers.deepseek_files import FilesError
    if isinstance(exc, FilesError):
        kind = {401: "authentication", 403: "authentication", 402: "balance", 429: "rate_limit"}.get(exc.status_code,
            "transport" if exc.code in ("transport", "deadline") else "configuration_changed" if exc.code == "authorization" else "protocol")
        return "unknown", kind, "文件测试未完成，已保存远端状态；未把服务、额度或连接失败判断为模型不支持。"
    return "unknown", "not_confirmed", "本次响应未完成该项测试要求，可核对配置后重试。"


def run(profile_id, capability, expected_config, diagnostic_id=None):
    if capability not in PROBES:
        raise profiles.ModelConfigurationError("此能力尚无可执行诊断，不会发送测试请求", 422)
    with SessionLocal() as db:
        values = _values(db, profile_id)
    if not hmac.compare_digest(_digest(values), expected_config):
        raise ConfigurationChanged("模型配置已变化，请刷新后重新选择测试")
    diagnostic_id = diagnostic_id or "capability-" + uuid4().hex
    try:
        _start(profile_id, capability, diagnostic_id)
    except ProbeCancelled:
        return {"diagnostic_id": diagnostic_id, "profile_id": profile_id, "profile_version": values["version"],
                "model_id": values["model_id"], "capability": capability, "status": "unknown", "failure_kind": "cancelled",
                "detail": "本项测试已取消，未发送请求。", "tested_at": utcnow().isoformat(), "request_count": 0, "calls": []}
    client = _Client(values, expected_config, diagnostic_id)
    status, kind, detail = "supported", "confirmed", "本次合成测试通过；结果仅适用于当前模型配置。"
    with bind_runtime_config({"run_id": diagnostic_id}):
        try:
            _probe(client, capability)
            client.check()
        except Exception as exc:
            status, kind, detail = _failure(exc, capability)
    if status == "supported" and capability == "structured":
        detail = "JSON 对象样例正确返回；不代表严格 Schema 或服务端格式强制已验证。"
    calls = list_calls(run_id=diagnostic_id, include_synthetic=True)["items"]
    result = {"diagnostic_id": diagnostic_id, "profile_id": profile_id, "profile_version": values["version"],
              "model_id": values["model_id"], "capability": capability, "status": status, "failure_kind": kind,
              "endpoint": values["base_url"] + "/chat/completions", "protocol": "openai_chat",
              "detail": detail, "tested_at": utcnow().isoformat(), "request_count": len(calls), "calls": calls,
              "cost_summary": summarize_calls(run_id=diagnostic_id, include_synthetic=True),
              "observations": client.observations, "remote_file": client.remote_file}
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        operation = db.get(DesktopState, _operation_key(diagnostic_id))
        cancelled = not operation or (operation.value or {}).get("state") == "cancelled"
        try:
            current = _values(db, profile_id)
        except Exception:
            current = None
        if cancelled:
            result.update(status="unknown", failure_kind="cancelled", detail="测试已取消，结果不覆盖已有能力；已发请求费用仍记录在账本。")
        elif current is None or not hmac.compare_digest(_digest(current), expected_config):
            result.update(status="unknown", failure_kind="configuration_changed", detail="配置已变化，旧测试结果不写入当前模型。")
        if operation:
            operation.value = {**(operation.value or {}), "state": "finished", "result": {k: v for k, v in result.items() if k != "calls"}}
            operation.revision += 1
        if result["failure_kind"] in ("cancelled", "configuration_changed"):
            db.commit()
            return result
        row = profiles.get_profile(db, profile_id)
        row.capabilities = {**(row.capabilities or {}), capability: result["status"]}
        key = f"model-capabilities:{profile_id}"
        saved = db.get(DesktopState, key)
        results = deepcopy((saved.value or {}).get("results", {})) if saved else {}
        results[capability] = {k: v for k, v in result.items() if k != "calls"}
        if saved:
            saved.value, saved.revision, saved.updated_at = {"results": results}, saved.revision + 1, utcnow()
        else:
            db.add(DesktopState(key=key, value={"results": results}, revision=1))
        db.commit()
    return result
