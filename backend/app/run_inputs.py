"""Durable user steering/next-turn inbox, independent from observer lifetime.

Mutations flush only. The API commits reception; the executor commits application
together with its checkpoint. A received input is never reported as applied early.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from copy import deepcopy
import logging

from sqlalchemy import DateTime, Integer, JSON, String, Text, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base
from .models import Conversation, DeletedConversation, ForegroundRun, TaskRecord
from .timeutil import utcnow

logger = logging.getLogger(__name__)


class RunInputQueue(Base):
    __tablename__ = "run_input_queues"
    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class RunInput(Base):
    __tablename__ = "run_inputs"
    input_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    workspace_revision: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    goal_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    runtime_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    workspace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dispatch_options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    child_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunInputError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _target(db: Session, run_id: str, conversation_id: str):
    if db.get(DeletedConversation, conversation_id):
        raise RunInputError(410, "该会话已删除")
    run = db.get(ForegroundRun, run_id)
    if run is None:
        run = db.get(TaskRecord, run_id)
        if run is not None and run.type != "agent":
            run = None
    if run is None or run.conversation_id != conversation_id:
        raise RunInputError(404, "未找到该会话中的运行")
    return run


def _public(row: RunInput) -> dict:
    from .model_profiles import public_runtime_config
    return {key: getattr(row, key) for key in (
        "input_id", "run_id", "conversation_id", "kind", "content", "workspace_revision",
        "revision", "status", "goal_revision", "attempt_id", "reason", "child_run_id",
    )} | {"created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
          "runtime_config": public_runtime_config(row.runtime_config),
          "source_revision_ids": (row.runtime_config or {}).get("source_revision_ids", []),
          "source_binding": {key: deepcopy((row.runtime_config or {}).get(key, []))
                             for key in ("source_revision_ids", "source_bindings")}}


def list_inputs(db: Session, run_id: str, conversation_id: str) -> dict:
    _target(db, run_id, conversation_id)
    queue = db.get(RunInputQueue, run_id, populate_existing=True)
    rows = db.scalars(select(RunInput).where(RunInput.run_id == run_id).order_by(
        RunInput.created_at, RunInput.input_id).execution_options(populate_existing=True)).all()
    return {"run_id": run_id, "conversation_id": conversation_id,
            "revision": queue.revision if queue else 0,
            "inputs": [_public(row) for row in rows],
            "next_turn_dispatch": "automatic"}


def list_conversation_inputs(db: Session, conversation_id: str) -> dict:
    if db.get(DeletedConversation, conversation_id):
        raise RunInputError(410, "该会话已删除")
    ids = db.scalars(select(RunInput.run_id).where(
        RunInput.conversation_id == conversation_id).distinct()).all()
    return {"conversation_id": conversation_id,
            "queues": [list_inputs(db, run_id, conversation_id) for run_id in ids]}


def _advance(db: Session, run_id: str, conversation_id: str, expected: int) -> int:
    db.execute(insert(RunInputQueue).values(run_id=run_id, conversation_id=conversation_id,
                                          revision=0).on_conflict_do_nothing())
    changed = db.execute(update(RunInputQueue).where(
        RunInputQueue.run_id == run_id, RunInputQueue.conversation_id == conversation_id,
        RunInputQueue.revision == expected,
    ).values(revision=expected + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise RunInputError(409, "输入队列已更新，请同步后重试；输入内容已保留")
    return expected + 1


def submit_input(db: Session, run_id: str, *, input_id: str, conversation_id: str,
                 kind: str, content: str, expected_revision: int, workspace_revision: int,
                 source_revision_ids=None) -> dict:
    run = _target(db, run_id, conversation_id)
    from .source_runtime import revision_ids, freeze_sources
    from .source_store import SourceError
    try:
        source_ids = revision_ids(source_revision_ids if source_revision_ids is not None else
                                  (run.runtime_config or {}).get("source_revision_ids", []))
    except ValueError as exc:
        raise RunInputError(422, str(exc)) from None
    content = content.strip()
    if kind not in ("steer", "next_turn") or not content or len(content) > 200_000:
        raise RunInputError(422, "输入类型或内容无效")
    existing = db.get(RunInput, input_id)
    if existing is not None:
        if (existing.run_id, existing.conversation_id, existing.kind, existing.content,
            existing.workspace_revision, (existing.runtime_config or {}).get("source_revision_ids", [])) != (
                run_id, conversation_id, kind, content, workspace_revision, source_ids):
            raise RunInputError(409, "该输入编号已用于其他内容，不能重复使用")
        return {**list_inputs(db, run_id, conversation_id), "input": _public(existing)}
    conv = db.get(Conversation, conversation_id)
    if conv is not None and conv.archived_at is not None:
        raise RunInputError(409, "会话已归档，请先恢复")
    workspace = (run.workspace if isinstance(run, ForegroundRun) else
                 (run.payload or {}).get("workspace")) or {}
    bound_revision = workspace.get("workspace_revision", 0)
    if workspace_revision != bound_revision or (conv and conv.workspace_revision != workspace_revision):
        raise RunInputError(409, "工作目录已变化，请核对当前运行和输入范围")
    allowed = ("running",) if isinstance(run, ForegroundRun) else ("pending", "running")
    if run.status not in allowed:
        raise RunInputError(409, "该运行已结束或暂停，输入已保留，请选择恢复或发送新一轮")
    if isinstance(run, TaskRecord) and (run.checkpoint or {}).get("phase") in ("deliver", "completed"):
        raise RunInputError(409, "该运行已提交本轮回答，输入已保留，请发送新一轮；不会把新输入追加到已完成的执行")
    pending = db.query(RunInput).filter(RunInput.run_id == run_id, RunInput.status == "queued").count()
    if pending >= 32:
        raise RunInputError(409, "待处理输入已达32条，请先撤销或处理已有输入")
    binding, scope, dispatch_options = None, None, None
    try:
        source_binding = freeze_sources(db, conversation_id, source_ids, {})
    except SourceError as exc:
        raise RunInputError(409, str(exc)) from None
    binding = source_binding
    if kind == "next_turn":
        from . import workspace as workspace_service
        from .model_profiles import capture_run_config, ModelConfigurationError
        from .agent.paper_scope import describe, PaperScopeError
        from .models import Paper
        payload = {} if isinstance(run, ForegroundRun) else (run.payload or {})
        paper_id = run.paper_id if isinstance(run, ForegroundRun) else payload.get("paper_id", run.paper_id)
        try:
            binding = capture_run_config(db, conversation_id)
            binding.update(source_binding)
            scope = workspace_service.capture(db, conversation_id, workspace.get("project_id"),
                [f["path"] for f in workspace.get("files", [])], workspace_revision=workspace_revision)
            describe(db, paper_id)
        except (ModelConfigurationError, PaperScopeError) as exc:
            raise RunInputError(getattr(exc, "status_code", 409), str(exc)) from exc
        paper = db.get(Paper, paper_id) if paper_id else None
        dispatch_options = {"paper_id": paper_id,
            "source_revision_ids": source_ids,
            "paper_content_hash": paper.content_hash if paper else None,
            "skills": deepcopy(run.skills if isinstance(run, ForegroundRun) else payload.get("skills", [])),
            "execution_mode": run.execution_mode if isinstance(run, ForegroundRun) else payload.get("execution_mode", "execute")}
    revision = _advance(db, run_id, conversation_id, expected_revision)
    # Reception order must not depend on mutable status revisions or random
    # input IDs when two submissions share a clock tick (or the clock moves).
    received_at = utcnow()
    latest = db.scalar(select(func.max(RunInput.created_at)).where(
        RunInput.conversation_id == conversation_id))
    if latest is not None and received_at <= latest:
        received_at = latest + timedelta(microseconds=1)
    row = RunInput(input_id=input_id, run_id=run_id, conversation_id=conversation_id,
                   kind=kind, content=content, workspace_revision=workspace_revision,
                   revision=revision, status="queued", runtime_config=binding,
                   workspace=scope, dispatch_options=dispatch_options,
                   created_at=received_at, updated_at=received_at)
    db.add(row)
    db.flush()
    return {**list_inputs(db, run_id, conversation_id), "input": _public(row)}


def cancel_input(db: Session, run_id: str, input_id: str, conversation_id: str,
                 expected_revision: int) -> dict:
    _target(db, run_id, conversation_id)
    row = db.get(RunInput, input_id)
    if row is None or row.run_id != run_id or row.conversation_id != conversation_id:
        raise RunInputError(404, "未找到该输入")
    if row.status == "cancelled":
        return {**list_inputs(db, run_id, conversation_id), "input": _public(row)}
    if row.status != "queued":
        raise RunInputError(409, "该输入已生效或已处理，不能撤销")
    revision = _advance(db, run_id, conversation_id, expected_revision)
    row.status, row.revision, row.updated_at = "cancelled", revision, utcnow()
    db.flush()
    return {**list_inputs(db, run_id, conversation_id), "input": _public(row)}


def consume_steering_inputs(db: Session, run_id: str, conversation_id: str,
                            *, attempt_id: str | None = None) -> list[dict]:
    """Apply at an executor safe boundary; caller checkpoints and commits atomically."""
    run = _target(db, run_id, conversation_id)
    rows = db.scalars(select(RunInput).where(
        RunInput.run_id == run_id, RunInput.conversation_id == conversation_id,
        RunInput.kind == "steer", RunInput.status == "queued",
    ).order_by(RunInput.revision)).all()
    if not rows:
        return []
    queue = db.get(RunInputQueue, run_id, populate_existing=True)
    revision = _advance(db, run_id, conversation_id, queue.revision)
    from .goal_ledger import ensure_run_goal, record_user_input
    question = run.question if isinstance(run, ForegroundRun) else (run.payload or {}).get("question", run.title)
    goal = ensure_run_goal(db, run_id, conversation_id, question)
    applied = []
    for row in rows:
        from .source_runtime import validate_frozen_sources
        from .source_store import SourceError
        try:
            validate_frozen_sources(db, conversation_id, row.runtime_config)
        except SourceError as exc:
            row.status, row.reason = "blocked", str(exc)
            row.revision, row.updated_at = revision, utcnow()
            continue
        goal = record_user_input(db, goal["id"], expected_revision=goal["revision"],
                                 source_message_id=row.input_id, content=row.content, kind=row.kind)
        row.status, row.goal_revision = "applied", goal["revision"]
        row.attempt_id, row.revision, row.updated_at = attempt_id, revision, utcnow()
        applied.append(row)
    db.flush()
    return [_public(row) for row in applied]


def publish_input_change(result: dict) -> None:
    """Best-effort live projection; durable GET remains authoritative after navigation."""
    from . import chat_events, task_events
    try:
        channel = chat_events.get(result["run_id"])
        if channel is not None:
            event = {"input": result["input"], "revision": result["revision"],
                     "conversation_id": result["conversation_id"]}
            channel.broadcast(channel.record("run_input", event))
        task_events.notify_tasks_changed()
    except Exception:
        # Reception is already committed. An observer failure must not turn its
        # acknowledgement into a false 500 or cause the caller to create a new ID.
        logger.warning("Run input notification unavailable; durable query remains available")
