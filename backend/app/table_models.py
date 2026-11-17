"""Version-bound, content-addressed results of actual local table computations."""
from datetime import datetime
from uuid import uuid4
from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class TableComputation(Base):
    __tablename__ = "table_computations"
    __table_args__ = (UniqueConstraint("conversation_id", "request_id", name="uq_table_computation_request"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[str] = mapped_column(String(96))
    request_sha256: Mapped[str] = mapped_column(String(64))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    extraction_fingerprint: Mapped[str] = mapped_column(String(64))
    program: Mapped[str] = mapped_column(String)
    program_sha256: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
