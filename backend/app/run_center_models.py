from datetime import datetime
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class RunAttentionReceipt(Base):
    __tablename__ = "run_attention_receipts"
    item_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunNotificationReceipt(Base):
    __tablename__ = "run_notification_receipts"
    item_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
