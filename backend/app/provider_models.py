"""Private replay and remote file records; never expose opaque transport data."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class ProviderOperationContext(Base):
    __tablename__ = "provider_operation_contexts"
    operation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProviderFileBinding(Base):
    __tablename__ = "provider_file_bindings"
    __table_args__ = (UniqueConstraint("conversation_id", "request_id", name="uq_provider_file_request"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    revision_id: Mapped[str] = mapped_column(String(32), index=True)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(40))
    expires_seconds: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(96))
    configuration_version: Mapped[int] = mapped_column(Integer)
    credential_ref: Mapped[str] = mapped_column(String(32))
    policy: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24), default="prepared")
    version: Mapped[int] = mapped_column(Integer, default=1)
    remote_file_id: Mapped[str | None] = mapped_column(String(220), default=None)
    remote_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    identity_verified: Mapped[int] = mapped_column(Integer, default=0)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    call_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
