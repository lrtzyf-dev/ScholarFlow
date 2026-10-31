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
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DATA_DIR

MCP_CONFIG_PATH = DATA_DIR / "mcp_servers.json"
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_name(name: str) -> str:
    return _NAME_RE.sub("_", name.strip())


@dataclass
class _Conn:
    config: dict
    client: Any = None
    exit_stack: Any = None
    tools: list[dict] = field(default_factory=list)
    status: str = "disconnected"  # disconnected | connected | error
    error: str = ""


class MCPBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conns: dict[str, _Conn] = {}
        self._disabled_builtin: set[str] = set()
        self._lock = threading.Lock()
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
            if self._loop is not None and self._loop.is_running():
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
        return fut.result(timeout=timeout)

    # ---------- 配置持久化 ----------
    def _load(self) -> None:
        if not MCP_CONFIG_PATH.exists():
            return
        try:
            data = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._disabled_builtin = set(data.get("disabled_builtin", []) or [])
        for cfg in data.get("servers", []):
            name = cfg.get("name")
            if name and not cfg.get("builtin"):
                self._conns[name] = _Conn(config=cfg)

    def _persist(self) -> None:
        MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        user_servers = [
            c.config for c in self._conns.values() if not c.config.get("builtin")
        ]
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
                "tool_count": len(conn.tools),
                "url": conn.config.get("url", ""),
                "command": conn.config.get("command", ""),
            })
        return out

    def upsert_server(self, cfg: dict) -> dict:
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
        cfg.setdefault("enabled", True)
        cfg.setdefault("transport", "stdio")
        if name in self._conns:
            # 若已连接，先断开再用新配置
            self._run(self._disconnect(name), timeout=30)
        self._conns[name] = _Conn(config=cfg)
        self._persist()
        if cfg.get("enabled", True):
            self._run(self._connect(name), timeout=90)
        return self._summary(name)

    def remove_server(self, name: str) -> None:
        if name in self._builtin_servers:
            raise ValueError("内置服务器不可删除，只能停用")
        if name in self._conns:
            try:
                self._run(self._disconnect(name), timeout=30)
            except Exception:
                pass
            del self._conns[name]
            self._persist()

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name in self._conns:
            self._conns[name].config["enabled"] = bool(enabled)
            self._persist()
            if not enabled:
                try:
                    self._run(self._disconnect(name), timeout=30)
                except Exception:
                    pass

    def connect(self, name: str) -> dict:
        if name not in self._conns:
            raise KeyError(name)
        self._run(self._connect(name), timeout=90)
        return self._summary(name)

    def disconnect(self, name: str) -> dict:
        self._run(self._disconnect(name), timeout=30)
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
            "tool_count": len(conn.tools),
            "tools": conn.tools,
        }

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

        headers = await build_http_headers(cfg)
        client = create_mcp_http_client(headers=headers or None)
        async with client:
            async with streamable_http_client(cfg["url"], http_client=client) as streams:
                yield streams

    # ---------- 异步：连接 / 断开 ----------
    async def _connect(self, name: str) -> None:
        from contextlib import AsyncExitStack

        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        conn = self._conns[name]
        await self._disconnect(name)
        cfg = conn.config
        try:
            if cfg.get("transport") == "builtin":
                # 内置服务器：in-process 连接，无子进程
                server: Any = self._builtin_servers[name]
            elif cfg.get("transport") == "http":
                server = self._http_transport(cfg)
            else:
                server = StdioServerParameters(
                    command=cfg.get("command", ""),
                    args=list(cfg.get("args", []) or []),
                    env=dict(cfg.get("env", {}) or {}),
                    cwd=cfg.get("cwd") or None,
                )
            client = Client(server, raise_exceptions=True)
            stack = AsyncExitStack()
            await stack.enter_async_context(client)
            result = await client.list_tools()
            conn.client = client
            conn.exit_stack = stack
            conn.tools = [self._tool_to_openai(name, t) for t in result.tools]
            conn.status = "connected"
            conn.error = ""
        except Exception as exc:  # noqa: BLE001
            conn.status = "error"
            conn.error = f"{type(exc).__name__}: {exc}"
            if conn.exit_stack is not None:
                try:
                    await conn.exit_stack.aclose()
                except Exception:
                    pass
            conn.client = None
            conn.exit_stack = None

    async def _disconnect(self, name: str) -> None:
        conn = self._conns.get(name)
        if conn is None:
            return
        if conn.exit_stack is not None:
            try:
                await conn.exit_stack.aclose()
            except Exception:
                pass
        conn.client = None
        conn.exit_stack = None
        conn.tools = []
        if conn.status == "connected":
            conn.status = "disconnected"

    # ---------- 工具转换 ----------
    @staticmethod
    def _tool_to_openai(server: str, tool: Any) -> dict:
        schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
        return {
            "name": f"mcp__{_safe_name(server)}__{tool.name}",
            "description": getattr(tool, "description", None) or f"MCP tool {tool.name} from {server}",
            "parameters": schema,
            "_mcp_server": server,
            "_mcp_tool": tool.name,
        }

    # ---------- Agent 接口 ----------
    def get_tool_specs(self) -> list[dict]:
        """连接所有 enabled 但未连接的服务器（尽力），返回全部 MCP 工具的 OpenAI 规格。"""
        specs: list[dict] = []
        for name, conn in list(self._conns.items()):
            if not conn.config.get("enabled", True):
                continue
            if conn.status != "connected":
                try:
                    self._run(self._connect(name), timeout=45)
                except Exception:
                    continue
            if conn.status == "connected":
                specs.extend(conn.tools)
        return specs

    def is_mcp_tool(self, name: str) -> bool:
        return name.startswith("mcp__")

    def call_tool(self, qualified: str, arguments: dict) -> str:
        parts = qualified.split("__")
        if len(parts) < 3:
            return json.dumps({"error": f"bad mcp tool name: {qualified}"}, ensure_ascii=False)
        server = parts[1]
        tool = "__".join(parts[2:])
        # 单工具超时覆盖，缺省 120s
        from .mcp_policy import timeout_for

        timeout = timeout_for(qualified) or 120
        try:
            return self._run(self._call_tool(server, tool, arguments or {}), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    async def _call_tool(self, server: str, tool: str, arguments: dict) -> str:
        conn = self._conns.get(server)
        if conn is None or conn.client is None:
            # 尝试重连一次
            await self._connect(server)
            conn = self._conns.get(server)
        if conn is None or conn.client is None:
            return json.dumps({"error": f"MCP server '{server}' 未连接"}, ensure_ascii=False)
        result = await conn.client.call_tool(tool, arguments)
        texts = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
        payload = {"server": server, "tool": tool, "is_error": bool(result.is_error),
                   "content": "\n".join(texts) if texts else "(无文本内容)"}
        return json.dumps(payload, ensure_ascii=False)


bridge = MCPBridge()
