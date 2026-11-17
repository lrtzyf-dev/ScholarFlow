"""Declared model limits; never inferred from a model ID or a model listing."""
DEFAULT_CONTEXT_WINDOW_TOKENS = 131072
DEFAULT_MAX_OUTPUT_TOKENS = 8192
MAX_CONTEXT_WINDOW_TOKENS = 16777216
MAX_OUTPUT_TOKENS = 1048576


def context_limits(values=None):
    if values is not None and not isinstance(values, dict):
        raise ValueError("模型 token 限制配置无效")
    values = values or {}
    limits = {}
    for key, default, maximum in (
        ("context_window_tokens", DEFAULT_CONTEXT_WINDOW_TOKENS, MAX_CONTEXT_WINDOW_TOKENS),
        ("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS),
    ):
        value = values.get(key)
        value = default if value is None else value
        if type(value) is not int or not 1 <= value <= maximum:
            label = "上下文窗口" if key == "context_window_tokens" else "最大输出"
            raise ValueError(f"{label}须为1至{maximum}的整数 token 数")
        limits[key] = value
    if limits["max_output_tokens"] >= limits["context_window_tokens"]:
        raise ValueError("最大输出 token 数必须小于上下文窗口，需为输入保留空间")
    return limits


def public_context_limits(values=None):
    values = values or {}
    source = values.get("context_limits_source")
    if source not in ("configured", "compatibility_default"):
        source = "configured" if all(values.get(key) is not None for key in ("context_window_tokens", "max_output_tokens")) else "compatibility_default"
    return {**context_limits(values), "context_limits_source": source, "context_limits_verified": False}


def ensure_request_capacity(body, limits):
    from .agent.active_context import ContextCapacityExceeded, estimate_tokens
    limits = context_limits(limits)
    estimated = estimate_tokens(body.get("messages", []), body.get("tools"))
    if estimated + limits["max_output_tokens"] > limits["context_window_tokens"]:
        raise ContextCapacityExceeded("输入与输出预留超过此模型配置的上下文窗口；请减少输入、整理上下文或核对模型窗口后继续")
