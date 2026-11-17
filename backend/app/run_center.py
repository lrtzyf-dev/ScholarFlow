"""A narrow, paginated read projection over existing execution owners.

No checkpoint, token, provider payload, tool arguments or partial answer is
loaded into the task list. Commands keep their existing admission endpoints.
"""
import base64
from datetime import datetime, timedelta
import hashlib
import json
from sqlalchemy import and_, case, cast, func, literal, or_, select, String, union_all
from .models import Conversation, DeletedConversation, DesktopState, ForegroundRun, TaskRecord
from .runtime_models import RuntimeRun, RunAttempt
from .goal_models import Goal
from .resource_models import PersistentSubtask
from .research_models import ResearchChildLink
from .run_inputs import RunInput
from .run_center_models import RunAttentionReceipt, RunNotificationReceipt
from .timeutil import utcnow

ATTENTION = ("waiting_permission", "waiting_input", "waiting_budget", "blocked", "partial", "failed", "interrupted", "paused")
ACTIVE = ("running", "queued", *ATTENTION)


def _projection():
    empty = literal(None, type_=String)
    def columns(item_id, kind, run, parent, cid, title, status, outcome, pause, reason, record, updated, attempt, error):
        return [item_id.label("item_id"), (literal(kind) if isinstance(kind, str) else kind).label("kind"), run.label("run_id"),
            parent.label("parent_run_id"), cid.label("conversation_id"), title.label("title"),
            status.label("raw_status"), outcome.label("outcome"), pause.label("pause_kind"),
            reason.label("reason"), record.label("record_id"), updated.label("updated_at"),
            cast(attempt, String).label("attempt_id"), func.substr(error, 1, 600).label("error")]
    f, t, r, a = ForegroundRun, TaskRecord, RuntimeRun, RunAttempt
    from . import run_store
    fg_status = func.coalesce(r.status, f.status)
    fg_status = case((and_(fg_status.in_(("running", "queued")),
        f.heartbeat_at < utcnow() - timedelta(seconds=20), f.id.not_in(list(run_store.active_runs()))), "interrupted"), else_=fg_status)
    fg = select(*columns(literal("foreground:") + f.id, "foreground", f.id, empty, f.conversation_id,
        func.substr(f.question, 1, 180), fg_status,
        func.coalesce(r.outcome, f.agent_checkpoint["result"]["outcome"].as_string(),
            case((f.agent_checkpoint["result"]["completed"].as_boolean().is_(True), "completed"),
                 (f.agent_checkpoint["result"]["completed"].as_boolean().is_(False), "partial"), else_="open")),
        f.agent_checkpoint["resource_pause"]["kind"].as_string(),
        f.agent_checkpoint["result"]["stop_reason"].as_string(),
        func.coalesce(a.result["qa_id"].as_string(), f.agent_checkpoint["result"]["qa_id"].as_string()),
        f.updated_at, r.current_attempt_id, f.error)).outerjoin(r, r.id == f.id).outerjoin(a, a.id == r.current_attempt_id)
    fg = fg.where(f.status != "dismissed")
    task_outcome = func.coalesce(r.outcome, t.result["outcome"].as_string(),
        case((t.result["completed"].as_boolean().is_(True), "completed"),
             (t.result["completed"].as_boolean().is_(False), "partial"),
             (and_(t.status == "done", t.type != "agent"), "completed"), else_="open"))
    tasks = select(*columns(literal("background:") + t.id, "background", t.id, empty, t.conversation_id,
        func.substr(t.title, 1, 180), func.coalesce(r.status, t.status), task_outcome,
        t.checkpoint["resource_pause"]["kind"].as_string(), t.result["stop_reason"].as_string(),
        t.result["qa_id"].as_string(), t.updated_at, r.current_attempt_id, t.error)).outerjoin(r, r.id == t.id)
    s = PersistentSubtask
    subtasks = select(*columns(literal("subtask:") + s.id, "subtask", s.id, s.parent_run_id, s.conversation_id,
        func.substr(func.coalesce(s.specification["task"].as_string(), s.specification["question"].as_string(),
                                 s.specification["title"].as_string(), s.specification["topic"].as_string(), "子任务"), 1, 180),
        s.state, case((s.state == "completed", "completed"), else_="open"),
        s.checkpoint["resource_pause"]["kind"].as_string(), s.result["stop_reason"].as_string(), empty,
        s.updated_at, s.attempt, s.result["error"].as_string()))
    i = RunInput
    child_admitted = or_(select(f.id).where(f.id == i.child_run_id, f.started_at.is_not(None)).exists(),
                         select(r.id).where(r.id == i.child_run_id).exists())
    inputs = select(*columns(literal("input:") + i.input_id, "input", i.run_id, i.run_id, i.conversation_id,
        func.substr(i.content, 1, 180), case((i.status == "dispatched", "blocked"), else_=i.status),
        literal("open"), empty, empty, empty, i.updated_at,
        i.revision, i.reason)).where(i.kind == "next_turn", or_(
            i.status.in_(("queued", "dispatching", "failed", "blocked")),
            and_(i.status == "dispatched", ~child_admitted)))
    # Logical runs whose UI rows are absent are still visible, without creating
    # another lifecycle or inventing a completed result.
    orphan = select(*columns(literal("run:") + r.id, case((r.kind.in_(("framework","translation","quality","matrix")), r.kind), else_="run"),
        r.id, ResearchChildLink.parent_run_id, r.conversation_id, case((r.kind.in_(("framework","translation","quality","matrix")), func.substr(Goal.description, 1, 180)), else_="执行记录"),
        r.status, r.outcome, a.checkpoint["resource_pause"]["kind"].as_string(), empty, a.result["qa_id"].as_string(), r.updated_at,
        r.current_attempt_id, a.error)).outerjoin(a, a.id == r.current_attempt_id).outerjoin(Goal, Goal.id == r.goal_id).outerjoin(
            ResearchChildLink, ResearchChildLink.child_run_id == r.id).where(
            ~select(f.id).where(f.id == r.id).exists(), ~select(t.id).where(t.id == r.id).exists())
    return union_all(fg, tasks, subtasks, inputs, orphan).subquery()


def _query():
    from .approval_store import pending_run_counts
    pending = pending_run_counts()
    u = _projection()
    raw = u.c.raw_status
    # A delivered answer may still be partial. Never map ended/done to goal
    # completed unless a verified execution result explicitly carries it.
    state = case(
        (and_(u.c.run_id.in_(list(pending)), raw.in_(("running", "pending", "queued"))), "waiting_permission"),
        (raw == "running", "running"),
        (raw.in_(("pending", "queued", "dispatching")), "queued"),
        (u.c.pause_kind == "budget", "waiting_budget"),
        (u.c.reason == "needs_clarification", "waiting_input"),
        (u.c.outcome == "completed", "completed"),
        (u.c.outcome == "blocked", "blocked"),
        (u.c.outcome == "partial", "partial"),
        (raw.in_(("failed", "blocked", "interrupted", "paused")), raw),
        (raw.in_(("cancelled", "stopped")), "cancelled"),
        (raw == "stale", "blocked"),
        else_="ended").label("status")
    query = select(u, state, Conversation.title.label("conversation_title"), Conversation.project_id,
                   Conversation.archived_at).outerjoin(Conversation, Conversation.id == u.c.conversation_id).where(
        ~select(DeletedConversation.id).where(DeletedConversation.id == u.c.conversation_id).exists())
    return query.subquery(), pending


def preferences(db):
    row = db.get(DesktopState, "run-center:preferences")
    return {"muted": bool((row.value if row else {}).get("muted", False))}


def _key(row):
    # Heartbeats and token progress deliberately do not create notifications.
    return hashlib.sha256(json.dumps([row[key] for key in (
        "item_id", "attempt_id", "status", "outcome", "error")], ensure_ascii=False).encode()).hexdigest()


def list_runs(db, *, view="active", status=None, limit=40, cursor=None, ids=None):
    q, pending = _query()
    filtered = select(q)
    if ids is not None:
        filtered = filtered.where(q.c.item_id.in_(ids))
    if status:
        filtered = filtered.where(q.c.status == status)
    elif view == "active":
        filtered = filtered.where(q.c.status.in_(ACTIVE))
    elif view == "attention":
        filtered = filtered.where(q.c.status.in_(ATTENTION))
    elif view == "history":
        filtered = filtered.where(~q.c.status.in_(ACTIVE))
    total = db.scalar(select(func.count()).select_from(filtered.subquery()))
    if cursor:
        try:
            version, scope, stamp, identity = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if version != 1 or scope != [view, status] or not isinstance(identity, str):
                raise ValueError()
            stamp = datetime.fromisoformat(stamp)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError("运行列表游标无效，请重新加载") from None
        filtered = filtered.where(or_(q.c.updated_at < stamp, and_(q.c.updated_at == stamp, q.c.item_id > identity)))
    rows = db.execute(filtered.order_by(q.c.updated_at.desc(), q.c.item_id).limit(limit + 1)).mappings().all()
    more, rows = len(rows) > limit, rows[:limit]
    prefs = preferences(db)
    receipts = {r.item_id: r.event_key for r in db.scalars(select(RunAttentionReceipt).where(
        RunAttentionReceipt.item_id.in_([row["item_id"] for row in rows])))} if rows else {}
    items = []
    target_ids = [row["parent_run_id"] or row["run_id"] for row in rows]
    background_ids = set(db.scalars(select(TaskRecord.id).where(TaskRecord.id.in_(target_ids)))) if rows else set()
    framework_ids = set(db.scalars(select(RuntimeRun.id).where(RuntimeRun.id.in_(target_ids), RuntimeRun.kind == "framework"))) if rows else set()
    translation_ids = set(db.scalars(select(RuntimeRun.id).where(RuntimeRun.id.in_(target_ids), RuntimeRun.kind == "translation"))) if rows else set()
    quality_ids = set(db.scalars(select(RuntimeRun.id).where(RuntimeRun.id.in_(target_ids), RuntimeRun.kind == "quality"))) if rows else set()
    matrix_ids = set(db.scalars(select(RuntimeRun.id).where(RuntimeRun.id.in_(target_ids), RuntimeRun.kind == "matrix"))) if rows else set()
    child_parents = set(db.scalars(select(PersistentSubtask.parent_run_id).join(RuntimeRun,
        RuntimeRun.id == PersistentSubtask.parent_run_id).where(
        PersistentSubtask.parent_run_id.in_(target_ids)).distinct())) if rows else set()
    research_child_parents = set(db.scalars(select(ResearchChildLink.parent_run_id).where(
        ResearchChildLink.parent_run_id.in_(target_ids)).distinct())) if rows else set()
    for row in rows:
        item = dict(row)
        item.pop("raw_status", None)
        item["updated_at"] = row["updated_at"].isoformat()
        item["archived_at"] = row["archived_at"].isoformat() if row["archived_at"] else None
        item["event_key"] = _key(row)
        item["unread"] = not prefs["muted"] and row["status"] in ATTENTION and receipts.get(row["item_id"]) != item["event_key"]
        item["approval_count"] = pending.get(row["run_id"], 0)
        # Subtasks and queued messages are controlled by their owning parent.
        item["target_run_id"] = row["parent_run_id"] or row["run_id"]
        item["target_kind"] = ("framework" if item["target_run_id"] in framework_ids else
            "translation" if item["target_run_id"] in translation_ids else
            "quality" if item["target_run_id"] in quality_ids else
            "matrix" if item["target_run_id"] in matrix_ids else
            "background" if item["target_run_id"] in background_ids else "foreground")
        item["child_history_available"] = item["target_run_id"] in child_parents
        item["research_children_available"] = item["target_run_id"] in research_child_parents
        items.append(item)
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([1, [view, status], last["updated_at"].isoformat(), last["item_id"]]).encode()).decode()
    return {"items": items, "total": total, "next_cursor": next_cursor, "preferences": prefs}


def acknowledge(db, events):
    ids = [event["item_id"] for event in events]
    current = {item["item_id"]: item for item in list_runs(db, view="all", limit=100, ids=ids)["items"]}
    acknowledged = []
    for event in events:
        item = current.get(event["item_id"])
        if item is None or item["event_key"] != event["event_key"]:
            continue
        row = db.get(RunAttentionReceipt, item["item_id"])
        if row is None:
            row = RunAttentionReceipt(item_id=item["item_id"], event_key=item["event_key"])
            db.add(row)
        else:
            row.event_key, row.updated_at = item["event_key"], utcnow()
        acknowledged.append(item["item_id"])
    return acknowledged


def claim_notification(db, item_id):
    if preferences(db)["muted"]:
        return None
    items = list_runs(db, view="all", limit=1, ids=[item_id])["items"]
    if not items or items[0]["status"] in ("running", "queued"):
        return None
    item = items[0]
    read = db.get(RunAttentionReceipt, item_id)
    row = db.get(RunNotificationReceipt, item_id)
    if (read and read.event_key == item["event_key"]) or (row and row.event_key == item["event_key"]):
        return None
    if row is None:
        db.add(RunNotificationReceipt(item_id=item_id, event_key=item["event_key"]))
    else:
        row.event_key, row.updated_at = item["event_key"], utcnow()
    db.flush()
    return item
