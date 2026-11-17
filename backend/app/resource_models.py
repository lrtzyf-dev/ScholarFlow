"""Durable resource policy; credentials and request bodies never belong here."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class ResourceBudget(Base):
    __tablename__ = "resource_budgets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_run_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    limit_micros: Mapped[int | None] = mapped_column(Integer, default=None)
    request_estimate_micros: Mapped[int | None] = mapped_column(Integer, default=None)
    request_price_version: Mapped[str | None] = mapped_column(String(160), default=None)
    seconds_limit_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    spent_micros: Mapped[int] = mapped_column(Integer, default=0)
    reserved_micros: Mapped[int] = mapped_column(Integer, default=0)
    active_ms: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResourceReservation(Base):
    __tablename__ = "resource_reservations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    budget_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    call_id: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    reserved_micros: Mapped[int] = mapped_column(Integer, default=0)
    settled_micros: Mapped[int | None] = mapped_column(Integer, default=None)
    price_version: Mapped[str | None] = mapped_column(String(160), default=None)
    active_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class ResourceStartupRecovery(Base):
    """One durable recovery receipt per orphan; never a supplier bill."""
    __tablename__ = "resource_startup_recoveries"
    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(32))
    budget_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(96), default=None)
    call_id: Mapped[str | None] = mapped_column(String(64), default=None)
    action: Mapped[str] = mapped_column(String(40))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    previous_state: Mapped[str] = mapped_column(String(32))
    previous_call_state: Mapped[str | None] = mapped_column(String(24), default=None)
    released_micros: Mapped[int] = mapped_column(Integer, default=0)
    previous_budget_reserved_micros: Mapped[int | None] = mapped_column(Integer, default=None)
    resulting_budget_reserved_micros: Mapped[int | None] = mapped_column(Integer, default=None)
    recovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConversationResourcePolicy(Base):
    __tablename__ = "conversation_resource_policies"
    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PersistentSubtask(Base):
    __tablename__ = "persistent_subtasks"
    __table_args__ = (UniqueConstraint("parent_run_id", "fingerprint", name="uq_persistent_subtask"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_run_id: Mapped[str] = mapped_column(String(96), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    fingerprint: Mapped[str] = mapped_column(String(64))
    specification: Mapped[dict] = mapped_column(JSON)
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    budget_id: Mapped[str | None] = mapped_column(String(64), default=None)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    writer_token: Mapped[str | None] = mapped_column(String(64), default=None)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, default=None)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PersistentSubtaskAttempt(Base):
    __tablename__ = "persistent_subtask_attempts"
    __table_args__ = (UniqueConstraint("subtask_id", "number", name="uq_persistent_subtask_attempt"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subtask_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[int] = mapped_column(Integer)
    writer_token: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="running")
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SubtaskOperationResolution(Base):
    """User reconciliation receipt; the original execution attempt stays intact."""
    __tablename__ = "subtask_operation_resolutions"
    __table_args__ = (UniqueConstraint("subtask_id", "request_id", name="uq_subtask_resolution_request"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subtask_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_run_id: Mapped[str] = mapped_column(String(96), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(80))
    operation_fingerprint: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))
    reference: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SubtaskCallLink(Base):
    """Keep parent ledger aggregation while attributing a specific child attempt."""
    __tablename__ = 'subtask_call_links'
    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subtask_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
