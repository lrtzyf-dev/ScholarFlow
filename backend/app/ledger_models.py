"""Request-level resource facts; budget reservations live in a later layer."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow


class CallRecord(Base):
    __tablename__ = "call_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(160), index=True)
    run_id: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    attempt_id: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    parent_call_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    retry_of_call_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    retry_index: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(100))
    service: Mapped[str] = mapped_column(String(60))
    endpoint: Mapped[str] = mapped_column(String(2048))
    model_id: Mapped[str | None] = mapped_column(String(256), default=None)
    synthetic: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default="started")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    actual_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    estimated_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_request_id: Mapped[str | None] = mapped_column(String(200), default=None)
    error_kind: Mapped[str | None] = mapped_column(String(100), default=None)
    price_version: Mapped[str | None] = mapped_column(String(160), default=None)
    unit_prices: Mapped[dict | None] = mapped_column(JSON, default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    amount: Mapped[str | None] = mapped_column(String(100), default=None)
    amount_source: Mapped[str] = mapped_column(String(24), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class CallUsageReceipt(Base):
    __tablename__ = "call_usage_receipts"
    __table_args__ = (UniqueConstraint("call_id", "event_id", name="uq_call_usage_receipt"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    units: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
