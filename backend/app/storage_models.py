"""Content-free local cleanup audit; independent of the deleted transcript."""
from datetime import datetime
from uuid import uuid4
from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class CleanupReceipt(Base):
    __tablename__ = "cleanup_receipts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda:uuid4().hex)
    # Never retain the title, transcript, paths, or a relationship back to source content.
    owner_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
