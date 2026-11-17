"""Recoverable source update drafts, separate from accepted fixed revisions."""
from datetime import datetime
from sqlalchemy import DateTime,Integer,LargeBinary,String,JSON
from sqlalchemy.orm import Mapped,mapped_column
from .db import Base
from .timeutil import utcnow


class SourceUpdateDraft(Base):
    __tablename__="source_update_drafts"
    conversation_id:Mapped[str]=mapped_column(String(64),primary_key=True)
    source_id:Mapped[str]=mapped_column(String(32),primary_key=True)
    revision:Mapped[int]=mapped_column(Integer,default=1)
    state:Mapped[str]=mapped_column(String(16),default="pending")
    kind:Mapped[str]=mapped_column(String(16),default="upload")
    local_context:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    filename:Mapped[str|None]=mapped_column(String(255),nullable=True)
    media_type:Mapped[str|None]=mapped_column(String(128),nullable=True)
    raw_bytes:Mapped[bytes|None]=mapped_column(LargeBinary,nullable=True,deferred=True)
    raw_sha256:Mapped[str|None]=mapped_column(String(64),nullable=True)
    raw_size:Mapped[int]=mapped_column(Integer,default=0)
    expected_version:Mapped[int|None]=mapped_column(Integer,nullable=True)
    baseline_revision_id:Mapped[str|None]=mapped_column(String(32),nullable=True)
    request_id:Mapped[str|None]=mapped_column(String(96),nullable=True)
    fingerprint:Mapped[str]=mapped_column(String(64))
    updated_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)
