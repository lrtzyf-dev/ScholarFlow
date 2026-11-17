"""Compatibility facade for /goal, backed by the shared transactional ledger.

Legacy goals.json is imported without rewriting it. Per-run goals use the same
tables but never replace the user's explicitly selected global goal.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select

from .config import DATA_DIR
from .db import SessionLocal
from .goal_models import Goal
from .goal_ledger import (GoalConflict, GoalError, GoalMigrationError, OPEN_OUTCOMES,
                          create_goal, import_legacy_goals, revise_goal, snapshot_goal)

GOAL_PATH = Path(DATA_DIR) / "goals.json"
ACTIVE, PAUSED, DONE, CANCELLED = "active", "paused", "done", "cancelled"
OPEN_STATUSES, TERMINAL_STATUSES = (ACTIVE, PAUSED), (DONE, CANCELLED)


def _save(goals: list[dict]) -> None:
    """Legacy import fixture/writer; normal goal mutations never call this.

    Kept for compatibility with offline import utilities. A failed write keeps
    the original file; the ledger refuses to overwrite already migrated data.
    """
    GOAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = GOAL_PATH.with_name(GOAL_PATH.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(goals, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(GOAL_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _legacy(snapshot: dict) -> dict:
    status = ({"completed": DONE, "cancelled": CANCELLED}.get(snapshot["outcome"])
              or (PAUSED if snapshot["control_state"] == "paused" else ACTIVE))
    return {**snapshot, "status": status}


def _globals(db):
    return list(db.scalars(select(Goal).where(Goal.scope == "global")
                           .order_by(Goal.created_at, Goal.id)))


def _current(db):
    rows = [g for g in _globals(db) if g.outcome in OPEN_OUTCOMES]
    return rows[-1] if rows else None


def load_goals() -> list[dict]:
    with SessionLocal.begin() as db:
        import_legacy_goals(db, GOAL_PATH)
        return [_legacy(snapshot_goal(db, goal.id)) for goal in _globals(db)]


def get_current_goal() -> dict | None:
    with SessionLocal.begin() as db:
        import_legacy_goals(db, GOAL_PATH)
        goal = _current(db)
        return _legacy(snapshot_goal(db, goal.id)) if goal else None


def set_goal(description: str, *, source_message_id: str | None = None) -> dict:
    if not isinstance(description, str) or not description.strip():
        raise GoalError("目标描述不能为空")
    message_id = source_message_id or f"user:goal:{uuid.uuid4().hex}"
    with SessionLocal.begin() as db:
        import_legacy_goals(db, GOAL_PATH)
        for old in _globals(db):
            if old.outcome in OPEN_OUTCOMES:
                revise_goal(db, old.id, expected_revision=old.revision, action="replace",
                            source_message_id=message_id, reason="被新设置的目标取代")
        return _legacy(create_goal(db, description.strip(), scope="global", source_message_id=message_id))


def _transition(action: str, *, expected_revision: int | None = None,
                source_message_id: str | None = None) -> dict:
    with SessionLocal.begin() as db:
        import_legacy_goals(db, GOAL_PATH)
        goal = _current(db)
        if goal is None:
            raise GoalError("当前没有可操作的目标")
        result = revise_goal(db, goal.id, expected_revision=goal.revision if expected_revision is None else expected_revision,
                             action=action, source_message_id=source_message_id or f"user:goal:{uuid.uuid4().hex}")
        return _legacy(result)


def pause_goal(**kwargs) -> dict:
    return _transition("pause", **kwargs)


def resume_goal(**kwargs) -> dict:
    return _transition("resume", **kwargs)


def complete_goal(**kwargs) -> dict:
    # /goal/complete is an explicit user confirmation, not model completion.
    return _transition("confirm_complete", **kwargs)


def cancel_goal(**kwargs) -> dict:
    return _transition("cancel", **kwargs)


def active_goal_block() -> str:
    goal = get_current_goal()
    if not goal or goal["status"] != ACTIVE:
        return ""
    return ("# 当前持久目标（跨轮有效，必须持续推进直至完成）\n"
            f"目标：{goal['description']}\n"
            "每轮工作都应服务于该目标；用户要求独立保存，模型改计划不能取消要求；"
            "未获得实际完成依据时不得宣称全部完成。")
