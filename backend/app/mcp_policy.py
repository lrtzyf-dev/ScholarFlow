"""MCP 工具级管控策略（对标 Codex tools.enabled_tools / disabled_tools 与 per-tool 配置）。

策略持久化在 DATA_DIR/mcp_tool_policy.json：
{
  "enabled_tools": ["mcp__server__tool"],   // 白名单：非空时仅这些 MCP 工具可用；空=不启用
  "disabled_tools": ["mcp__server__tool2"], // 黑名单：这些工具对模型隐藏
  "tool_rules": {
    "mcp__server__tool": {
      "approval": "always",   // always=每次强制审批；never=自动放行；缺省=按风险默认
      "timeout": 30           // 单次调用超时秒数
    }
  }
}

判定顺序：先黑名单，再白名单。内置（非 MCP）工具不受此策略限制。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import DATA_DIR

POLICY_PATH = Path(DATA_DIR) / "mcp_tool_policy.json"

VALID_APPROVAL = {"always", "never"}


def load_policy() -> dict:
    """读取策略；缺失或损坏时返回空策略。"""
    if not POLICY_PATH.exists():
        return {"enabled_tools": [], "disabled_tools": [], "tool_rules": {}}
    try:
        data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled_tools": [], "disabled_tools": [], "tool_rules": {}}
    if not isinstance(data, dict):
        return {"enabled_tools": [], "disabled_tools": [], "tool_rules": {}}
    # 补齐字段，避免下游 KeyError
    data.setdefault("enabled_tools", [])
    data.setdefault("disabled_tools", [])
    data.setdefault("tool_rules", {})
    return data


def save_policy(policy: dict) -> dict:
    """校验并持久化策略，返回规范化后的策略。"""
    enabled = list(policy.get("enabled_tools") or [])
    disabled = list(policy.get("disabled_tools") or [])
    rules = policy.get("tool_rules") or {}

    clean_rules: dict[str, dict] = {}
    for tool, rule in rules.items():
        if not isinstance(rule, dict):
            continue
        entry: dict = {}
        approval = rule.get("approval")
        if approval in VALID_APPROVAL:
            entry["approval"] = approval
        timeout = rule.get("timeout")
        if isinstance(timeout, (int, float)) and 0 < timeout <= 600:
            entry["timeout"] = float(timeout)
        if entry:
            clean_rules[tool] = entry

    normalized = {
        "enabled_tools": [str(x) for x in enabled],
        "disabled_tools": [str(x) for x in disabled],
        "tool_rules": clean_rules,
    }
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return normalized


def is_allowed(qualified_name: str) -> bool:
    """某 MCP 工具是否对模型可见（先黑名单，再白名单）。"""
    policy = load_policy()
    if qualified_name in policy["disabled_tools"]:
        return False
    enabled = policy["enabled_tools"]
    if enabled and qualified_name not in enabled:
        return False
    return True


def approval_for(qualified_name: str) -> str | None:
    """单工具审批策略覆盖：'always' / 'never' / None（走默认风险判定）。"""
    rule = load_policy()["tool_rules"].get(qualified_name) or {}
    return rule.get("approval")


def timeout_for(qualified_name: str) -> float | None:
    """单工具超时覆盖（秒）。"""
    rule = load_policy()["tool_rules"].get(qualified_name) or {}
    value = rule.get("timeout")
    return float(value) if value is not None else None
