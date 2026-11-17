"""Opaque protected OAuth grants; no callback codes or PKCE secrets are logged."""
from sqlalchemy import String, Integer, Float, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class McpUserGrant(Base):
    __tablename__ = 'mcp_user_grants'
    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default='unauthorized')
    protected: Mapped[str | None] = mapped_column(Text, default=None)
    expires_at: Mapped[float] = mapped_column(Float, default=0)
    refresh_owner: Mapped[str | None] = mapped_column(String(64), default=None)
    refresh_until: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str] = mapped_column(String(256), default='')
