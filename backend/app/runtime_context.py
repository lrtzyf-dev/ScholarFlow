"""Execution-local model/policy identity, never a mutable process-wide override.

The binding contains references and public configuration only, not credentials.
Generators install it only while advancing so interleaved observers cannot leak
one conversation's configuration into another conversation on the same thread.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy


_binding = ContextVar("scholarflow_runtime_binding", default=None)


def current_runtime_config():
    value = _binding.get()
    return deepcopy(value) if value is not None else None


@contextmanager
def bind_runtime_config(value):
    token = _binding.set(deepcopy(value))
    try:
        yield
    finally:
        _binding.reset(token)


def public_runtime_config(value):
    """Keep private credential references out of UI/history payloads too."""
    if not value:
        return None
    fields = ("version", "profile_id", "profile_version", "profile_name", "model_id",
              "reasoning_effort", "approval_mode", "policy_revision", "mode", "resource_budget_id", "execution_environment", "history_revision",
              "context_window_tokens", "max_output_tokens", "context_limits_source", "context_limits_verified",
              "capabilities", "capability_notices", "capability_observations")
    from .model_context_limits import public_context_limits
    return {**{key: deepcopy(value[key]) for key in fields if key in value}, **public_context_limits(value)}
