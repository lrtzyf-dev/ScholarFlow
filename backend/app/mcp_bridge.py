"""MCP 桥接层：把 ScholarFlow 升级为 MCP Host。

- 用官方 mcp SDK（async）连接外部 MCP 服务器（stdio / streamable-http）。
- 单独的后台线程跑事件循环，向同步 Agent 暴露同步接口。
- 把 MCP 工具自动发现并转成 OpenAI function 格式，合并进 Agent 工具集。
- 服务器配置持久化到 DATA_DIR/mcp_servers.json。

工具命名约定（避免跨服务器重名）：mcp__<server>__<tool>
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import threading
import time
from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DATA_DIR

MCP_CONFIG_PATH = DATA_DIR / "mcp_servers.json"
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_CREDENTIAL_FIELDS = ("headers", "bearer_token", "oauth", "env")
_REPLAY_SAFE_BUILTINS = {"mcp__builtin__" + name for name in (
    "arxiv_search", "semantic_scholar_search", "crossref_search", "web_fetch")}


def _nonempty(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_nonempty(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_nonempty(v) for v in value)
    return value is not None and value != ""


def _read_credentials(config: dict) -> dict:
    """Decrypt the private auth bundle, retaining read compatibility for old files."""
    protected = config.get("auth_protected")
    if protected:
        from .credential_protection import unprotect

        try:
            value = json.loads(unprotect(protected))
        except (ValueError, json.JSONDecodeError) as exc:
            from .credential_protection import CredentialProtectionError

            if isinstance(exc, CredentialProtectionError):
                raise
            raise CredentialProtectionError("MCP 认证凭证不可用，请重新配置") from exc
        if not isinstance(value, dict):
            from .credential_protection import CredentialProtectionError

            raise CredentialProtectionError("MCP 认证凭证不可用，请重新配置")
        return {key: value[key] for key in _CREDENTIAL_FIELDS if key in value}
    return {key: copy.deepcopy(config[key]) for key in _CREDENTIAL_FIELDS if key in config}


def _store_credentials(config: dict, provided_fields: set[str], previous: dict | None = None) -> dict:
    """Seal MCP credentials before the server configuration reaches disk."""
    result = copy.deepcopy(config)
    previous = previous or {}
    merged = _read_credentials(previous) if previous else {}
    for key in _CREDENTIAL_FIELDS:
        if key in provided_fields:
            value = result.pop(key, None)
            if _nonempty(value):
                merged[key] = value
            else:
                merged.pop(key, None)
        else:
            result.pop(key, None)
    result.pop("auth_protected", None)
    if merged:
        from .credential_protection import protect

        result["auth_protected"] = protect(json.dumps(merged, ensure_ascii=False, separators=(",", ":")))
    return result


def _runtime_config(config: dict) -> dict:
    result = copy.deepcopy(config)
    result.pop("auth_protected", None)
    result.update(_read_credentials(config))
    return result


def _redact_error(exc: Exception, config: dict) -> str:
    """Avoid exposing remote error bodies that may echo configured or runtime secrets."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc), re.IGNORECASE)
        status = match.group(1) if match else None
    detail = f"HTTP {status}" if status else "连接失败，请检查网络、地址和认证配置"
    return f"{type(exc).__name__}: {detail}"


def _safe_name(name: str) -> str:
    return _NAME_RE.sub("_", name.strip())


def _qualified_server_name(name: str) -> str:
    import hashlib
    safe = _safe_name(name)
    return safe if safe == name and '__' not in safe and len(safe) <= 48 else re.sub(r'_+', '-', safe)[:30] + '-' + hashlib.sha256(name.encode()).hexdigest()[:16]


@dataclass
class _Conn:
    config: dict
    client: Any = None
    exit_stack: Any = None
    tools: list[dict] = field(default_factory=list)
    cached_tools: list[dict] = field(default_factory=list)
    connect_lock: Any = None
    retry_after: float = 0.0
    discovery_failures: int = 0
    discovery_paused: bool = False
    status: str = "disconnected"  # disconnected | connecting | connected | error
    error: str = ""


class MCPBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conns: dict[str, _Conn] = {}
        self._disabled_builtin: set[str] = set()
        self._lock = threading.Lock()
        self._discovery_lock = threading.Lock()
        self._discovery_jobs: dict[str, Any] = {}
        self._builtin_servers = self._build_builtin()
        self._load()
        self._register_builtin()

    @staticmethod
    def _build_builtin() -> dict:
        # 延迟导入，避免打包静态分析遗漏
        from .mcp_builtin import build_builtin_servers

        return build_builtin_servers()

    def _register_builtin(self) -> None:
        """把内置服务器注册进连接表（默认启用，随软件本体存在），并自动连接。

        in-process 连接只做工具自省，不发起网络请求，启动开销很小。
        """
        for name in self._builtin_servers:
            cfg = {
                "name": name,
                "transport": "builtin",
                "enabled": name not in self._disabled_builtin,
                "builtin": True,
            }
            if name not in self._conns:
                self._conns[name] = _Conn(config=cfg)
        # 自动连接启用的内置服务器（失败不阻断启动，可在设置页重连）
        for name, conn in self._conns.items():
            if conn.config.get("builtin") and conn.config.get("enabled", True):
                try:
                    self._run(self._connect(name), timeout=30)
                except Exception:
                    pass

    # ---------- 事件循环线程 ----------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if (self._loop is not None and not self._loop.is_closed()
                    and (self._loop.is_running() or self._thread is not None and self._thread.is_alive())):
                return self._loop
            loop = asyncio.new_event_loop()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            self._thread = threading.Thread(target=_run, name="mcp-loop", daemon=True)
            self._thread.start()
            self._loop = loop
            return loop

    def _run(self, coro: Any, timeout: float = 60.0) -> Any:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            fut.cancel()
            raise

    def _cancel_discovery(self, name: str) -> None:
        lock = getattr(self, "_discovery_lock", None)
        if lock is None:
            return
        with lock:
            future = self._discovery_jobs.pop(name, None)
        if future is not None:
            future.cancel()

    def _schedule_discovery(self, name: str, conn: _Conn) -> None:
        """Schedule once without making a conversation wait for remote I/O."""
        if (not conn.config.get("enabled", True) or conn.discovery_paused or conn.status == "connected"
                or time.monotonic() < conn.retry_after):
            return
        from .goal_policy import tool_denial
        if tool_denial("mcp__" + _qualified_server_name(name) + "__discovery"):
            return
        loop = self._ensure_loop()
        with self._discovery_lock:
            previous = self._discovery_jobs.get(name)
            if previous is not None and not previous.done():
                return
            if (self._conns.get(name) is not conn or not conn.config.get("enabled", True)
                    or conn.discovery_paused):
                return
            conn.status = "connecting"
            future = asyncio.run_coroutine_threadsafe(self._discover(name, conn), loop)
            self._discovery_jobs[name] = future

        def completed(done):
            # Callbacks can run immediately, so register outside the lock.
            with self._discovery_lock:
                if self._discovery_jobs.get(name) is done:
                    self._discovery_jobs.pop(name, None)

        future.add_done_callback(completed)

    @staticmethod
    def _connection_failed(conn: _Conn) -> None:
        conn.discovery_failures += 1
        conn.retry_after = time.monotonic() + min(60.0, 5.0 * 2 ** min(conn.discovery_failures - 1, 4))

    async def _discover(self, name: str, conn: _Conn) -> None:
        try:
            await asyncio.wait_for(self._connect(name, expected_conn=conn), timeout=45.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._conns.get(name) is conn and conn.config.get("enabled", True):
                conn.status = "error"
                conn.error = _redact_error(exc, conn.config)
                self._connection_failed(conn)

    # ---------- 配置持久化 ----------
    def _load(self) -> None:
        if not MCP_CONFIG_PATH.exists():
            return
        try:
            data = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._disabled_builtin = set(data.get("disabled_builtin", []) or [])
        migrated = False
        for cfg in data.get("servers", []):
            name = cfg.get("name")
            if name and not cfg.get("builtin"):
                if not cfg.get('server_id'):
                    cfg['server_id'] = uuid4().hex
                    migrated = True
                if any(key in cfg for key in _CREDENTIAL_FIELDS):
                    try:
                        cfg = _store_credentials(cfg, set(cfg) & set(_CREDENTIAL_FIELDS), previous=cfg)
                        migrated = True
                    except Exception:
                        # Keep legacy configurations usable when the platform
                        # cannot protect credentials, but never rewrite them in
                        # plaintext on a later settings change.
                        pass
                self._conns[name] = _Conn(config=cfg)
        if migrated:
            try:
                self._persist()
            except Exception:
                pass

    def _persist(self) -> None:
        MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        user_servers = []
        for conn in self._conns.values():
            if conn.config.get("builtin"):
                continue
            cfg = copy.deepcopy(conn.config)
            if any(key in cfg for key in _CREDENTIAL_FIELDS):
                # A legacy plaintext record must be sealed before any write,
                # including unrelated enable/disable changes.
                cfg = _store_credentials(cfg, set(cfg) & set(_CREDENTIAL_FIELDS), previous=cfg)
                conn.config = cfg
            user_servers.append(cfg)
        disabled = sorted(
            n for n, c in self._conns.items()
            if c.config.get("builtin") and not c.config.get("enabled", True)
        )
        data = {"servers": user_servers, "disabled_builtin": disabled}
        MCP_CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 服务器管理（同步） ----------
    def list_servers(self) -> list[dict]:
        out = []
        # 内置服务器排在前面
        ordered = sorted(
            self._conns.items(),
            key=lambda kv: (not kv[1].config.get("builtin", False), kv[0]),
        )
        for name, conn in ordered:
            out.append({
                "name": name,
                "transport": conn.config.get("transport", "stdio"),
                "enabled": bool(conn.config.get("enabled", True)),
                "builtin": bool(conn.config.get("builtin", False)),
                "status": conn.status,
                "error": conn.error,
                "tool_count": len(conn.tools or conn.cached_tools),
                "url": conn.config.get("url", ""),
                "command": conn.config.get("command", ""),
                **self._oauth_summary(conn.config),
            })
        return out

    def upsert_server(self, cfg: dict, *, provided_fields: set[str] | None = None) -> dict:
        name = (cfg.get("name") or "").strip()
        if not name:
            raise ValueError("服务器名称不能为空")
        cfg["name"] = name
        # 内置服务器：只允许启停，不允许改配置
        if name in self._builtin_servers:
            self.set_enabled(name, bool(cfg.get("enabled", True)))
            if self._conns[name].config.get("enabled", True):
                self._run(self._connect(name), timeout=90)
            return self._summary(name)
        previous = self._conns.get(name)
        old_config = previous.config if previous else None
        provided = set(cfg) if provided_fields is None else set(provided_fields)
        merged = copy.deepcopy(old_config or {})
        merged.update({key: copy.deepcopy(value) for key, value in cfg.items()
                       if key not in _CREDENTIAL_FIELDS and key != "auth_protected"})
        merged.update({key: copy.deepcopy(cfg[key]) for key in provided & set(_CREDENTIAL_FIELDS)
                       if key in cfg})
        merged["name"] = name
        merged['server_id'] = (old_config or {}).get('server_id') or uuid4().hex
        merged.setdefault("enabled", True)
        merged.setdefault("transport", "stdio")
        merged = _store_credentials(merged, provided & set(_CREDENTIAL_FIELDS), previous=old_config)
        from . import mcp_user_oauth
        if previous and mcp_user_oauth.fingerprint(_runtime_config(old_config)) != mcp_user_oauth.fingerprint(_runtime_config(merged)):
            mcp_user_oauth.invalidate(_runtime_config(old_config), '连接配置已更改，请重新授权')
            self._run(mcp_user_oauth.cancel(_runtime_config(old_config), invalidate_grant=False), timeout=10)
        if provided & {"bearer_token", "oauth", "headers"}:
            from .mcp_oauth import manager

            manager.clear()
        if previous:
            # 若已连接，先断开再用新配置
            previous.discovery_paused = True
            self._cancel_discovery(name)
            self._run(self._disconnect(name), timeout=30)
        self._conns[name] = _Conn(config=merged)
        self._persist()
        if merged.get("enabled", True) and not mcp_user_oauth.user_flow(_runtime_config(merged)):
            self._run(self._connect(name), timeout=90)
        return self._summary(name)

    def remove_server(self, name: str) -> None:
        if name in self._builtin_servers:
            raise ValueError("内置服务器不可删除，只能停用")
        if name in self._conns:
            from . import mcp_user_oauth
            cfg = _runtime_config(self._conns[name].config)
            mcp_user_oauth.invalidate(cfg, '连接已删除')
            self._run(mcp_user_oauth.cancel(cfg, invalidate_grant=False), timeout=10)
            self._conns[name].discovery_paused = True
            self._cancel_discovery(name)
            try:
                self._run(self._disconnect(name), timeout=30)
            except Exception:
                pass
            del self._conns[name]
            self._persist()

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name in self._conns:
            self._conns[name].config["enabled"] = bool(enabled)
            if enabled:
                self._conns[name].retry_after = 0.0
            self._persist()
            if not enabled:
                self._cancel_discovery(name)
                try:
                    self._run(self._disconnect(name), timeout=30)
                except Exception:
                    pass

    def connect(self, name: str) -> dict:
        if name not in self._conns:
            raise KeyError(name)
        conn = self._conns[name]
        conn.discovery_paused = True
        self._cancel_discovery(name)
        try:
            self._run(self._connect(name, force=True), timeout=90)
        finally:
            conn.discovery_paused = False
        return self._summary(name)

    def disconnect(self, name: str) -> dict:
        conn = self._conns.get(name)
        if conn is None:
            raise KeyError(name)
        conn.discovery_paused = True
        self._cancel_discovery(name)
        try:
            self._run(self._disconnect(name), timeout=30)
        finally:
            conn.discovery_paused = False
        return self._summary(name)

    def _summary(self, name: str) -> dict:
        conn = self._conns[name]
        return {
            "name": name,
            "transport": conn.config.get("transport", "stdio"),
            "enabled": bool(conn.config.get("enabled", True)),
            "builtin": bool(conn.config.get("builtin", False)),
            "status": conn.status,
            "error": conn.error,
            "tool_count": len(conn.tools or conn.cached_tools),
            "tools": copy.deepcopy(conn.tools or conn.cached_tools),
            **self._oauth_summary(conn.config),
        }

    @staticmethod
    def _oauth_summary(config):
        from . import mcp_user_oauth
        try:
            runtime = _runtime_config(config)
        except Exception:
            return {'auth_mode': 'unavailable', 'user_oauth': {'state': 'reauthorize', 'message': '凭证不可读取，请重新配置'}}
        if mcp_user_oauth.user_flow(runtime):
            return {'auth_mode': 'authorization_code', 'user_oauth': mcp_user_oauth.status(runtime)}
        return {'auth_mode': 'client_credentials' if runtime.get('oauth') else 'other'}

    def _authorization_available(self, conn):
        info = self._oauth_summary(conn.config)
        return info['auth_mode'] != 'unavailable' and (info['auth_mode'] != 'authorization_code'
            or info['user_oauth']['state'] == 'authorized')

    def user_authorization(self, name, action='status'):
        from . import mcp_user_oauth
        if name not in self._conns:
            raise KeyError(name)
        conn = self._conns[name]
        cfg = _runtime_config(conn.config)
        if not mcp_user_oauth.user_flow(cfg):
            raise ValueError('此连接没有配置用户 OAuth')
        if action == 'status':
            return mcp_user_oauth.status(cfg)
        if action == 'login':
            result = self._run(mcp_user_oauth.start(cfg), timeout=10)
        elif action == 'cancel':
            self._run(mcp_user_oauth.cancel(cfg), timeout=10)
            result = mcp_user_oauth.status(cfg)
        elif action == 'revoke':
            result = self._run(mcp_user_oauth.revoke(cfg), timeout=20)
        else:
            raise ValueError('不支持的授权操作')
        if action in ('cancel', 'revoke'):
            self._cancel_discovery(name)
            conn.tools.clear()
            conn.cached_tools.clear()
            self._run(self._disconnect(name), timeout=30)
            conn.status, conn.error = 'disconnected', ''
        return result

    @contextlib.asynccontextmanager
    async def _http_transport(self, cfg: dict):
        """带认证头（静态头/bearer/OAuth）的 streamable-http transport。

        自带 httpx client 时 SDK 不负责关闭，这里统一在退出时关闭，避免泄漏。
        """
        from mcp.client.streamable_http import (
            create_mcp_http_client,
            streamable_http_client,
        )

        from .mcp_oauth import build_http_headers
        from .mcp_user_oauth import user_flow, LiveUserAuth
        if user_flow(cfg):
            # Caller-provided Authorization must never override a live user grant.
            headers = {k: v for k, v in (cfg.get('headers') or {}).items() if k.lower() != 'authorization'}
            client = create_mcp_http_client(headers=headers or None, auth=LiveUserAuth(cfg))
        else:
            headers = await build_http_headers(cfg)
            client = create_mcp_http_client(headers=headers or None)
        async with client:
            async with streamable_http_client(cfg["url"], http_client=client) as streams:
                yield streams

    # ---------- 异步：连接 / 断开 ----------
    async def _connect(self, name: str, *, force: bool = False, expected_conn: _Conn | None = None) -> None:
        conn = self._conns.get(name)
        if conn is None or expected_conn is not None and conn is not expected_conn:
            return
        if conn.connect_lock is None:
            conn.connect_lock = asyncio.Lock()
        async with conn.connect_lock:
            if self._conns.get(name) is not conn or not conn.config.get("enabled", True):
                return
            if conn.status == "connected" and conn.client is not None and not force:
                return
            from .goal_policy import tool_denial
            if tool_denial("mcp__" + _qualified_server_name(name) + "__connect"):
                if conn.client is None:
                    conn.status = "disconnected"
                return
            await self._open_connection(name, conn)

    async def _open_connection(self, name: str, conn: _Conn) -> None:
        from contextlib import AsyncExitStack

        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        await self._close_connection(conn)
        cfg = conn.config
        conn.status = "connecting"
        stack = AsyncExitStack()
        try:
            runtime_cfg = _runtime_config(cfg)
            if cfg.get("transport") == "builtin":
                # 内置服务器：in-process 连接，无子进程
                server: Any = self._builtin_servers[name]
            elif cfg.get("transport") == "http":
                server = self._http_transport(runtime_cfg)
            else:
                server = StdioServerParameters(
                    command=runtime_cfg.get("command", ""),
                    args=list(runtime_cfg.get("args", []) or []),
                    env=dict(runtime_cfg.get("env", {}) or {}),
                    cwd=runtime_cfg.get("cwd") or None,
                )
            client = Client(server, raise_exceptions=True)
            from .goal_policy import tool_denial
            if tool_denial("mcp__" + _qualified_server_name(name) + "__connect"):
                conn.status = "disconnected"
                return
            await stack.enter_async_context(client)
            if tool_denial("mcp__" + _qualified_server_name(name) + "__discovery"):
                await stack.aclose()
                conn.status = "disconnected"
                return
            result = await client.list_tools()
            if self._conns.get(name) is not conn or not cfg.get("enabled", True):
                await stack.aclose()
                conn.status = "disconnected"
                return
            conn.client = client
            conn.exit_stack = stack
            conn.tools = [self._tool_to_openai(name, t) for t in result.tools]
            conn.cached_tools = copy.deepcopy(conn.tools)
            conn.status = "connected"
            conn.error = ""
            conn.discovery_failures = 0
            conn.retry_after = 0.0
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await stack.aclose()
            conn.client = None
            conn.exit_stack = None
            conn.tools = []
            if isinstance(exc, asyncio.CancelledError):
                conn.status = "disconnected"
                raise
            if not isinstance(exc, Exception):
                raise
            conn.status = "error"
            conn.error = _redact_error(exc, cfg)
            self._connection_failed(conn)

    async def _disconnect(self, name: str) -> None:
        conn = self._conns.get(name)
        if conn is None:
            return
        if conn.connect_lock is None:
            conn.connect_lock = asyncio.Lock()
        async with conn.connect_lock:
            await self._close_connection(conn)

    @staticmethod
    async def _close_connection(conn: _Conn) -> None:
        if conn.exit_stack is not None:
            try:
                await conn.exit_stack.aclose()
            except Exception:
                pass
        conn.client = None
        conn.exit_stack = None
        conn.tools = []
        if conn.status in ("connected", "connecting"):
            conn.status = "disconnected"

    # ---------- 工具转换 ----------
    @staticmethod
    def _tool_to_openai(server: str, tool: Any) -> dict:
        schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
        return {
            "name": f"mcp__{_qualified_server_name(server)}__{tool.name}",
            "description": getattr(tool, "description", None) or f"MCP tool {tool.name} from {server}",
            "parameters": schema,
            "_mcp_server": server,
            "_mcp_tool": tool.name,
        }

    # ---------- Agent 接口 ----------
    def get_tool_specs(self) -> list[dict]:
        """Return known schemas immediately; discover disconnected servers in background."""
        specs: list[dict] = []
        for name, conn in list(self._conns.items()):
            if not conn.config.get("enabled", True) or not self._authorization_available(conn):
                if (self._oauth_summary(conn.config)).get('auth_mode') == 'authorization_code':
                    conn.tools.clear()
                    conn.cached_tools.clear()
                continue
            if conn.status != "connected":
                try:
                    self._schedule_discovery(name, conn)
                except Exception:
                    pass
            if self._conns.get(name) is conn and conn.config.get("enabled", True):
                specs.extend(copy.deepcopy(conn.tools or conn.cached_tools))
        return specs

    def is_mcp_tool(self, name: str) -> bool:
        return name.startswith("mcp__")

    def call_tool(self, qualified: str, arguments: dict) -> str:
        parts = qualified.split("__")
        if len(parts) < 3:
            return json.dumps({"error": f"bad mcp tool name: {qualified}"}, ensure_ascii=False)
        server = parts[1]
        tool = "__".join(parts[2:])
        matches = [name for name in self._conns if _qualified_server_name(name) == server]
        if len(matches) != 1:
            return json.dumps({'error': 'MCP 工具对应的连接不存在或名称不唯一，请重新发现工具'}, ensure_ascii=False)
        server = matches[0]
        # 单工具超时覆盖，缺省 120s
        from .mcp_policy import timeout_for

        timeout = timeout_for(qualified) or 120
        # The synchronous timeout can race the loop thread before dispatch.
        # Closing this receipt under the same lock prevents a late coroutine
        # from sending a write after we have reported "not executed".
        dispatch = {"lock": threading.Lock(), "started": False, "closed": False}
        try:
            return self._run(self._call_tool(server, tool, arguments or {}, dispatch=dispatch), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            with dispatch["lock"]:
                dispatch["closed"] = True
                started = dispatch["started"]
            config = self._conns.get(server).config if self._conns.get(server) else {}
            # These built-in adapters are implemented as read-only GETs. A
            # third-party MCP annotation/approval override is not proof that
            # an arbitrary tool can safely be repeated after a lost reply.
            read_only = qualified in _REPLAY_SAFE_BUILTINS
            unknown = bool(started and not read_only)
            return json.dumps({"error": _redact_error(exc, config),
                "not_executed": not started, "execution_unknown": unknown,
                "automatic_retry": False if unknown else True,
                "hint": "工具已派发但结果未确认；请核对外部系统，不能自动重发或视为成功。" if unknown else
                    "只读请求失败，可以调整后重试。" if started else "工具尚未派发，可以调整配置后重试。"}, ensure_ascii=False)

    async def _call_tool(self, server: str, tool: str, arguments: dict, *, dispatch=None) -> str:
        conn = self._conns.get(server)
        if conn is None or not conn.config.get("enabled", True):
            return json.dumps({"error": f"MCP server '{server}' 不可用或已禁用"}, ensure_ascii=False)
        selected_conn = conn
        from .goal_policy import tool_denial
        qualified = "mcp__" + _qualified_server_name(server) + "__" + tool
        denial = tool_denial(qualified, arguments)
        if denial:
            return json.dumps(denial, ensure_ascii=False)
        if not self._authorization_available(conn):
            return json.dumps({'error': '用户授权已失效，请重新登录；工具未执行'}, ensure_ascii=False)
        if conn is None or conn.client is None:
            # 尝试重连一次
            await self._connect(server, expected_conn=selected_conn)
            conn = self._conns.get(server)
        if conn is None or conn.client is None:
            return json.dumps({"error": f"MCP server '{server}' 未连接"}, ensure_ascii=False)
        if conn is not selected_conn or not conn.config.get("enabled", True) or not self._authorization_available(conn):
            return json.dumps({"error": "MCP 连接配置已变更或禁用，请重新发现工具后再试"}, ensure_ascii=False)
        denial = tool_denial(qualified, arguments)
        if denial:
            return json.dumps(denial, ensure_ascii=False)
        if dispatch is not None:
            with dispatch["lock"]:
                if dispatch["closed"]:
                    return json.dumps({"error": "本次调用已在派发前结束，工具未执行。", "not_executed": True}, ensure_ascii=False)
                dispatch["started"] = True
        result = await conn.client.call_tool(tool, arguments)
        texts = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
        payload = {"server": server, "tool": tool, "is_error": bool(result.is_error),
                   "content": "\n".join(texts) if texts else "(无文本内容)"}
        if payload["is_error"]:
            # MCP protocol errors are results, not raised transport exceptions.
            # Keep the diagnostic content, but expose a stable failure marker
            # so the executor never accepts this output as completion proof.
            payload["error"] = "MCP 工具返回错误，本次操作未成功；请检查工具输出并调整请求。"
            if qualified not in _REPLAY_SAFE_BUILTINS:
                # A tool's error result says nothing about rollback of writes
                # it may already have made. Do not authorize the same write
                # again merely because this protocol response arrived intact.
                payload.update(execution_unknown=True, automatic_retry=False,
                    hint="工具返回错误，但不能确认它没有产生副作用；请先核对远端实际结果。")
        return json.dumps(payload, ensure_ascii=False)


bridge = MCPBridge()
