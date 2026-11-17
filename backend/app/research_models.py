"""Durable user research state; source originals remain in source_models."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


def _id():
    return uuid4().hex


class ResearchMatrix(Base):
    __tablename__ = "research_matrices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MatrixRevision(Base):
    __tablename__ = "research_matrix_revisions"
    __table_args__ = (UniqueConstraint("matrix_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    matrix_id: Mapped[str] = mapped_column(ForeignKey("research_matrices.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(32), default="local_user")
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MatrixCreationRequest(Base):
    __tablename__ = "research_matrix_creation_requests"
    __table_args__ = (UniqueConstraint("conversation_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(96))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    matrix_id: Mapped[str] = mapped_column(ForeignKey("research_matrices.id", ondelete="CASCADE"))


class ReadingState(Base):
    __tablename__ = "research_reading_states"
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[dict] = mapped_column(JSON, default=dict)
    user_read: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReadingAnnotation(Base):
    __tablename__ = "research_annotations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id", ondelete="CASCADE"), index=True)
    locator_id: Mapped[str] = mapped_column(ForeignKey("source_locators.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text, default="")
    selected_text: Mapped[str] = mapped_column(Text, default="")
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MatrixRunSnapshot(Base):
    """Immutable data inputs, not another execution state machine.

    Both matrix admission and prepared retrieval corpus are scoped directly to
    the conversation, so generic data removal also removes their retained text
    and vectors. Runtime checkpoints hold only IDs/digests and cell journals.
    """
    __tablename__ = "research_matrix_run_snapshots"
    __table_args__ = (UniqueConstraint("run_id", "kind"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime_runs.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    matrix_id: Mapped[str] = mapped_column(ForeignKey("research_matrices.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    document: Mapped[dict] = mapped_column(JSON)
    vectors: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    vectors_sha256: Mapped[str] = mapped_column(String(64))
    byte_count: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AnnotationRevision(Base):
    __tablename__ = "research_annotation_revisions"
    __table_args__ = (UniqueConstraint("annotation_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    annotation_id: Mapped[str] = mapped_column(ForeignKey("research_annotations.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResearchChildLink(Base):
    """Immutable input relationship only; execution lives in RuntimeRun."""
    __tablename__ = "research_child_links"
    __table_args__ = (UniqueConstraint("parent_run_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    parent_run_id: Mapped[str] = mapped_column(ForeignKey("runtime_runs.id", ondelete="CASCADE"), index=True)
    child_run_id: Mapped[str] = mapped_column(ForeignKey("runtime_runs.id", ondelete="CASCADE"), unique=True)
    parent_goal_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(96))
    kind: Mapped[str] = mapped_column(String(24))
    input_sha256: Mapped[str] = mapped_column(String(64))
    resource_budget_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResearchChildAttemptLink(Base):
    """Immutable attribution of one research attempt to its exact parent."""
    __tablename__ = "research_child_attempt_links"
    child_attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id", ondelete="CASCADE"), primary_key=True)
    link_id: Mapped[str] = mapped_column(ForeignKey("research_child_links.id", ondelete="CASCADE"), index=True)
    parent_attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id", ondelete="CASCADE"), index=True)
    parent_writer_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResearchChildRetryGrant(Base):
    """Human authorization for one exact uncertain operation, never a state."""
    __tablename__ = "research_child_retry_grants"
    __table_args__ = (UniqueConstraint("conversation_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    link_id: Mapped[str] = mapped_column(ForeignKey("research_child_links.id", ondelete="CASCADE"), index=True)
    parent_attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id", ondelete="CASCADE"))
    parent_revision: Mapped[int] = mapped_column(Integer)
    child_attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id", ondelete="CASCADE"))
    pending_sha256: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResearchChildCommand(Base):
    """Immutable resume/apply intent; HTTP loss cannot change its payload."""
    __tablename__ = "research_child_commands"
    __table_args__ = (UniqueConstraint("link_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    link_id: Mapped[str] = mapped_column(ForeignKey("research_child_links.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[str] = mapped_column(String(96))
    input_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResearchChildRetryConsumption(Base):
    __tablename__ = "research_child_retry_consumptions"
    grant_id: Mapped[str] = mapped_column(ForeignKey("research_child_retry_grants.id", ondelete="CASCADE"), primary_key=True)
    parent_attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id", ondelete="CASCADE"))
    child_attempt_id: Mapped[str] = mapped_column(ForeignKey("run_attempts.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RelationEvidence(Base):
    __tablename__ = "research_relations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_paper_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    extracted: Mapped[dict] = mapped_column(JSON)
    effective: Mapped[dict] = mapped_column(JSON)
    manual: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RelationRevision(Base):
    __tablename__ = "research_relation_revisions"
    __table_args__ = (UniqueConstraint("relation_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    relation_id: Mapped[str] = mapped_column(ForeignKey("research_relations.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def _immutable(mapper, connection, target):
    raise ValueError("研究修订不可原地修改，请建立新修订")


for _model in (MatrixRevision, MatrixRunSnapshot, AnnotationRevision, RelationRevision,
               ResearchChildLink, ResearchChildAttemptLink, ResearchChildRetryGrant, ResearchChildRetryConsumption,
               ResearchChildCommand):
    event.listen(_model, "before_update", _immutable)
