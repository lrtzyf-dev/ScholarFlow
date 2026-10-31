"""
生命周期钩子（Codex 风格 Hooks）。

在 Agent 关键事件点触发用户配置的命令钩子（command hooks），可用于：
- 每次入库/检索后自动记录、打标签、同步
- 工具执行前做自定义校验（阻断）
- 压缩前备份、回答完成后通知 / 归档

钩子配置存于数据目录 hooks.json，结构：
{
  "PreToolUse": [
    {"matcher": ".*", "hooks": [
        {"type": "command", "command": "python hook.py",
         "commandWindows": "python hook_win.py"}
    ]}
  ]
}

约定（与 Codex 一致）：
- 退出码 0：通过；
- 退出码 2：阻断（仅对决策类事件生效，stderr 反馈给模型/用户）；
- 其它非零：非阻断错误，仅记录 stderr；
- 超时：按非阻断处理，记录超时。

任何钩子异常都不会让 Agent 崩溃。
"""
from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
from pathlib import Path

from .config import DATA_DIR

HOOKS_PATH = Path(DATA_DIR) / "hooks.json"

# 支持的全部生命周期事件
HOOK_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
]

# 阻断类事件：这些事件上的钩子退出码 2 会阻止动作
DECISION_EVENTS = {"PreToolUse", "PermissionRequest", "UserPromptSubmit"}

DEFAULT_TIMEOUT = 10.0
BLOCKING_EXIT = 2


def load_hooks() -> dict:
    """读取钩子配置；不存在或损坏时返回空配置。"""
    if not HOOKS_PATH.exists():
        return {}
    try:
        data = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_hooks(data: dict) -> None:
    """持久化钩子配置。"""
    HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _select_command(hook: dict) -> str:
    """按平台选择命令：Windows 优先 commandWindows，否则 command。"""
    if platform.system() == "Windows":
        return hook.get("commandWindows") or hook.get("command_windows") or hook.get("command", "")
    return hook.get("command", "")


def _match(pattern: str, value: str) -> bool:
    """matcher 正则匹配工具名；空/缺省视为匹配全部。"""
    if not pattern:
        return True
    try:
        import re

        return re.search(pattern, value or "") is not None
    except re.error:
        return False


def _run_one(command: str, payload: dict | None, timeout: float) -> dict:
    """执行单条命令钩子，payload 以 JSON 经 stdin 传入。"""
    if not command.strip():
        return {"command": command, "exit_code": None, "stdout": "",
                "stderr": "空命令，已跳过", "timed_out": False, "blocking": False}
    stdin = json.dumps(payload or {}, ensure_ascii=False)
    try:
        if platform.system() == "Windows":
            proc = subprocess.run(
                command, input=stdin, shell=True, capture_output=True,
                text=True, encoding="utf-8", timeout=timeout,
            )
        else:
            proc = subprocess.run(
                shlex.split(command), input=stdin, capture_output=True,
                text=True, timeout=timeout,
            )
        code = proc.returncode
        return {
            "command": command, "exit_code": code,
            "stdout": (proc.stdout or "")[:2000],
            "stderr": (proc.stderr or "")[:2000],
            "timed_out": False, "blocking": code == BLOCKING_EXIT,
        }
    except subprocess.TimeoutExpired:
        return {"command": command, "exit_code": None, "stdout": "",
                "stderr": f"钩子超时（>{timeout}s）", "timed_out": True,
                "blocking": False}
    except Exception as exc:  # noqa: BLE001
        return {"command": command, "exit_code": None, "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}", "timed_out": False,
                "blocking": False}


def run_hooks(event: str, payload: dict | None = None,
              matcher_value: str | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """触发某事件的全部匹配钩子。

    返回 {event, ran, results, blocked, feedback}：
    - blocked：决策类事件上是否有钩子要求阻断；
    - feedback：阻断/错误信息汇总（可反馈给模型或用户）。
    """
    config = load_hooks()
    groups = config.get(event) or []
    results: list[dict] = []
    ran = 0

    for group in groups:
        if not isinstance(group, dict):
            continue
        pattern = group.get("matcher", "")
        if matcher_value is not None and not _match(pattern, matcher_value):
            continue
        for hook in group.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            if hook.get("type", "command") != "command":
                # 仅支持 command 钩子；prompt/agent 钩子解析但跳过
                continue
            command = _select_command(hook)
            res = _run_one(command, payload, float(hook.get("timeout") or timeout))
            res["event"] = event
            results.append(res)
            ran += 1

    blocking_hits = [r for r in results if r["blocking"]]
    is_decision = event in DECISION_EVENTS
    blocked = is_decision and bool(blocking_hits)
    feedback = ""
    if blocked:
        feedback = "；".join(
            (r["stderr"] or r["stdout"] or "钩子阻断").strip()
            for r in blocking_hits
        )
    return {"event": event, "ran": ran, "results": results,
            "blocked": blocked, "feedback": feedback}
