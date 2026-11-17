"""Frozen model identity and conservative options for auxiliary review calls."""
import hashlib
import json


MODEL_FIELDS = ("version", "profile_id", "profile_version", "profile_name", "base_url", "model_id",
    "mode", "credential_ref", "reasoning_effort", "capabilities", "capability_observations",
    "context_window_tokens", "max_output_tokens", "context_limits_source", "context_limits_verified")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _binding(llm):
    from ..runtime_context import current_runtime_config
    return getattr(llm, "_runtime_config", None) or current_runtime_config()


def review_chat_options(llm):
    """Default auxiliary reasoning to low only for the official DeepSeek API.

    No key lookup, request, model substitution, output-limit increase, thinking
    disable or change to an explicitly selected effort is performed here.
    """
    from ..llm import APIClient
    if type(llm) is not APIClient:
        return {}
    binding = _binding(llm)
    if binding:
        base, model, effort = (binding.get(key) for key in ("base_url", "model_id", "reasoning_effort"))
        if (binding.get("capabilities") or {}).get("reasoning") == "unsupported":
            return {}
    else:
        from ..config import settings
        base, model, effort = settings.llm_base_url, settings.llm_model, settings.reasoning_effort
    if effort not in (None, ""):
        return {}
    try:
        from ..providers.chat import adapter_for
        official = adapter_for(base, model, "review-classification-placeholder").provider == "deepseek"
    except (ValueError, TypeError):
        return {}
    return {"reasoning_effort": "low"} if official else {}


def model_review_identity(llm):
    """Hash model semantics, excluding mutable run/attempt/budget controls."""
    binding = _binding(llm)
    client = {"class": type(llm).__module__ + "." + type(llm).__qualname__,
        "options_sha256": _digest([getattr(llm, "_provider_options", None),
            getattr(llm, "_context_limits", None), getattr(llm, "_synthetic", None)]),
        "review_chat_options": review_chat_options(llm)}
    if binding:
        return {"binding_sha256": _digest({key: binding[key] for key in MODEL_FIELDS if key in binding}), **client}
    from ..llm import APIClient
    if isinstance(llm, APIClient):
        from ..config import settings
        return {"binding_sha256": _digest([settings.llm_base_url, settings.llm_model,
            settings.llm_api_key, settings.reasoning_effort]), **client}
    return {**client, "mode": getattr(llm, "mode", None)}
