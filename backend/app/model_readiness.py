"""Configuration readiness without connecting to a provider or resolving DNS."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


MISSING_CLOUD_KEY = "模型尚未配置密钥，请添加该模型的 API Key，或明确选择离线演示"


def allows_keyless_attempt(base_url: str) -> bool:
    """A configured local service may be tried without a key, not assumed public.

    This is only a configuration hint. Local services can still require auth;
    the real response determines whether the configured request is accepted.
    No DNS lookup or network probe is performed by opening a settings page.
    """
    try:
        parts = urlsplit(base_url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if parts.scheme not in ("http", "https") or not host:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return True
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private
    except ValueError:
        return False


def configured_api_mode(base_url: str, has_key: bool) -> str:
    return "api" if has_key or allows_keyless_attempt(base_url) else "unconfigured"
