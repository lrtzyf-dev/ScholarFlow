"""Sidebar metadata without loading answer bodies, images or task checkpoints."""
from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from . import approval_store, run_store
from .conversation_store import backfill_conversations
from .models import Conversation, DeletedConversation, DesktopState, ForegroundRun, QARecord, TaskRecord


def conversation_summaries(db: Session, scope: str = "active", ids: list[str] | None = None) -> list[dict]:
    from .task_runner import running_task_ids

    if ids == []:
        return []
    if ids is None:
        backfill_conversations(db)
    def selected(query, column):
        return query.filter(column.in_(ids)) if ids is not None else query

    counts = {cid: (total, unread) for cid, total, unread in db.query(
        QARecord.conversation_id, func.count(QARecord.id),
        func.sum(case(((QARecord.read_at.is_(None)) & (QARecord.answer != ""), 1), else_=0)),
    ).filter(QARecord.conversation_id.in_(ids) if ids is not None else True).group_by(QARecord.conversation_id).all()}
    deleted = {row.id for row in selected(db.query(DeletedConversation.id), DeletedConversation.id).all()}
    conversations = selected(db.query(Conversation), Conversation.id).all()
    archived = {conv.id for conv in conversations if conv.archived_at is not None}
    items = {}

    def add(cid, title, created_at, updated_at, pinned_at=None, **extra):
        if not cid or cid in deleted or cid in items:
            return
        if (scope == "active" and cid in archived) or (scope == "archived" and cid not in archived):
            return
        total, unread = counts.get(cid, (0, 0))
        items[cid] = {
            "id": cid, "title": title or "新会话", "message_count": total,
            "unread_count": unread, "running_count": 0, "finishing_count": 0, "approval_count": 0,
            "created_at": created_at.isoformat() if created_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "pinned_at": pinned_at.isoformat() if pinned_at else None,
            "archived_at": None, "archive_revision": 0, **extra,
        }

    for conv in conversations:
        add(conv.id, conv.title, conv.created_at, conv.updated_at, conv.pinned_at,
            project_id=conv.project_id,
            workspace_revision=conv.workspace_revision or 0,
            pin_position=conv.pin_position,
            archived_at=conv.archived_at.isoformat() if conv.archived_at else None,
            archive_revision=conv.archive_revision)

    # Paged readers do not backfill or load the complete legacy history.
    if ids is not None and len(conversations) < len(ids):
        legacy = db.query(QARecord.conversation_id, func.substr(QARecord.question, 1, 200).label("question"),
                          QARecord.created_at).filter(QARecord.conversation_id.in_([cid for cid in ids if cid not in items])).order_by(
                              QARecord.created_at.asc(), QARecord.id.asc()).all()
        for record in legacy:
            add(record.conversation_id, record.question, record.created_at, record.created_at)

    draft_text = DesktopState.value["text"].as_string()
    drafts = db.query(DesktopState.key, DesktopState.updated_at,
                      func.substr(draft_text, 1, 2000).label("draft_text")).filter(
        DesktopState.key.like("conversation:%"),
        DesktopState.key.in_([f"conversation:{cid}" for cid in ids]) if ids is not None else True,
        (func.length(func.trim(draft_text)) > 0) | (func.json_array_length(DesktopState.value["images"]) > 0),
    ).all()
    for draft in drafts:
        cid = draft.key.removeprefix("conversation:")
        title = ((draft.draft_text or "").strip().splitlines() or ["图片草稿"])[0][:80]
        add(cid, title, draft.updated_at, draft.updated_at)
        if cid in items and not items[cid]["message_count"]:
            items[cid]["draft_only"] = True

    active_runs = run_store.active_runs()
    cutoff = datetime.utcnow() - timedelta(seconds=20)
    # No history limit: an old live run must remain reachable among many interrupted ones.
    runs = db.query(ForegroundRun.id, ForegroundRun.conversation_id,
                    func.substr(ForegroundRun.question, 1, 200).label("question"),
                    ForegroundRun.status, ForegroundRun.created_at,
                    ForegroundRun.updated_at, ForegroundRun.heartbeat_at).filter(
        ForegroundRun.conversation_id.in_(ids) if ids is not None else True,
        ForegroundRun.status.in_(("running", "interrupted", "cancelled", "failed")) |
        ForegroundRun.id.in_(list(active_runs)),
    ).order_by(ForegroundRun.updated_at.desc(), ForegroundRun.id.desc()).all()
    seen = set()
    for run in runs:
        cid = run.conversation_id
        title = (run.question.strip().splitlines() or ["已中断的回答"])[0][:80]
        add(cid, title, run.created_at, run.updated_at, draft_only=True, interrupted_only=True)
        if cid not in items:
            continue
        item = items[cid]
        if not item["message_count"] and not item.get("draft_only"):
            item.update(draft_only=True, interrupted_only=True)
        seen.add(run.id)
        if run.status == "running" and (run.id in active_runs or (run.heartbeat_at and run.heartbeat_at >= cutoff)):
            item["running_count"] += 1
            item["approval_count"] += len(approval_store.list_for_run(run.id))
        elif run.id in active_runs:
            item["finishing_count"] += 1
    for run_id, cid in active_runs.items():
        if run_id not in seen and cid in items:
            items[cid]["running_count"] += 1
            items[cid]["approval_count"] += len(approval_store.list_for_run(run_id))

    executing = running_task_ids()
    tasks = db.query(TaskRecord.id, TaskRecord.conversation_id, TaskRecord.title,
                     TaskRecord.status, TaskRecord.created_at, TaskRecord.updated_at).filter(
        TaskRecord.type == "agent",
        TaskRecord.conversation_id.in_(ids) if ids is not None else True,
        TaskRecord.status.in_(("pending", "running")) | TaskRecord.id.in_(executing),
    ).all()
    for task in tasks:
        cid = task.conversation_id
        add(cid, task.title, task.created_at, task.updated_at)
        if cid in items:
            items[cid]["running_count" if task.status in ("pending", "running") else "finishing_count"] += 1
            items[cid]["approval_count"] += len(approval_store.list_for_run(task.id))
    return sorted(items.values(), key=lambda item: (item["updated_at"] or "", item["id"]), reverse=True)
