"""核心数据表。"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    authors: Mapped[str] = mapped_column(String(1024), default="")
    abstract: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[str | None] = mapped_column(String(32))
    pdf_path: Mapped[str] = mapped_column(String(1024), default="")
    parsed_path: Mapped[str] = mapped_column(String(1024), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    version_group: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="uploaded")  # uploaded/parsing/parsed/failed
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chunks: Mapped[list["Chunk"]] = relationship(back_populates="paper", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, index=True)
    parent_index: Mapped[int] = mapped_column(Integer, default=0)
    page: Mapped[int] = mapped_column(Integer, default=0)
    bbox: Mapped[list | None] = mapped_column(JSON, default=None)  # [x0, y0, x1, y1]
    section: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(16), default="text")  # text/table/figure/caption/formula
    content: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)

    paper: Mapped[Paper] = relationship(back_populates="chunks")


class Reference(Base):
    __tablename__ = "references"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), index=True)
    ref_index: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    linked_paper_id: Mapped[str | None] = mapped_column(String(32), index=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    root_identity: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Conversation(Base):
    """会话：一次连续的多轮问答，按 conversation_id 聚合。"""
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # 与 conversation_id 相同
    title: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    pin_position: Mapped[int | None] = mapped_column(Integer, default=None)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    archive_revision: Mapped[int] = mapped_column(Integer, default=0)
    project_id: Mapped[str | None] = mapped_column(String(32), default=None)
    workspace_revision: Mapped[int] = mapped_column(Integer, default=0)


class WorkspaceSwitch(Base):
    """Reviewed conversation attachment changes; never moves checkout contents."""
    __tablename__ = 'workspace_switches'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    source_project_id: Mapped[str] = mapped_column(String(32))
    target_project_id: Mapped[str] = mapped_column(String(32))
    workspace_revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default='prepared')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class WorkspaceTransfer(Base):
    """Byte-preserving, reviewed uncommitted Git state transfer journal."""
    __tablename__ = 'workspace_transfers'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    source_project_id: Mapped[str] = mapped_column(String(32))
    target_project_id: Mapped[str] = mapped_column(String(32))
    workspace_revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    journal: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default='prepared')
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class ManagedWorktree(Base):
    """Journal for app-owned Git worktree directories; transcripts survive removal."""
    __tablename__ = 'managed_worktrees'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source_project_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), unique=True, default=None)
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    source_scope: Mapped[dict] = mapped_column(JSON)
    name: Mapped[str] = mapped_column(String(120))
    base_ref: Mapped[str] = mapped_column(String(256))
    base_head: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(256), default='')
    path: Mapped[str] = mapped_column(Text)
    parent_identity: Mapped[str] = mapped_column(String(128))
    repository: Mapped[dict] = mapped_column(JSON)
    root_identity: Mapped[str | None] = mapped_column(String(128), default=None)
    status: Mapped[str] = mapped_column(String(24), default='creating')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    saved_head: Mapped[str | None] = mapped_column(String(64), default=None)
    removal_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)
    archive_info: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str] = mapped_column(Text, default='')
    output: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProjectChange(Base):
    """Durable file journal, independent of transcript deletion."""
    __tablename__ = "project_changes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(96), index=True)
    operation_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(JSON)
    path: Mapped[str] = mapped_column(String(1024))
    operation: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(String(500), default="")
    before_text: Mapped[str | None] = mapped_column(Text)
    after_text: Mapped[str | None] = mapped_column(Text)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    error: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    undo_of: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProjectCommand(Base):
    """One durable attempt at an agent command; retained independently of chats."""
    __tablename__ = 'project_commands'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(96), index=True)
    operation_key: Mapped[str] = mapped_column(String(160), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    previous_id: Mapped[str | None] = mapped_column(String(32), default=None)
    scope: Mapped[dict] = mapped_column(JSON)
    command: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(String(1024), default='')
    cwd: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(500), default='')
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    status: Mapped[str] = mapped_column(String(20), default='proposed')
    output: Mapped[str] = mapped_column(Text, default='')
    output_limited: Mapped[int] = mapped_column(Integer, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    pid: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str] = mapped_column(Text, default='')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GitOperation(Base):
    __tablename__ = "git_operations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(JSON)
    agent_owner: Mapped[dict | None] = mapped_column(JSON)
    action: Mapped[str] = mapped_column(String(16))
    paths: Mapped[list] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict] = mapped_column(JSON)
    patch: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="prepared")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    result_head: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DeletedConversation(Base):
    __tablename__ = "deleted_conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConversationMemory(Base):
    """Incremental, disposable summary; original QA records remain authoritative."""
    __tablename__ = "conversation_memory"

    conversation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(32), default="extractive")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CompactionCall(Base):
    """Call accounting survives chat deletion; contains no transcript or secrets."""
    __tablename__ = "compaction_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="started")
    usage_kind: Mapped[str] = mapped_column(String(16), default="unknown")
    usage: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DesktopState(Base):
    """Desktop UI documents, independent of a browser origin or a saved QA row."""
    __tablename__ = "desktop_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ForegroundRun(Base):
    """Durable UI-visible checkpoints for foreground chat streams."""
    __tablename__ = "foreground_runs"
    execution_mode: Mapped[str] = mapped_column(String(16), default="execute", server_default="execute")
    reviewed_plan_id: Mapped[str | None] = mapped_column(String(64), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    workspace: Mapped[dict | None] = mapped_column(JSON, default=None)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    question: Mapped[str] = mapped_column(Text)
    paper_id: Mapped[str | None] = mapped_column(String(128))
    skills: Mapped[list] = mapped_column(JSON, default=list)
    images: Mapped[list] = mapped_column(JSON, default=list)
    partial_answer: Mapped[str] = mapped_column(Text, default="")
    phase: Mapped[str] = mapped_column(String(32), default="setup")
    status: Mapped[str] = mapped_column(String(16), default="running")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class QARecord(Base):
    __tablename__ = "qa_records"
    execution_mode: Mapped[str] = mapped_column(String(16), default="execute", server_default="execute")
    plan_run_id: Mapped[str | None] = mapped_column(String(96), default=None)
    plan_steps: Mapped[list | None] = mapped_column(JSON, default=None)
    plan_images: Mapped[list | None] = mapped_column(JSON, default=None)
    reviewed_plan_id: Mapped[str | None] = mapped_column(String(64), default=None)
    workspace: Mapped[dict | None] = mapped_column(JSON, default=None)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    paper_id: Mapped[str | None] = mapped_column(String(32), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    claims: Mapped[list | None] = mapped_column(JSON, default=None)
    evidence: Mapped[list | None] = mapped_column(JSON, default=None)  # 快照，用于恢复会话
    trace: Mapped[list | None] = mapped_column(JSON, default=None)
    skills: Mapped[list | None] = mapped_column(JSON, default=None)
    refusal: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # 是否进入未来会话记忆：1=参与上下文注入；0=仅保存记录，不载入记忆（生成记忆开关关闭时）
    in_memory: Mapped[int] = mapped_column(Integer, default=1)
    # 本轮 token 用量快照：{prompt_tokens, completion_tokens, total_tokens}
    usage: Mapped[dict | None] = mapped_column(JSON, default=None)
    usage_origin_id: Mapped[str | None] = mapped_column(String(32), default=None)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    paper_id: Mapped[str | None] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(32), default="parse")
    title: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/done/failed/paused
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict | None] = mapped_column(JSON, default=None)
    plan: Mapped[list | None] = mapped_column(JSON, default=None)      # 待办步骤（检查点）
    events: Mapped[list | None] = mapped_column(JSON, default=None)   # 近期事件日志
    result: Mapped[dict | None] = mapped_column(JSON, default=None)   # 最终答案/证据
    checkpoint: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
