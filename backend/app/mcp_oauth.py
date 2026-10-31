"""MCP OAuth（客户端凭证流）与 HTTP 认证头构建。

支持三种 HTTP 认证方式（可叠加静态头）：
1. 静态自定义头：cfg["headers"]；
2. 静态 bearer：cfg["bearer_token"] → Authorization: Bearer <token>；
3. OAuth 客户端凭证：cfg["oauth"] = {
     "client_id", "client_secret", "token_url", "scope"(可选)
   } 调用前自动获取 access_token，缓存并在过期前刷新。

token 仅在内存缓存（不落盘），进程重启后重新获取，避免泄露长期凭证。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

# 过期前提前刷新的安全余量（秒）
EXPIRY_MARGIN = 60
TOKEN_TIMEOUT = 10.0


class OAuthError(RuntimeError):
    """OAuth 获取 token 失败。"""


@dataclass
class _Cached:
    access_token: str
    expires_at: float
    token_type: str


class OAuthTokenManager:
    """按 token_url+client_id+scope 缓存 access_token。"""

    def __init__(self) -> None:
        self._cache: dict[str, _Cached] = {}

    @staticmethod
    def _key(token_url: str, client_id: str, scope: str) -> str:
        return "|".join([token_url, client_id, scope])

    async def get_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_url: str,
        scope: str = "",
        force: bool = False,
    ) -> str:
        if not token_url:
            raise OAuthError("缺少 token_url")
        key = self._key(token_url, client_id, scope)
        now = time.time()
        cached = self._cache.get(key)
        if not force and cached and cached.expires_at - EXPIRY_MARGIN > now:
            return cached.access_token

        data = {"grant_type": "client_credentials"}
        if client_id:
            data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret
        if scope:
            data["scope"] = scope
        try:
            async with httpx.AsyncClient(timeout=TOKEN_TIMEOUT) as http:
                resp = await http.post(token_url, data=data)
        except httpx.HTTPError as exc:
            raise OAuthError(f"无法连接 token 端点：{type(exc).__name__}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise OAuthError(f"认证失败（HTTP {resp.status_code}）：client_id/secret 无效")
        if resp.status_code >= 400:
            raise OAuthError(f"token 请求被拒（HTTP {resp.status_code}）：{resp.text[:160]}")
        try:
            payload = resp.json()
            token = payload["access_token"]
        except (ValueError, KeyError) as exc:
            raise OAuthError("token 响应缺少 access_token 或不是合法 JSON") from exc
        expires_in = int(payload.get("expires_in", 3600))
        self._cache[key] = _Cached(
            access_token=token,
            expires_at=now + max(expires_in, 60),
            token_type=payload.get("token_type", "Bearer"),
        )
        return token

    def clear(self) -> None:
        self._cache.clear()


# 模块级单例
manager = OAuthTokenManager()


async def build_http_headers(cfg: dict) -> dict[str, str]:
    """根据服务器配置构建 HTTP 头；OAuth 优先于静态 bearer。"""
    headers: dict[str, str] = {}
    static = cfg.get("headers")
    if isinstance(static, dict):
        for k, v in static.items():
            headers[str(k)] = str(v)
    bearer = cfg.get("bearer_token")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    oauth = cfg.get("oauth")
    if isinstance(oauth, dict) and oauth.get("token_url"):
        token = await manager.get_token(
            client_id=oauth.get("client_id", ""),
            client_secret=oauth.get("client_secret", ""),
            token_url=oauth["token_url"],
            scope=oauth.get("scope", ""),
        )
        headers["Authorization"] = f"Bearer {token}"
    return headers
