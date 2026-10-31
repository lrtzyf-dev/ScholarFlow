"""MCP 工具级管控策略单元测试。"""
import app.mcp_policy as policy_mod


def test_default_allows_everything():
    policy_mod.save_policy({"enabled_tools": [], "disabled_tools": [], "tool_rules": {}})
    assert policy_mod.is_allowed("mcp__srv__a") is True
    assert policy_mod.approval_for("mcp__srv__a") is None
    assert policy_mod.timeout_for("mcp__srv__a") is None


def test_blacklist_hides_tool():
    policy_mod.save_policy({
        "enabled_tools": [],
        "disabled_tools": ["mcp__srv__blocked"],
        "tool_rules": {},
    })
    assert policy_mod.is_allowed("mcp__srv__blocked") is False
    assert policy_mod.is_allowed("mcp__srv__other") is True


def test_whitelist_only_listed():
    policy_mod.save_policy({
        "enabled_tools": ["mcp__srv__keep"],
        "disabled_tools": [],
        "tool_rules": {},
    })
    assert policy_mod.is_allowed("mcp__srv__keep") is True
    assert policy_mod.is_allowed("mcp__srv__other") is False


def test_blacklist_overrides_whitelist():
    policy_mod.save_policy({
        "enabled_tools": ["mcp__srv__x"],
        "disabled_tools": ["mcp__srv__x"],
        "tool_rules": {},
    })
    assert policy_mod.is_allowed("mcp__srv__x") is False


def test_tool_rules_approval_and_timeout():
    policy_mod.save_policy({
        "enabled_tools": [],
        "disabled_tools": [],
        "tool_rules": {
            "mcp__srv__a": {"approval": "always", "timeout": 25},
            "mcp__srv__b": {"approval": "never"},
        },
    })
    assert policy_mod.approval_for("mcp__srv__a") == "always"
    assert policy_mod.timeout_for("mcp__srv__a") == 25.0
    assert policy_mod.approval_for("mcp__srv__b") == "never"


def test_save_drops_invalid_values():
    out = policy_mod.save_policy({
        "enabled_tools": [],
        "disabled_tools": [],
        "tool_rules": {
            "mcp__srv__a": {"approval": "bogus", "timeout": 99999},
            "mcp__srv__b": {"approval": "always", "timeout": 10},
        },
    })
    # 非法 approval 与越界 timeout 被剔除，整条 a 因无有效字段而移除
    assert "mcp__srv__a" not in out["tool_rules"]
    assert out["tool_rules"]["mcp__srv__b"] == {"approval": "always", "timeout": 10.0}
