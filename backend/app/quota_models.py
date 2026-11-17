"""Account quota identities contain hashes, never usable credentials."""
from uuid import uuid4
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class QuotaPolicy(Base):
    __tablename__ = "account_quota_policy"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="accounts")
    store_id: Mapped[str] = mapped_column(String(32), default=lambda: uuid4().hex)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class QuotaGroup(Base):
    __tablename__ = "account_quota_groups"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    name: Mapped[str] = mapped_column(String(120))
    origin: Mapped[str] = mapped_column(String(400))
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    state: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class QuotaMember(Base):
    __tablename__ = "account_quota_members"
    __table_args__ = (UniqueConstraint("origin", "credential_fingerprint", name="uq_account_quota_member"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    origin: Mapped[str] = mapped_column(String(400))
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    group_id: Mapped[str] = mapped_column(ForeignKey("account_quota_groups.id"), index=True)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
