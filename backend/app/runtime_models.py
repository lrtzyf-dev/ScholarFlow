"""Durable logical runs and immutable execution attempts (separate from UI rows)."""
from sqlalchemy import DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

from .db import Base
from .timeutil import utcnow


class RuntimeRun(Base):
    __tablename__ = "runtime_runs"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    goal_id: Mapped[str | None] = mapped_column(String(64), default=None)
    current_attempt_id: Mapped[str | None] = mapped_column(String(32), default=None)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    outcome: Mapped[str] = mapped_column(String(24), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunAttempt(Base):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "number"),
                      UniqueConstraint("run_id", "request_id"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(96), index=True)
    number: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(96), default=None)
    writer_token: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    checkpoint: Mapped[dict | None] = mapped_column(JSON, default=None)
    result: Mapped[dict | None] = mapped_column(JSON, default=None)
    usage: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class RunAdmissionRequest(Base):
    """Several lost delivery responses may refer to the same immutable attempt."""
    __tablename__ = "run_admission_requests"
    run_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
