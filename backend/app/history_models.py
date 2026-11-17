"""Immutable edit ancestry; original answers and action journals are untouched."""
from datetime import datetime
from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class HistoryRevision(Base):
    __tablename__ = "history_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(96), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    source_conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    source_record_id: Mapped[str] = mapped_column(String(64), index=True)
    branch_conversation_id: Mapped[str] = mapped_column(String(64), unique=True)
    mode: Mapped[str] = mapped_column(String(24))
    original_question: Mapped[str] = mapped_column(Text)
    revised_question: Mapped[str] = mapped_column(Text)
    submitted_question: Mapped[str | None] = mapped_column(Text, default=None)
    original_answer_sha256: Mapped[str] = mapped_column(String(64))
    source_revision_map: Mapped[dict] = mapped_column(JSON, default=dict)
    run_id: Mapped[str | None] = mapped_column(String(96), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
