"""MCP discovery cannot block model startup; all connections are synthetic."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.mcp_bridge import MCPBridge, _Conn


@dataclass
class Remote:
    tool_name: str
    release: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    fail: bool = False
    attempts: int = 0
    active: int = 0
    max_active: int = 0
    closed: int = 0
    tool_calls: int = 0
    tool_error: bool = False


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "synthetic discovery did not reach expected state"
        time.sleep(0.005)


@pytest.fixture
def harness(monkeypatch):
    import mcp

    remotes = {}

    class Client:
        def __init__(self, server, **kwargs):
            self.remote = server if isinstance(server, Remote) else remotes[server.command]

        async def __aenter__(self):
            remote = self.remote
            remote.attempts += 1
            remote.active += 1
            remote.max_active = max(remote.max_active, remote.active)
            return self

        async def __aexit__(self, *args):
            self.remote.active -= 1
            self.remote.closed += 1

        async def list_tools(self):
            remote = self.remote
            remote.started.set()
            while not remote.release.is_set():
                await asyncio.sleep(0.005)
            if remote.fail:
                raise OSError("synthetic discovery failure")
            return SimpleNamespace(tools=[SimpleNamespace(
                name=remote.tool_name, description="Synthetic tool", input_schema={"type": "object", "properties": {}},
            )])

        async def call_tool(self, name, arguments):
            self.remote.tool_calls += 1
            return SimpleNamespace(is_error=self.remote.tool_error, content=[SimpleNamespace(text="synthetic result")])

    monkeypatch.setattr(mcp, "Client", Client)
    monkeypatch.setattr(MCPBridge, "_build_builtin", staticmethod(lambda: {}))
    monkeypatch.setattr(MCPBridge, "_load", lambda self: None)
    bridge = MCPBridge()
    monkeypatch.setattr(bridge, "_persist", lambda: None)

    def add(name, *, ready=False, cached=False):
        remote = Remote(name + "_tool")
        if ready:
            remote.release.set()
        remotes[name] = remote
        conn = _Conn(config={"name": name, "command": name, "transport": "stdio", "enabled": True})
        if cached:
            conn.cached_tools = [{"name": f"mcp__{name}__cached", "description": "Known cached tool",
                                  "parameters": {"type": "object", "properties": {}}}]
        bridge._conns[name] = conn
        return remote, conn

    yield bridge, add, remotes

    for name in list(bridge._conns):
        bridge._conns[name].config["enabled"] = False
        bridge._cancel_discovery(name)
    if bridge._loop is not None and bridge._loop.is_running():
        async def cleanup():
            for name in list(bridge._conns):
                await bridge._disconnect(name)
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        bridge._run(cleanup(), timeout=3)
        bridge._loop.call_soon_threadsafe(bridge._loop.stop)
        bridge._thread.join(timeout=3)
        assert not bridge._thread.is_alive()
        bridge._loop.close()


def test_multiple_slow_servers_do_not_block_known_tools_or_duplicate_discovery(harness):
    bridge, add, _ = harness
    first, _ = add("slow_one", cached=True)
    second, _ = add("slow_two", cached=True)
    started = time.monotonic()
    for _ in range(40):
        specs = bridge.get_tool_specs()
        assert {spec["name"] for spec in specs} == {"mcp__slow_one__cached", "mcp__slow_two__cached"}
    assert time.monotonic() - started < 0.5, "discovery waited for external MCP responses"
    assert first.started.wait(1) and second.started.wait(1)
    assert first.attempts == second.attempts == 1
    assert len(bridge._discovery_jobs) == 2
    first.release.set()
    second.release.set()
    wait_for(lambda: all(conn.status == "connected" for conn in bridge._conns.values()))
    assert {spec["name"] for spec in bridge.get_tool_specs()} == {
        "mcp__slow_one__slow_one_tool", "mcp__slow_two__slow_two_tool"}


def test_failed_discovery_backs_off_and_retains_cached_schema(harness):
    bridge, add, _ = harness
    remote, conn = add("broken", ready=True, cached=True)
    remote.fail = True
    assert bridge.get_tool_specs()[0]["name"] == "mcp__broken__cached"
    wait_for(lambda: conn.status == "error" and not bridge._discovery_jobs)
    assert remote.closed == 1 and remote.active == 0
    assert conn.retry_after > time.monotonic()
    for _ in range(20):
        assert bridge.get_tool_specs()[0]["name"] == "mcp__broken__cached"
    assert remote.attempts == 1
    conn.retry_after = time.monotonic() - 1
    bridge.get_tool_specs()
    wait_for(lambda: remote.attempts == 2 and not bridge._discovery_jobs)
    assert conn.discovery_failures == 2 and conn.retry_after > time.monotonic() + 8
    assert "synthetic discovery failure" not in conn.error


def test_disabled_or_removed_server_cannot_reappear_from_pending_discovery(harness):
    bridge, add, _ = harness
    remote, conn = add("disabled", cached=True)
    bridge.get_tool_specs()
    assert remote.started.wait(1)
    bridge.set_enabled("disabled", False)
    assert bridge.get_tool_specs() == []
    assert conn.status != "connected" and remote.active == 0
    result = json.loads(bridge.call_tool("mcp__disabled__cached", {}))
    assert result.get("error") and remote.tool_calls == 0
    bridge.set_enabled("disabled", True)
    assert bridge.get_tool_specs()[0]["name"] == "mcp__disabled__cached"
    wait_for(lambda: remote.attempts == 2)
    bridge.remove_server("disabled")
    remote.release.set()
    assert bridge.get_tool_specs() == [] and "disabled" not in bridge._conns
    assert remote.active == 0


def test_server_configuration_replacement_drops_old_schema_and_pending_job(harness):
    bridge, add, remotes = harness
    old, original = add("replace", cached=True)
    bridge.get_tool_specs()
    assert old.started.wait(1)
    replacement = Remote("replacement_tool")
    replacement.release.set()
    remotes["replacement_command"] = replacement
    result = bridge.upsert_server({"name": "replace", "command": "replacement_command",
                                   "transport": "stdio", "enabled": True})
    assert result["status"] == "connected"
    assert bridge._conns["replace"] is not original
    assert old.active == 0 and old.closed == 1
    specs = bridge.get_tool_specs()
    assert [spec["name"] for spec in specs] == ["mcp__replace__replacement_tool"]
    assert replacement.attempts == 1


def test_explicit_connect_remains_synchronous_and_serializes_pending_discovery(harness):
    bridge, add, _ = harness
    remote, conn = add("explicit")
    assert bridge.get_tool_specs() == []
    assert remote.started.wait(1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.connect, "explicit")
        wait_for(lambda: remote.attempts == 2)
        assert not future.done(), "explicit connect returned before discovering tools"
        remote.release.set()
        result = future.result(timeout=3)
    assert result["status"] == "connected" and result["tool_count"] == 1
    assert remote.max_active == 1 and remote.closed == 1
    assert conn.discovery_failures == 0 and conn.retry_after == 0


def test_stale_discovery_does_not_connect_replacement_configuration(harness):
    bridge, add, _ = harness
    old_remote, old_conn = add("stale", ready=True, cached=True)
    new_remote, new_conn = add("stale", ready=True)
    bridge._run(bridge._discover("stale", old_conn), timeout=3)
    assert old_remote.attempts == new_remote.attempts == 0
    assert new_conn.status == "disconnected" and new_conn.cached_tools == []


def test_cold_external_schema_becomes_visible_after_background_discovery(harness):
    bridge, add, _ = harness
    remote, conn = add("cold")
    assert bridge.get_tool_specs() == []
    assert remote.started.wait(1) and conn.status == "connecting"
    remote.release.set()
    wait_for(lambda: conn.status == "connected")
    first = bridge.get_tool_specs()
    first[0]["parameters"]["properties"]["injected"] = {"type": "string"}
    assert "injected" not in bridge.get_tool_specs()[0]["parameters"]["properties"]
    bridge.disconnect("cold")
    assert conn.cached_tools and conn.status == "disconnected"
    assert bridge.get_tool_specs()[0]["name"] == "mcp__cold__cold_tool"


def test_builtin_registration_still_exposes_tools_before_first_conversation(harness):
    bridge, _, _ = harness
    builtin = Remote("local_tool")
    builtin.release.set()
    bridge._builtin_servers["local"] = builtin
    bridge._register_builtin()
    assert bridge._conns["local"].status == "connected"
    assert bridge.get_tool_specs()[0]["name"] == "mcp__local__local_tool"
    assert builtin.attempts == 1 and not bridge._discovery_jobs


def test_mcp_protocol_error_preserves_content_and_exposes_failure_marker(harness):
    bridge, add, _ = harness
    remote, conn = add("protocol", ready=True)
    remote.tool_error = True
    bridge.connect("protocol")
    result = json.loads(bridge.call_tool("mcp__protocol__protocol_tool", {}))
    assert result["is_error"] is True and result["error"]
    assert result["content"] == "synthetic result"
    assert conn.status == "connected", "a tool-level error must not misreport a broken connection"
