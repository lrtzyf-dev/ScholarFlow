"""Confirmed process-local duration facts, independent of budgets and billing."""
from datetime import datetime
from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class TimingSpan(Base):
    __tablename__ = "run_timing_spans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(96), index=True)
    attempt_id: Mapped[str] = mapped_column(String(32), index=True)
    writer_token: Mapped[str] = mapped_column(String(64))
    process_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    dispatch_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    call_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    service: Mapped[str | None] = mapped_column(String(60), default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime)
    started_monotonic: Mapped[float] = mapped_column(Float)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default="open")
    clock_adjusted: Mapped[int] = mapped_column(Integer, default=0)
