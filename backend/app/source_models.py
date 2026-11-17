"""Scoped immutable source content, revisions, and server-derived locators."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow


def _id():
    return uuid4().hex


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    scope_type: Mapped[str] = mapped_column(String(16), index=True)
    scope_id: Mapped[str] = mapped_column(String(96), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    current_revision_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceSnapshot(Base):
    """Bodies deduplicate only inside one explicitly authorized scope."""
    __tablename__ = "source_snapshots"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "fingerprint", name="uq_source_scope_snapshot"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    scope_type: Mapped[str] = mapped_column(String(16), index=True)
    scope_id: Mapped[str] = mapped_column(String(96), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    text_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    media_type: Mapped[str] = mapped_column(String(128))
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    text: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceRevision(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("source_id", "version", name="uq_source_revision_version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title_at_capture: Mapped[str] = mapped_column(Text)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    authorization_id: Mapped[str] = mapped_column(String(160))
    origin_ref: Mapped[str | None] = mapped_column(Text, default=None)
    text_origin: Mapped[str] = mapped_column(String(24))
    parser: Mapped[str | None] = mapped_column(String(160), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceLocator(Base):
    __tablename__ = "source_locators"
    __table_args__ = (UniqueConstraint("revision_id", "fingerprint", name="uq_source_revision_locator"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    location: Mapped[dict] = mapped_column(JSON)
    quote: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceCaptureReceipt(Base):
    """A client upload retry returns its original revision, not a new source."""
    __tablename__ = "source_capture_receipts"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "request_id", name="uq_source_capture_request"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(String(96))
    request_id: Mapped[str] = mapped_column(String(96))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"))
    revision_fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceExtraction(Base):
    """One immutable local parse of one fixed revision; never silently upgraded."""
    __tablename__ = "source_extractions"
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id", ondelete="CASCADE"), primary_key=True)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    text_sha256: Mapped[str] = mapped_column(String(64))
    parser: Mapped[str] = mapped_column(String(160))
    document: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceLibraryImport(Base):
    """Explicit import receipt; repeated selection resolves to the same paper."""
    __tablename__ = "source_library_imports"
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id", ondelete="CASCADE"), primary_key=True)
    raw_sha256: Mapped[str] = mapped_column(String(64), index=True)
    paper_id: Mapped[str] = mapped_column(String(32), index=True)
    request_id: Mapped[str] = mapped_column(String(96))
    chunk_locators: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceLibraryBinding(Base):
    """The library owns a fixed original independently of any conversation."""
    __tablename__ = "source_library_bindings"
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceLibraryAnnotationOrigin(Base):
    __tablename__ = "source_library_annotation_origins"
    # Historical identity only: deleting the original conversation annotation
    # must not cascade into the independent library copy.
    origin_annotation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    library_annotation_id: Mapped[str] = mapped_column(ForeignKey("research_annotations.id", ondelete="CASCADE"), index=True)


class SourceLibraryRevisionOrigin(Base):
    __tablename__ = "source_library_revision_origins"
    # No FK to the old conversation revision: this is a provenance identity,
    # never an ownership edge back into a deleted transcript.
    origin_revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    library_revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"), index=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)


class ImmutableSourceError(ValueError):
    pass


def _immutable(mapper, connection, target):
    raise ImmutableSourceError("来源快照、版本和定位记录不可原地修改，请创建新版本")


for _model in (SourceSnapshot, SourceRevision, SourceLocator, SourceCaptureReceipt, SourceExtraction, SourceLibraryImport):
    event.listen(_model, "before_update", _immutable)
