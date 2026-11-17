"""Deterministic active-request compaction with exact checkpoint references.

No model-generated summary replaces evidence. Original messages are retained
in the checkpoint; signed/provider-native dialogue blocks are never rewritten.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math


class ContextCapacityExceeded(ValueError):
    def __init__(self, message, *, stage_reason=None):
        super().__init__(message)
        # Only deterministic pre-dispatch capacity/scope checks may request
        # automatic stage renewal. An arbitrary provider error is not replay.
        self.stage_reason = stage_reason


CONTEXT_TOOL = "read_context_archive"
CONTEXT_TOOL_SPEC = {"name": CONTEXT_TOOL, "description":
    "按当前任务上下文索引回查被压缩的原始工具正文；需保留当前来源授权。返回原始文字，不能把索引当作证据。",
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {"reference": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                       "length": {"type": "integer", "minimum": 1, "maximum": 8000}}, "required": ["reference"]}}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def estimate_tokens(messages, tools=None):
    # A conservative multilingual estimate, explicitly not vendor accounting.
    return math.ceil(len(_json([messages, tools or []]).encode("utf-8")) / 2) + 256


def scope_fingerprint(binding, workspace=None):
    binding = binding or {}
    return hashlib.sha256(_json({"conversation_id": binding.get("conversation_id"),
        "source_bindings": binding.get("source_bindings", []), "source_revision_ids": binding.get("source_revision_ids", []),
        "policy_revision": binding.get("policy_revision"), "memory_fingerprint": binding.get("memory_fingerprint"),
        "workspace": workspace}).encode()).hexdigest()


def _frozen_tool_block(message):
    def opaque(value):
        if isinstance(value, dict):
            return any(key in {"signature", "server_tool_calls", "encrypted_content", "redacted_thinking", "signed_payload"}
                       or opaque(item) for key, item in value.items())
        if isinstance(value, list):
            return any(opaque(item) for item in value)
        return False
    return opaque(message.get("_provider")) or (message.get("role") == "assistant" and opaque({
        key: value for key, value in message.items() if key != "_provider"}))


def compact_messages(messages, *, tools=None, binding=None, workspace=None, archive=None,
                     window_tokens=None, output_reserve=None):
    binding = binding or {}
    from ..model_context_limits import context_limits
    configured = {**binding,
        **({"context_window_tokens": window_tokens} if window_tokens is not None else {}),
        **({"max_output_tokens": output_reserve} if output_reserve is not None else {})}
    try:
        limits = context_limits(configured)
    except ValueError as exc:
        raise ContextCapacityExceeded(str(exc)) from None
    window, reserve = limits["context_window_tokens"], limits["max_output_tokens"]
    available = window - reserve
    outgoing = deepcopy(messages)
    archive = archive if archive is not None else {}
    current_scope = scope_fingerprint(binding, workspace)
    for reference in list(archive):
        if archive[reference].get("scope_fingerprint") != current_scope:
            archive.pop(reference)
    before = estimate_tokens(outgoing, tools)
    manifest = {"window_tokens": window, "output_reserved_tokens": reserve,
        "estimated_input_tokens_before": before, "estimate_method": "utf8_bytes_divided_by_two_plus_framing",
        "scope_fingerprint": current_scope, "entries": [],
        "raw_messages_retained": True, "provider_blocks_preserved": True}
    calls, protected = {}, set()
    for index, message in enumerate(outgoing):
        for call in message.get("tool_calls", []) if isinstance(message.get("tool_calls"), list) else []:
            function = call.get("function", {})
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except (ValueError, TypeError):
                arguments = {}
            calls[call.get("id")] = {"tool_name": function.get("name"), "arguments": arguments}
            if _frozen_tool_block(message):
                protected.add(call.get("id"))
    tool_indexes = [i for i, m in enumerate(outgoing) if m.get("role") == "tool"]
    allowed_sources = set(binding.get("source_revision_ids", []))
    for index in tool_indexes:
        call = calls.get(outgoing[index].get("tool_call_id"), {})
        if (call.get("tool_name") == "read_attached_source"
                and call.get("arguments", {}).get("source_revision_id") not in allowed_sources):
            if outgoing[index].get("tool_call_id") in protected or outgoing[index].get("_provider"):
                raise ContextCapacityExceeded("来源授权已变化，原生或签名工具块不能截段改写；已保留任务，需建立安全新阶段后继续",
                                              stage_reason="source_scope")
            outgoing[index]["content"] = _json({"source_unavailable": True,
                "instruction": "该来源不再属于当前授权范围，原文不可用于本轮。"})
    if estimate_tokens(outgoing, tools) <= available:
        manifest["estimated_input_tokens_after"] = estimate_tokens(outgoing, tools)
        return outgoing, manifest
    for index in tool_indexes[:-2]:
        message = outgoing[index]
        call_id = message.get("tool_call_id")
        content = message.get("content")
        if (call_id in protected or message.get("_provider") or not isinstance(content, str)
                or len(content) < 1800 or call_id not in calls):
            continue
        call = calls[call_id]
        digest = hashlib.sha256(content.encode()).hexdigest()
        reference = hashlib.sha256(_json([index, call_id, digest, manifest["scope_fingerprint"]]).encode()).hexdigest()
        archive[reference] = {"content": content, "sha256": digest, "message_index": index,
            "scope_fingerprint": manifest["scope_fingerprint"], **deepcopy(call)}
        entry = {"context_archive_ref": reference, "sha256": digest, "original_characters": len(content), **call,
            "instruction": "原始工具正文已保留。需要细节时调用 read_context_archive；此索引不构成证据或任务完成证明。"}
        message["content"] = _json(entry)
        manifest["entries"].append({"reference": reference, "message_index": index, "sha256": digest})
        if estimate_tokens(outgoing, tools) <= available:
            break
    manifest["estimated_input_tokens_after"] = estimate_tokens(outgoing, tools)
    if manifest["estimated_input_tokens_after"] > available:
        raise ContextCapacityExceeded("保留用户要求、纠偏和供应商协议块后仍超过模型上下文窗口；已保留原文，需要扩大窗口或建立可恢复的新阶段",
                                      stage_reason="native_capacity" if any(_frozen_tool_block(m) for m in messages) else None)
    return outgoing, manifest


def read_archive(archive, arguments, *, binding=None, workspace=None, authorize=None):
    entry = archive.get(arguments.get("reference"))
    if entry is None:
        raise ValueError("上下文索引不存在")
    if entry["scope_fingerprint"] != scope_fingerprint(binding, workspace):
        raise ValueError("来源或权限范围已变化，旧上下文索引不可回填")
    if authorize is None or not authorize(entry["tool_name"], entry["arguments"]):
        raise ValueError("当前无法重新核验该工具来源授权，请通过原工具重新读取")
    content = entry["content"]
    if hashlib.sha256(content.encode()).hexdigest() != entry["sha256"]:
        raise ValueError("原始工具正文校验失败")
    offset, length = arguments.get("offset", 0), arguments.get("length", 6000)
    if type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= 8000:
        raise ValueError("上下文回查范围无效")
    return {"content": content[offset:offset + length], "offset": offset, "total_characters": len(content),
        "next_offset": offset + length if offset + length < len(content) else None,
        "sha256": entry["sha256"], "original_tool": entry["tool_name"], "historical_tool_result": True}


_VOLATILE = {"timestamp", "created_at", "updated_at", "fetched_at", "elapsed_ms", "elapsed_seconds", "duration_ms",
             "duration_seconds", "request_id", "trace_id", "provider_request_id", "usage"}


def progress_fingerprint(name, arguments, raw_result):
    """Evidence/range progress, independent of tool order and timestamp noise."""
    if name in ("update_plan", CONTEXT_TOOL):
        return None
    try:
        payload = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except (ValueError, TypeError):
        payload = raw_result
    if isinstance(payload, dict) and any(payload.get(key) for key in (
            "error", "already_executed", "not_replayed", "execution_unknown", "blocked_by_tool_policy",
            "blocked_by_live_policy", "blocked_by_plan_mode", "denied_by_user")):
        return None
    def stable(value, parent=None):
        if isinstance(value, dict):
            return {k: stable(v, k) for k, v in value.items() if k not in _VOLATILE
                    and not (parent == "evidence" and k == "id")
                    and not (name == "search_project_files" and k == "snapshot")}
        if isinstance(value, list):
            items = [stable(v, parent) for v in value]
            return sorted(items, key=_json) if parent in ("evidence", "papers", "results", "matches") else items
        return value
    ranges = {key: value for key, value in (arguments or {}).items() if key in
        ("source_revision_id", "paper_id", "path", "page_index", "line_start", "line_end", "paragraph_start",
         "paragraph_end", "sheet", "cell_range", "offset", "snapshot", "version")}
    if name == "search_project_files":
        ranges.pop("snapshot", None)  # Query options alone do not create evidence.
    return hashlib.sha256(_json({"tool": name, "range": ranges, "result": stable(payload)}).encode()).hexdigest()
