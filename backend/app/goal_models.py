"""The single persistent goal ledger, shared by /goal and individual runs."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow


def _id() -> str:
    return uuid.uuid4().hex


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    scope: Mapped[str] = mapped_column(String(24), index=True)
    run_id: Mapped[str | None] = mapped_column(String(96), unique=True, default=None)
    conversation_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    description: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(24), default="open")
    control_state: Mapped[str] = mapped_column(String(16), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source_message_id: Mapped[str] = mapped_column(String(160))
    note: Mapped[str] = mapped_column(Text, default="")
    legacy_source: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class GoalRequirement(Base):
    __tablename__ = "goal_requirements"
    __table_args__ = (UniqueConstraint("goal_id", "identity", name="uq_goal_requirement_identity"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    identity: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    phase: Mapped[str] = mapped_column(String(24), default="analyze")
    origin: Mapped[str] = mapped_column(String(24))
    source_message_id: Mapped[str] = mapped_column(String(160))
    source_revision: Mapped[int] = mapped_column(Integer)
    required: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="open")
    acceptance: Mapped[str | None] = mapped_column(Text, default=None)
    tool_name: Mapped[str | None] = mapped_column(String(160), default=None)
    tool_arguments: Mapped[dict | None] = mapped_column(JSON, default=None)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    cancelled_revision: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GoalRevision(Base):
    __tablename__ = "goal_revisions"
    __table_args__ = (UniqueConstraint("goal_id", "revision", name="uq_goal_revision"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(24))
    source_message_id: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str] = mapped_column(Text, default="")
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
