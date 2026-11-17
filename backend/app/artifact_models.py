"""Versioned research deliverables; content and provenance commit together in SQLite."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow


def _id():
    return uuid4().hex


class ArtifactManifest(Base):
    __tablename__ = "artifact_manifests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(240))
    kind: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer, default=1)
    current_revision_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArtifactRevision(Base):
    __tablename__ = "artifact_revisions"
    __table_args__ = (UniqueConstraint("artifact_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    artifact_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_revision_id: Mapped[str | None] = mapped_column(String(32))
    restored_from: Mapped[str | None] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArtifactChange(Base):
    __tablename__ = "artifact_changes"
    __table_args__ = (UniqueConstraint("scope_key", "request_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    artifact_id: Mapped[str] = mapped_column(String(32), index=True)
    scope_key: Mapped[str] = mapped_column(String(96))
    request_id: Mapped[str] = mapped_column(String(96))
    request_sha256: Mapped[str] = mapped_column(String(64))
    base_revision_id: Mapped[str | None] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    restored_from: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    result_revision_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class ArtifactComputation(Base):
    __tablename__ = "artifact_computations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    artifact_id: Mapped[str] = mapped_column(String(32), index=True)
    revision_id: Mapped[str] = mapped_column(String(32), index=True)
    script_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArtifactExport(Base):
    """One committed export's final bytes; downloads never regenerate them."""
    __tablename__ = "artifact_exports"
    __table_args__ = (UniqueConstraint("scope_key", "request_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    artifact_id: Mapped[str] = mapped_column(String(32), index=True)
    revision_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    scope_key: Mapped[str] = mapped_column(String(96))
    request_id: Mapped[str] = mapped_column(String(96))
    request_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(240))
    format: Mapped[str] = mapped_column(String(16))
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    protocol: Mapped[str] = mapped_column(String(64), default="artifact-export-v1")
    renderer_version: Mapped[str] = mapped_column(String(96))
    filename: Mapped[str] = mapped_column(String(240))
    media_type: Mapped[str] = mapped_column(String(160))
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload_size: Mapped[int] = mapped_column(Integer)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    origin_run_id: Mapped[str | None] = mapped_column(String(96))
    origin_goal_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
