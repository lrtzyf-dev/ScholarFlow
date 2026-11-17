"""会话历史端点：列表/详情(恢复)/新建/重命名/删除。"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Conversation, ConversationMemory, ConversationOptions, DeletedConversation, DesktopState, ForegroundRun, QARecord, TaskRecord
from .. import conversation_memory
from ..conversation_store import materialize_history, ordered_pins
from ..conversation_activity import conversation_summaries
from ..conversation_query import InvalidCursor, query_conversations
from .. import run_store, task_events, task_runner
from ..timeutil import utcnow
from ..runtime_context import bind_runtime_config, public_runtime_config

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _new_id() -> str:
    return f"c_{int(time.time() * 1000):x}_{secrets.token_hex(3)}"


def _new_qa_id() -> str:
    return f"qa_{secrets.token_hex(12)}"


def _draft(db: Session, cid: str):
    row = db.get(DesktopState, f"conversation:{cid}")
    return row if row and (row.value.get("text", "").strip() or row.value.get("images")) else None


def _has_recoverable_run(db: Session, cid: str) -> bool:
    return db.query(ForegroundRun.id).filter(
        ForegroundRun.conversation_id == cid,
        ForegroundRun.status.in_(("running", "interrupted", "cancelled", "failed")),
    ).first() is not None


def _draft_title(row: DesktopState):
    return (row.value.get("text", "").strip().splitlines() or ["图片草稿"])[0][:80]


@router.get("")
def list_all(scope: Literal["active", "archived", "all"] = "active", db: Session = Depends(get_db)):
    return {"conversations": conversation_summaries(db, scope)}


@router.get("/page")
def page(scope: Literal["active", "archived", "all"] = "active",
         project_scope: Literal["all", "unassigned"] = "all",
         q: str = Query(default="", max_length=200),
         sort: Literal["updated_at", "created_at", "sidebar_updated_at", "sidebar_created_at"] = "updated_at",
         limit: int = Query(default=40, ge=1, le=100),
         cursor: str | None = Query(default=None, max_length=2048), db: Session = Depends(get_db)):
    try:
        return query_conversations(db, scope=scope, q=q, sort=sort, limit=limit, cursor=cursor,
                                   project_scope=project_scope)
    except InvalidCursor as exc:
        raise HTTPException(422, str(exc)) from exc


class LookupRequest(BaseModel):
    ids: list[str] = Field(max_length=100)
    scope: Literal["active", "archived", "all"] = "all"
    q: str = Field(default="", max_length=200)
    project_scope: Literal["all", "unassigned"] = "all"


@router.post("/lookup")
def lookup(req: LookupRequest, db: Session = Depends(get_db)):
    return query_conversations(db, ids=req.ids, scope=req.scope, q=req.q, limit=100,
                               project_scope=req.project_scope)


@router.get("/{conversation_id}/activity")
def summary(conversation_id: str, db: Session = Depends(get_db)):
    result = query_conversations(db, ids=[conversation_id], scope="all", limit=1)
    if not result["conversations"]:
        raise HTTPException(404, "会话不存在")
    return result["conversations"][0]


def _require_idle(db: Session, cid: str, action: str):
    from ..workspace_transfers import require_conversation_available
    require_conversation_available(db, cid)
    cutoff = utcnow() - timedelta(seconds=20)
    stale_runs = db.query(ForegroundRun).filter(
        ForegroundRun.conversation_id == cid,
        ForegroundRun.status == "running", ForegroundRun.heartbeat_at < cutoff,
    ).all()
    for run in stale_runs:
        run.status = "interrupted"
        run.error = "前台连接已中断，保留了最后保存的部分回答。"
        run.updated_at = utcnow()
        run.heartbeat_at = run.updated_at
        run.revision += 1
    db.flush()
    active_task = db.query(TaskRecord.id).filter(
        TaskRecord.conversation_id == cid,
        TaskRecord.status.in_(("pending", "running")) | TaskRecord.id.in_(task_runner.running_task_ids()),
    ).first()
    active_frontend = db.query(ForegroundRun.id).filter(
        ForegroundRun.conversation_id == cid, ForegroundRun.status == "running",
    ).first()
    if active_task or active_frontend or run_store.active_for_conversation(cid):
        raise HTTPException(409, f"该会话仍有任务运行，请先停止任务后{action}")


def _archive_state(conv: Conversation):
    return {"id": conv.id, "archived_at": conv.archived_at.isoformat() if conv.archived_at else None,
            "archive_revision": conv.archive_revision}


class ArchiveRequest(BaseModel):
    archived: bool
    expected_revision: int = Field(ge=0)


@router.put("/{conversation_id}/archive")
def archive(conversation_id: str, req: ArchiveRequest, db: Session = Depends(get_db)):
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        if db.get(DeletedConversation, conversation_id):
            raise HTTPException(410, "该会话已删除")
        conv = materialize_history(db, conversation_id)
        if conv is None:
            draft = _draft(db, conversation_id)
            run = db.query(ForegroundRun).filter(ForegroundRun.conversation_id == conversation_id).order_by(
                ForegroundRun.created_at.asc()).first()
            if not draft and not run:
                raise HTTPException(404, "会话不存在")
            title = _draft_title(draft) if draft else run.question[:80]
            first_at = draft.updated_at if draft else run.created_at
            last_at = draft.updated_at if draft else run.updated_at
            conv = Conversation(id=conversation_id, title=title, created_at=first_at,
                                updated_at=last_at, archive_revision=0)
            db.add(conv)
        if (conv.archived_at is not None) == req.archived:
            result = _archive_state(conv)
            db.rollback()
            return result
        if req.expected_revision != conv.archive_revision:
            raise HTTPException(409, {"message": "会话归档状态已在其他窗口变更，请刷新后重试",
                                      "current": _archive_state(conv)})
        if req.archived:
            _require_idle(db, conversation_id, "归档")
            conv.archived_at = utcnow()
            conv.pinned_at = None
            conv.pin_position = None
        else:
            conv.archived_at = None
        conv.archive_revision += 1
        db.commit()
        result = _archive_state(conv)
    task_events.notify_tasks_changed()
    return result


class ReadRequest(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/{conversation_id}/read")
def mark_read(conversation_id: str, req: ReadRequest, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    if db.get(DeletedConversation, conversation_id):
        raise HTTPException(410, "该会话已删除")
    # A receipt only covers observed IDs; concurrent new answers remain unread.
    ids = [row.id for row in db.query(QARecord.id).filter(
        QARecord.conversation_id == conversation_id, QARecord.id.in_(req.message_ids),
    ).all()]
    changed = db.query(QARecord).filter(
        QARecord.conversation_id == conversation_id, QARecord.id.in_(ids), QARecord.read_at.is_(None),
    ).update({QARecord.read_at: utcnow()}, synchronize_session=False)
    db.commit()
    if changed:
        task_events.notify_tasks_changed()
    return {"message_ids": ids}


class PinRequest(BaseModel):
    pinned: bool


PinId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\w-]+$")]


class PinOrderRequest(BaseModel):
    conversation_ids: list[PinId] = Field(max_length=10000)
    expected_order: list[PinId] = Field(max_length=10000)
    project_scope: Literal["all", "unassigned"] = "all"

    @model_validator(mode="after")
    def permutation(self):
        desired, expected = self.conversation_ids, self.expected_order
        if len(set(desired)) != len(desired) or len(set(expected)) != len(expected) or set(desired) != set(expected):
            raise ValueError("固定顺序必须包含相同且不重复的会话")
        return self


@router.put("/pins/order")
def reorder_pins(req: PinOrderRequest, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    pins = ordered_pins(db)
    selected = [conv for conv in pins if req.project_scope == "all" or conv.project_id is None]
    current = [conv.id for conv in selected]
    if req.conversation_ids == current:
        db.rollback()
        return {"conversation_ids": current}
    if req.expected_order != current:
        raise HTTPException(409, {"message": "固定会话已在其他窗口变更，请重新排序", "current_order": current})
    by_id = {conv.id: conv for conv in pins}
    # Reorder the visible subset in its existing slots; hidden project pins
    # keep their global positions and remain intact in the all-history view.
    replacements = iter(req.conversation_ids)
    selected_ids = set(current)
    merged = [next(replacements) if conv.id in selected_ids else conv.id for conv in pins]
    for position, cid in enumerate(merged):
        by_id[cid].pin_position = position
    db.commit()
    task_events.notify_tasks_changed()
    return {"conversation_ids": req.conversation_ids}


@router.put("/{conversation_id}/pin")
def pin(conversation_id: str, req: PinRequest, db: Session = Depends(get_db)):
    # Serialize with deletion so a late pin cannot recreate a deleted draft.
    db.execute(text("BEGIN IMMEDIATE"))
    if db.get(DeletedConversation, conversation_id):
        raise HTTPException(410, "该会话已删除")
    conv = materialize_history(db, conversation_id)
    if conv is not None and conv.archived_at is not None:
        raise HTTPException(409, "该会话已归档，请先恢复会话后固定")
    if conv is None:
        draft = _draft(db, conversation_id)
        run = db.query(ForegroundRun).filter(
            ForegroundRun.conversation_id == conversation_id,
            ForegroundRun.status.in_(("running", "interrupted", "cancelled", "failed")),
        ).order_by(ForegroundRun.created_at.asc()).first()
        if draft is None and run is None:
            raise HTTPException(404, "会话不存在")
        if not req.pinned:
            return {"id": conversation_id, "pinned_at": None, "pin_position": None}
        title = _draft_title(draft) if draft else (run.question.strip().splitlines() or ["已中断的回答"])[0][:80]
        created_at = draft.updated_at if draft else run.created_at
        updated_at = draft.updated_at if draft else run.updated_at
        conv = Conversation(id=conversation_id, title=title, created_at=created_at, updated_at=updated_at)
        db.add(conv)
    if req.pinned and not conv.pinned_at:
        for position, other in enumerate(ordered_pins(db), 1):
            other.pin_position = position
        conv.pinned_at = utcnow()
        conv.pin_position = 0
    elif not req.pinned:
        conv.pinned_at = None
        conv.pin_position = None
    db.commit()
    task_events.notify_tasks_changed()
    return {"id": conv.id, "pinned_at": conv.pinned_at.isoformat() if conv.pinned_at else None,
            "pin_position": conv.pin_position}


@router.get("/usage/summary")
def usage_summary_all(db: Session = Depends(get_db)):
    """汇总全部会话 token 用量，供 /usage 查看。"""
    records = db.query(QARecord).all()
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    turns = 0
    conversations: set[str] = set()
    origins: set[str] = set()
    for record in records:
        if not record.usage:
            continue
        if record.conversation_id:
            conversations.add(record.conversation_id)
        origin = record.usage_origin_id or record.id
        if origin in origins:
            continue
        origins.add(origin)
        turns += 1
        for key in total:
            total[key] += int(record.usage.get(key) or 0)
    compaction = conversation_memory.usage_summary(db)
    for key in total:
        total[key] += compaction["usage"][key]
    return {"turns": turns, "conversations": len(conversations), "usage": total, "compaction": compaction}


@router.get("/{conversation_id}")
def detail(conversation_id: str, db: Session = Depends(get_db),
           limit: int | None = Query(default=None,ge=1,le=100),
           offset: int | None = Query(default=None,ge=0,le=1000000),
           anchor: str | None = Query(default=None,max_length=64)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        db.execute(text("BEGIN IMMEDIATE"))
        conv = materialize_history(db, conversation_id)
        db.commit()
    if conv is None:
        draft = _draft(db, conversation_id)
        task = db.query(TaskRecord.title).filter(
            TaskRecord.conversation_id == conversation_id,
            TaskRecord.status.in_(("pending", "running")) | TaskRecord.id.in_(task_runner.running_task_ids()),
        ).first()
        if (draft or task or _has_recoverable_run(db, conversation_id)) and not db.get(DeletedConversation, conversation_id):
            title = _draft_title(draft) if draft else task.title if task else "已中断的回答"
            return {"id": conversation_id, "title": title, "messages": [], "draft_only": True}
        raise HTTPException(404, "会话不存在")
    query=db.query(QARecord).filter(QARecord.conversation_id==conversation_id)
    total=query.count()
    start=0
    ordered=query.order_by(QARecord.created_at.asc(),QARecord.id.asc())
    if limit is not None:
        start=max(0,total-limit) if offset is None else min(offset,max(0,total-1))
        if anchor and offset is None:
            from sqlalchemy import and_,or_
            at=db.get(QARecord,anchor)
            if at is not None and at.conversation_id==conversation_id:
                prior=query.filter(or_(QARecord.created_at<at.created_at,and_(QARecord.created_at==at.created_at,QARecord.id<at.id))).count()
                start=max(0,min(prior-limit//2,total-limit))
        ordered=ordered.offset(start).limit(limit)
    records=ordered.all()
    # Resolve only the visible page, within this conversation. Copied history
    # in a branch must not acquire edit access to its original task's goal.
    from ..runtime_models import RuntimeRun
    from ..goal_models import Goal
    run_ids = {r.runtime_config.get("run_id") for r in records
               if isinstance(r.runtime_config, dict) and isinstance(r.runtime_config.get("run_id"), str)}
    goals = dict(db.query(RuntimeRun.id, Goal.id).join(Goal, Goal.id == RuntimeRun.goal_id).filter(
        RuntimeRun.id.in_(run_ids), RuntimeRun.conversation_id == conversation_id,
        Goal.conversation_id == conversation_id).all()) if run_ids else {}
    return {
        "id": conv.id,
        "title": conv.title,
        "project_id": conv.project_id,
        "workspace_revision": conv.workspace_revision or 0,
        **_archive_state(conv),
        "history_window":{"total":total,"offset":start,"limit":limit,"previous_offset":max(0,start-(limit or total)) if start else None,
            "next_offset":start+len(records) if start+len(records)<total else None},
        "messages": [
            {
                "id": r.id,
                "goal_id": goals.get((r.runtime_config or {}).get("run_id")),
                "question": r.question,
                "paper_id": r.paper_id,
                "answer": r.answer,
                "execution_mode": r.execution_mode,
                "reviewed_plan_id": r.reviewed_plan_id,
                "plan_run_id": r.plan_run_id,
                "plan_steps": r.plan_steps or [],
                "plan_images": r.plan_images or [],
                "file_paths": [f["path"] for f in (r.workspace or {}).get("files", [])],
                "source_revision_ids": (r.runtime_config or {}).get("source_revision_ids", []),
                "source_references": [{key: source.get(key) for key in (
                    "source_id", "revision_id", "title", "media_type", "version", "document_format")}
                    for source in (r.runtime_config or {}).get("source_bindings", [])],
                "project_id": (r.workspace or {}).get("project_id"),
                "workspace_revision": (r.workspace or {}).get("workspace_revision", 0),
                "evidence": r.evidence or [],
                "claims": r.claims or [],
                "trace": r.trace or [],
                "skills": r.skills or [],
                "refusal": r.refusal,
                "latency_ms": r.latency_ms,
                "usage": r.usage,
                "runtime_config": public_runtime_config(r.runtime_config),
                "read_at": r.read_at.isoformat() if r.read_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
    }


class PlanClaimsRequest(BaseModel):
    record_ids:list[str]=Field(min_length=1,max_length=100)


@router.post("/{conversation_id}/plan-claims")
def plan_claims(conversation_id:str,req:PlanClaimsRequest,db:Session=Depends(get_db)):
    if db.get(DeletedConversation,conversation_id): raise HTTPException(410,"会话已删除")
    if not db.get(Conversation,conversation_id): raise HTTPException(404,"会话不存在")
    rows=db.query(QARecord.id,QARecord.plan_run_id).filter(QARecord.conversation_id==conversation_id,
        QARecord.execution_mode=='plan',QARecord.id.in_(req.record_ids)).all()
    return {'items':[{'id':rid,'plan_run_id':run_id} for rid,run_id in rows]}


@router.get("/{conversation_id}/usage")
def usage_summary(conversation_id: str, db: Session = Depends(get_db)):
    """汇总该会话全部轮次的 token 用量。"""
    records = (
        db.query(QARecord)
        .filter(QARecord.conversation_id == conversation_id)
        .all()
    )
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    turns = 0
    for r in records:
        if not r.usage:
            continue
        turns += 1
        for key in total:
            total[key] += int(r.usage.get(key) or 0)
    compaction = conversation_memory.usage_summary(db, conversation_id)
    for key in total:
        total[key] += compaction["usage"][key]
    return {"conversation_id": conversation_id, "turns": turns, "usage": total, "compaction": compaction}


def _memory_conversation(db, cid):
    if db.get(DeletedConversation, cid):
        raise HTTPException(410, "会话已删除")
    conv = db.get(Conversation, cid)
    if conv is None and db.query(QARecord.id).filter(QARecord.conversation_id == cid).first() is None:
        raise HTTPException(404, "会话不存在")
    return conv


@router.get("/{conversation_id}/memory")
def memory_status(conversation_id: str, db: Session = Depends(get_db)):
    _memory_conversation(db, conversation_id)
    return conversation_memory.snapshot(db, conversation_id)


class CompactRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    force: bool = False


@router.post("/{conversation_id}/memory/compact")
def compact_memory(conversation_id: str, req: CompactRequest, db: Session = Depends(get_db)):
    from ..llm import get_llm
    run_id = f"compact_{secrets.token_hex(12)}"
    with run_store.coordinated():
        conv = _memory_conversation(db, conversation_id)
        if conv and conv.archived_at:
            raise HTTPException(409, "请先恢复会话后更新摘要")
        _require_idle(db, conversation_id, "更新摘要")
        from ..model_profiles import capture_run_config, ModelConfigurationError
        try:
            binding = capture_run_config(db, conversation_id)
        except ModelConfigurationError as exc:
            db.rollback()
            raise HTTPException(exc.status_code, str(exc)) from exc
        db.commit()
        run_store.begin(run_id, conversation_id)
    status = "done"
    try:
        with bind_runtime_config(binding):
            return conversation_memory.compact(db.get_bind(), conversation_id, get_llm(), run_id=run_id,
                                               force=req.force, expected_revision=req.expected_revision)
    except conversation_memory.MemoryConflict as exc:
        status = "failed"
        raise HTTPException(409, str(exc)) from exc
    except conversation_memory.MemoryCancelled as exc:
        status = "cancelled"
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        status = "failed"
        raise
    finally:
        run_store.finish(run_id, status)


@router.post("/{conversation_id}/memory/cancel")
def cancel_compaction(conversation_id: str):
    for run_id in run_store.active_for_conversation(conversation_id):
        if run_id.startswith("compact_"):
            run_store.cancel(run_id)
    return {"cancelling": True}


class CreateRequest(BaseModel):
    title: str = "新会话"
    project_id: str | None = None


@router.post("")
def create(req: CreateRequest, db: Session = Depends(get_db)):
    from datetime import datetime

    cid = _new_id()
    now = utcnow()
    if req.project_id:
        from ..workspace import get_project, root_path, project_scope
        root_path(project_scope(get_project(db, req.project_id)))
    db.add(Conversation(id=cid, title=req.title[:200], project_id=req.project_id, created_at=now, updated_at=now))
    db.commit()
    task_events.notify_tasks_changed()
    return {"id": cid, "title": req.title[:200]}


class RenameRequest(BaseModel):
    title: str


@router.put("/{conversation_id}")
def rename(conversation_id: str, req: RenameRequest, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    conv = materialize_history(db, conversation_id)
    if conv is None:
        if (not _draft(db, conversation_id) and not _has_recoverable_run(db, conversation_id)) or db.get(DeletedConversation, conversation_id):
            raise HTTPException(404, "会话不存在")
        conv = Conversation(id=conversation_id)
        db.add(conv)
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    conv.title = title[:200]
    from datetime import datetime

    conv.updated_at = utcnow()
    db.commit()
    task_events.notify_tasks_changed()
    return {"id": conv.id, "title": conv.title}


@router.delete("/{conversation_id}")
def delete(conversation_id: str, db: Session = Depends(get_db)):
    with run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        if db.get(DeletedConversation, conversation_id):
            return {"deleted": conversation_id, "already_deleted": True}
        _require_idle(db, conversation_id, "删除")
        conv = materialize_history(db, conversation_id)
        if conv is None and not _draft(db, conversation_id) and not _has_recoverable_run(db, conversation_id):
            raise HTTPException(404, "会话不存在")
        from ..data_cleanup import remove
        cleanup = remove(db, conversation_id)
        db.add(DeletedConversation(id=conversation_id))
        db.commit()
        from ..chat_events import forget_conversation
        forget_conversation(conversation_id)
    task_events.notify_tasks_changed()
    return {"deleted": conversation_id, "cleanup": cleanup}


@router.get("/{conversation_id}/deletion-preview")
def deletion_preview(conversation_id: str, db: Session = Depends(get_db)):
    if db.get(DeletedConversation, conversation_id):
        raise HTTPException(410, "会话已删除")
    if materialize_history(db, conversation_id) is None and not _draft(db, conversation_id) and not _has_recoverable_run(db, conversation_id):
        raise HTTPException(404, "会话不存在")
    from ..data_cleanup import preview
    return preview(db, conversation_id)


class ForkRequest(BaseModel):
    after_message_id: str | None = None
    title: str | None = None


@router.post("/{conversation_id}/fork")
def fork(conversation_id: str, req: ForkRequest, db: Session = Depends(get_db)):
    """从某条消息（含）起复制为新会话，用于探索另一条思路。"""
    db.execute(text("BEGIN IMMEDIATE"))
    src = materialize_history(db, conversation_id)
    if src is None:
        raise HTTPException(404, "会话不存在")
    records = (
        db.query(QARecord)
        .filter(QARecord.conversation_id == conversation_id)
        .order_by(QARecord.created_at.asc(), QARecord.id.asc())
        .all()
    )
    if req.after_message_id:
        idx = next((i for i, r in enumerate(records) if r.id == req.after_message_id), -1)
        if idx < 0:
            raise HTTPException(404, "分叉点消息不存在")
        records = records[: idx + 1]
    if not records:
        raise HTTPException(400, "没有可分叉的消息")

    import copy
    from datetime import datetime

    new_id = _new_id()
    now = utcnow()
    base_title = req.title or f"{src.title or '会话'}（分叉）"
    db.add(Conversation(id=new_id, title=base_title[:200], project_id=src.project_id,
                        workspace_revision=src.workspace_revision, created_at=now, updated_at=now))
    source_options = db.get(ConversationOptions, conversation_id)
    if source_options:
        db.add(ConversationOptions(conversation_id=new_id, model_profile_id=source_options.model_profile_id,
                                   approval_mode=source_options.approval_mode,
                                   reasoning_effort=source_options.reasoning_effort, revision=1))
    db.flush()  # Source ownership checks must see the new scoped conversation.
    source_fork_cache = {}
    for r in records:
        from ..source_forks import fork_record_sources, remap_plan_sources
        from ..source_store import SourceError
        try:
            fork_runtime, fork_evidence = fork_record_sources(db, r, conversation_id, new_id, source_fork_cache)
        except SourceError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from None
        copied_id = _new_qa_id()
        db.add(QARecord(
            id=copied_id,
            conversation_id=new_id,
            paper_id=r.paper_id,
            question=r.question,
            answer=r.answer,
            execution_mode=r.execution_mode,
            plan_steps=remap_plan_sources(copy.deepcopy(r.plan_steps), source_fork_cache),
            plan_images=copy.deepcopy(r.plan_images),
            workspace=copy.deepcopy(r.workspace),
            claims=copy.deepcopy(r.claims),
            evidence=fork_evidence,
            trace=copy.deepcopy(r.trace),
            skills=copy.deepcopy(r.skills),
            refusal=r.refusal,
            latency_ms=r.latency_ms,
            in_memory=r.in_memory,
            usage=copy.deepcopy(r.usage),
            runtime_config=fork_runtime,
            provider_context=copy.deepcopy(r.provider_context),
            usage_origin_id=r.usage_origin_id or r.id,
            read_at=now,
            created_at=r.created_at,
        ))
        from ..memory_governance import copy_item_policy
        copy_item_policy(db, r.id, copied_id, new_id)
    db.commit()
    task_events.notify_tasks_changed()
    return {"id": new_id, "title": base_title[:200], "message_count": len(records)}
