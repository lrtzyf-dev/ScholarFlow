"""持久目标（对标 Codex /goal）。

跨轮生效的单一目标：设置后注入每一轮的系统提示，Agent 持续推进直至完成；
支持暂停（暂停期间不注入、不强制推进）、恢复、完成与取消。

配置存于数据目录 goals.json，保存全部历史目标，结构为列表，每条：
{
  "id": "g_...",
  "description": "目标描述",
  "status": "active" | "paused" | "done" | "cancelled",
  "note": "可选备注（如被新目标取代）",
  "created_at": ISO8601,
  "updated_at": ISO8601,
  "completed_at": ISO8601 | None
}

任意时刻最多只有一个「当前目标」（最新一条 active/paused）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR

GOAL_PATH = Path(DATA_DIR) / "goals.json"

# 目标状态
ACTIVE = "active"
PAUSED = "paused"
DONE = "done"
CANCELLED = "cancelled"
# 仍视为「当前目标」（未终结）的状态
OPEN_STATUSES = (ACTIVE, PAUSED)
TERMINAL_STATUSES = (DONE, CANCELLED)


class GoalError(ValueError):
    """目标操作非法（如当前没有目标却要暂停）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_goals() -> list[dict]:
    """读取全部目标；不存在或损坏时返回空列表（按时间正序）。"""
    if not GOAL_PATH.exists():
        return []
    try:
        data = json.loads(GOAL_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [g for g in data if isinstance(g, dict) and g.get("description")]


def _save(goals: list[dict]) -> None:
    GOAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOAL_PATH.write_text(
        json.dumps(goals, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_current_goal() -> dict | None:
    """返回当前未终结目标（最新一条 active/paused）；没有则 None。"""
    open_goals = [g for g in load_goals() if g.get("status") in OPEN_STATUSES]
    return open_goals[-1] if open_goals else None


def set_goal(description: str) -> dict:
    """设置新的当前目标。

    若已有未终结目标，先将其标记为 cancelled（被新目标取代），再创建新目标。
    返回新建的目标。
    """
    description = (description or "").strip()
    if not description:
        raise GoalError("目标描述不能为空")
    goals = load_goals()
    now = _now()
    for g in reversed(goals):
        if g.get("status") in OPEN_STATUSES:
            g["status"] = CANCELLED
            g["note"] = "被新设置的目标取代"
            g["updated_at"] = now
            g["completed_at"] = now
        else:
            break
    goal = {
        "id": "g_" + uuid.uuid4().hex[:12],
        "description": description[:1000],
        "status": ACTIVE,
        "note": "",
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    goals.append(goal)
    _save(goals)
    return goal


def _transition(new_status: str, action: str) -> dict:
    """当前目标状态机通用切换。"""
    goal = get_current_goal()
    if goal is None:
        raise GoalError(f"当前没有可{action}的目标")
    now = _now()
    goal["status"] = new_status
    goal["updated_at"] = now
    if new_status in TERMINAL_STATUSES:
        goal["completed_at"] = now
    goals = load_goals()
    for g in goals:
        if g.get("id") == goal["id"]:
            g.update(goal)
            break
    _save(goals)
    return goal


def pause_goal() -> dict:
    """暂停当前目标（仅 active 可暂停）。"""
    goal = get_current_goal()
    if goal is None:
        raise GoalError("当前没有可暂停的目标")
    if goal.get("status") != ACTIVE:
        raise GoalError("仅进行中的目标可以暂停")
    return _transition(PAUSED, "暂停")


def resume_goal() -> dict:
    """恢复当前目标（仅 paused 可恢复）。"""
    goal = get_current_goal()
    if goal is None:
        raise GoalError("当前没有可恢复的目标")
    if goal.get("status") != PAUSED:
        raise GoalError("仅已暂停的目标可以恢复")
    return _transition(ACTIVE, "恢复")


def complete_goal() -> dict:
    """将当前目标标记为完成。"""
    if get_current_goal() is None:
        raise GoalError("当前没有可完成的目标")
    return _transition(DONE, "完成")


def cancel_goal() -> dict:
    """取消当前目标。"""
    if get_current_goal() is None:
        raise GoalError("当前没有可取消的目标")
    return _transition(CANCELLED, "取消")


def active_goal_block() -> str:
    """生成注入系统提示的目标文本；仅 active 目标注入，paused 不注入。"""
    goal = get_current_goal()
    if not goal or goal.get("status") != ACTIVE:
        return ""
    return (
        "# 当前持久目标（跨轮有效，必须持续推进直至完成）\n"
        f"目标：{goal['description']}\n"
        "每轮工作都应服务于该目标；若本轮取得进展，简要说明与目标的关系；"
        "在目标完成前不要偏离主题，也不要宣称目标已完成，除非确实达成。"
    )
