"""Immutable tariffs, frozen per-call pricing, and auditable budget settlements."""
from datetime import datetime
from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class PricingVersion(Base):
    __tablename__ = 'pricing_versions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    endpoint: Mapped[str] = mapped_column(String(2048), index=True)
    service: Mapped[str] = mapped_column(String(60))
    model_id: Mapped[str | None] = mapped_column(String(256), default=None)
    profile_id: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str] = mapped_column(String(3))
    components: Mapped[list] = mapped_column(JSON)
    informational_units: Mapped[list] = mapped_column(JSON, default=list)
    complete_contract: Mapped[int] = mapped_column(Integer, default=0)
    source_reference: Mapped[str] = mapped_column(String(200))
    active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CallPriceAssessment(Base):
    __tablename__ = 'call_price_assessments'
    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tariff_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    tariff_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)
    unknown_units: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default='missing_price')
    amount: Mapped[str | None] = mapped_column(String(100), default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BudgetChargeEvent(Base):
    __tablename__ = 'budget_charge_events'
    __table_args__ = (UniqueConstraint('reservation_id', 'revision', name='uq_budget_charge_revision'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(64), index=True)
    call_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    revision: Mapped[int] = mapped_column(Integer)
    amount_micros: Mapped[int] = mapped_column(Integer)
    previous_micros: Mapped[int | None] = mapped_column(Integer, default=None)
    currency: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CallChargeConfirmation(Base):
    __tablename__ = 'call_charge_confirmations'
    __table_args__ = (UniqueConstraint('call_id', 'revision', name='uq_call_confirmation_revision'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    amount: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
