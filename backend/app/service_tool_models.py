"""Independent paid service configuration, credentials and invocation receipts.

These records intentionally have no relationship to chat model credentials.
Only protected bytes are persisted; a settings read never exposes key references.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow


class ServiceToolConfiguration(Base):
    __tablename__ = "service_tool_configurations"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled_services: Mapped[list] = mapped_column(JSON, default=list)
    credential_ref: Mapped[str | None] = mapped_column(String(32), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ServiceToolCredential(Base):
    __tablename__ = "service_tool_credentials"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    protected_value: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ServiceToolInvocation(Base):
    """An unknown external outcome is never silently submitted a second time."""
    __tablename__ = "service_tool_invocations"
    __table_args__ = (UniqueConstraint("operation_key", name="uq_service_tool_operation"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    operation_key: Mapped[str] = mapped_column(String(240))
    provider: Mapped[str] = mapped_column(String(32))
    service: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    attempt_id: Mapped[str | None] = mapped_column(String(96), default=None)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    configuration_version: Mapped[int] = mapped_column(Integer)
    credential_ref: Mapped[str] = mapped_column(String(32))
    arguments_sha256: Mapped[str] = mapped_column(String(64))
    authorization: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="prepared")
    result: Mapped[dict | None] = mapped_column(JSON, default=None)
    call_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
