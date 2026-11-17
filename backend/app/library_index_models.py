"""Atomic library index generations, with independent historical evidence."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow


class LibraryIndexState(Base):
    __tablename__ = "library_index_states"
    paper_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    indexed_revision_id: Mapped[str | None] = mapped_column(String(32))
    current_build_id: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibraryIndexOperation(Base):
    __tablename__ = "library_index_operations"
    __table_args__ = (UniqueConstraint("paper_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    paper_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(80), unique=True)
    request_id: Mapped[str] = mapped_column(String(96))
    request_sha256: Mapped[str] = mapped_column(String(64))
    target_revision_id: Mapped[str] = mapped_column(String(32))
    target_fingerprint: Mapped[str] = mapped_column(String(64))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    expected_version: Mapped[int] = mapped_column(Integer)
    expected_primary_id: Mapped[str] = mapped_column(String(32))
    set_primary: Mapped[int] = mapped_column(Integer, default=0)
    index_mode: Mapped[str] = mapped_column(String(16))
    embedding_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    prepared: Mapped[dict] = mapped_column(JSON, default=dict)
    vectors: Mapped[bytes | None] = mapped_column(LargeBinary)
    vector_sha256: Mapped[str | None] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    published_build_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibraryIndexReceipt(Base):
    """Immutable publication facts; survives deletion of the initiating chat."""
    __tablename__ = "library_index_receipts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(32), index=True)
    revision_id: Mapped[str] = mapped_column(String(32), index=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    index_mode: Mapped[str] = mapped_column(String(16))
    embedding_fingerprint: Mapped[str | None] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column(Integer)
    chunk_locators: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibraryIndexPreparedBatch(Base):
    __tablename__ = "library_index_prepared_batches"
    operation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    start: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    dimension: Mapped[int] = mapped_column(Integer)
    embedding_fingerprint: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))


class LibraryIndexVector(Base):
    __tablename__ = "library_index_vectors"
    chunk_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(32), index=True)
    build_id: Mapped[str] = mapped_column(String(32), index=True)
    dimension: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))


class LibraryChunkArchive(Base):
    __tablename__ = "library_chunk_archives"
    chunk_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_id: Mapped[str] = mapped_column(String(32), index=True)
    document: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    vector_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    vector_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def _immutable(_mapper, _connection, _target):
    raise ValueError("已发布的索引回执和历史原文不可改写")


for _model in (LibraryIndexReceipt, LibraryIndexVector, LibraryChunkArchive):
    event.listen(_model, "before_update", _immutable)
