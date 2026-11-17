"""Import previews and connector receipts extend the existing Paper identity."""
from __future__ import annotations
from datetime import datetime
from uuid import uuid4
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


def _id(): return uuid4().hex


class ImportBatch(Base):
    __tablename__="bibliography_import_batches"
    __table_args__=(UniqueConstraint("conversation_id","request_id"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    conversation_id:Mapped[str]=mapped_column(String(64),index=True)
    request_id:Mapped[str]=mapped_column(String(96))
    request_sha256:Mapped[str]=mapped_column(String(64))
    format:Mapped[str]=mapped_column(String(24))
    source_revision_id:Mapped[str|None]=mapped_column(ForeignKey("source_revisions.id"))
    title:Mapped[str]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)


class ImportEntry(Base):
    __tablename__="bibliography_import_entries"
    __table_args__=(UniqueConstraint("batch_id","position"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    batch_id:Mapped[str]=mapped_column(ForeignKey("bibliography_import_batches.id",ondelete="CASCADE"),index=True)
    position:Mapped[int]=mapped_column(Integer)
    version:Mapped[int]=mapped_column(Integer,default=1)
    raw_entry:Mapped[str]=mapped_column(Text)
    metadata_value:Mapped[dict]=mapped_column(JSON,default=dict)
    status:Mapped[str]=mapped_column(String(24),default="pending")
    error:Mapped[str]=mapped_column(Text,default="")
    candidate_paper_id:Mapped[str|None]=mapped_column(String(32))
    candidate_fingerprint:Mapped[str|None]=mapped_column(String(64))
    paper_id:Mapped[str|None]=mapped_column(String(32))
    decision:Mapped[str|None]=mapped_column(String(24))
    decided_at:Mapped[datetime|None]=mapped_column(DateTime)


class PaperIdentifier(Base):
    __tablename__="paper_identifiers"
    __table_args__=(UniqueConstraint("kind","value"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    paper_id:Mapped[str]=mapped_column(ForeignKey("papers.id",ondelete="CASCADE"),index=True)
    kind:Mapped[str]=mapped_column(String(32))
    value:Mapped[str]=mapped_column(String(1024))


class PaperMetadataRevision(Base):
    __tablename__="paper_metadata_revisions"
    __table_args__=(UniqueConstraint("paper_id","version"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    paper_id:Mapped[str]=mapped_column(ForeignKey("papers.id",ondelete="CASCADE"),index=True)
    version:Mapped[int]=mapped_column(Integer)
    entry_id:Mapped[str|None]=mapped_column(String(32))
    before:Mapped[dict]=mapped_column(JSON)
    after:Mapped[dict]=mapped_column(JSON)
    actor:Mapped[str]=mapped_column(String(32),default="local_user")
    created_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)


class ConnectorBinding(Base):
    __tablename__="zotero_bindings"
    __table_args__=(UniqueConstraint("conversation_id","user_id","collection_key"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    conversation_id:Mapped[str]=mapped_column(String(64),index=True)
    version:Mapped[int]=mapped_column(Integer,default=1)
    user_id:Mapped[str]=mapped_column(String(32))
    collection_key:Mapped[str]=mapped_column(String(8))
    collection_name:Mapped[str]=mapped_column(Text)
    protected_credential:Mapped[str|None]=mapped_column(Text)
    status:Mapped[str]=mapped_column(String(24),default="active")
    access:Mapped[dict]=mapped_column(JSON,default=dict)
    cursor:Mapped[int]=mapped_column(Integer,default=0)
    last_batch_id:Mapped[str|None]=mapped_column(String(32))
    last_error:Mapped[str]=mapped_column(Text,default="")
    created_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)
    updated_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)


class ConnectorItem(Base):
    __tablename__="zotero_items"
    __table_args__=(UniqueConstraint("binding_id","remote_key"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    binding_id:Mapped[str]=mapped_column(ForeignKey("zotero_bindings.id",ondelete="CASCADE"),index=True)
    remote_key:Mapped[str]=mapped_column(String(8))
    remote_version:Mapped[int]=mapped_column(Integer)
    data:Mapped[dict]=mapped_column(JSON)
    state:Mapped[str]=mapped_column(String(32),default="available")
    import_entry_id:Mapped[str|None]=mapped_column(String(32))
    source_revision_id:Mapped[str|None]=mapped_column(ForeignKey("source_revisions.id"))
    updated_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)


class ConnectorItemRevision(Base):
    __tablename__="zotero_item_revisions"
    __table_args__=(UniqueConstraint("item_id","remote_version","data_sha256"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    item_id:Mapped[str]=mapped_column(ForeignKey("zotero_items.id",ondelete="CASCADE"),index=True)
    remote_version:Mapped[int]=mapped_column(Integer)
    data_sha256:Mapped[str]=mapped_column(String(64))
    data:Mapped[dict]=mapped_column(JSON)
    source_revision_id:Mapped[str|None]=mapped_column(ForeignKey("source_revisions.id"))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)


class ConnectorAttachmentVersion(Base):
    __tablename__="zotero_attachment_versions"
    __table_args__=(UniqueConstraint("item_id","remote_version"),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=_id)
    item_id:Mapped[str]=mapped_column(ForeignKey("zotero_items.id",ondelete="CASCADE"),index=True)
    remote_version:Mapped[int]=mapped_column(Integer)
    remote_md5:Mapped[str]=mapped_column(String(32))
    source_revision_id:Mapped[str]=mapped_column(ForeignKey("source_revisions.id"))
    created_at:Mapped[datetime]=mapped_column(DateTime,default=utcnow)


def _immutable(mapper,connection,target):
    raise ValueError("元数据修订不可原地修改")


event.listen(PaperMetadataRevision,"before_update",_immutable)
event.listen(ConnectorItemRevision,"before_update",_immutable)
event.listen(ConnectorAttachmentVersion,"before_update",_immutable)
