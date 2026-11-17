"""SSRF 防护：校验目标 URL，阻断回环 / 私有 / 链路本地 / 保留地址。

- assert_public_url：在发起请求前校验（含 DNS 解析后的真实 IP，防字面主机名绕过）。
- request_guard_hook：httpx event hook，对每次实际发出的请求（含重定向逐跳）都校验，
  避免"初始 URL 公网、302 跳转到内网"的绕过。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx


class SSRFError(ValueError):
    """请求目标指向内网 / 回环 / 保留地址，已被阻断。"""


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_ips(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFError(f"无法解析主机名：{host}") from exc
    return [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]


def assert_public_url(url: str) -> None:
    """校验一个 URL 必须指向公网 http/https 目标，否则抛 SSRFError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError("仅允许 http/https 协议")
    host = (parsed.hostname or "").lower()
    if not host:
        raise SSRFError("URL 缺少主机名")
    # 主机名层面的本地标识（localhost / *.local / *.localhost）
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise SSRFError(f"禁止访问本地/内网主机：{host}")
    # 字面 IP 直接判定；否则解析 DNS 后逐个判定
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        ips = _resolve_ips(host)
    for ip in ips:
        if _is_blocked_ip(ip):
            raise SSRFError(f"禁止访问内网/保留地址：{ip}")


def request_guard_hook(request: httpx.Request) -> None:
    """httpx event hook：每次发送请求（含重定向）前校验目标地址。"""
    assert_public_url(str(request.url))
