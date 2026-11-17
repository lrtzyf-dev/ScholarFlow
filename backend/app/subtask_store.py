"""Durable child nodes with fenced checkpoints and scoped result reuse."""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4
from sqlalchemy import select
from .db import SessionLocal
from .resource_budget import _write
from .resource_models import PersistentSubtask, PersistentSubtaskAttempt
from .runtime_models import RuntimeRun
from .timeutil import utcnow


class SubtaskConflict(ValueError):
    pass


def _snapshot(row):
    return {"id": row.id, "parent_run_id": row.parent_run_id, "conversation_id": row.conversation_id,
        "fingerprint": row.fingerprint, "specification": deepcopy(row.specification), "scope": deepcopy(row.scope),
        "budget_id": row.budget_id, "state": row.state, "attempt": row.attempt, "writer_token": row.writer_token,
        "checkpoint": deepcopy(row.checkpoint), "result": deepcopy(row.result),
        "cancel_requested": bool(row.cancel_requested)}


def claim(*, parent_run_id, parent_attempt_id, specification, scope, conversation_id=None, budget_id=None):
    fingerprint = hashlib.sha256(json.dumps([specification, scope], sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    with _write() as db:
        row = db.scalar(select(PersistentSubtask).where(PersistentSubtask.parent_run_id == parent_run_id,
                                                       PersistentSubtask.fingerprint == fingerprint))
        if row is None:
            row = PersistentSubtask(id=uuid4().hex, parent_run_id=parent_run_id,
                conversation_id=conversation_id, fingerprint=fingerprint,
                specification=deepcopy(specification), scope=deepcopy(scope), budget_id=budget_id, checkpoint={})
            db.add(row)
            db.flush()
        if row.state == "completed":
            return _snapshot(row), True
        if row.budget_id != budget_id:
            raise SubtaskConflict("子任务预算绑定已变化，不能绕过原共享预算")
        if row.state == "running":
            current = db.get(RuntimeRun, parent_run_id)
            old_attempt = (row.checkpoint or {}).get("parent_attempt_id")
            if (not current or not parent_attempt_id or current.current_attempt_id != parent_attempt_id
                    or old_attempt == parent_attempt_id):
                raise SubtaskConflict("子任务仍有执行者；须由父任务恢复流程确认新尝试后接管")
            old = db.scalar(select(PersistentSubtaskAttempt).where(
                PersistentSubtaskAttempt.subtask_id == row.id, PersistentSubtaskAttempt.number == row.attempt))
            if old:
                old.state = "interrupted"
        row.state = "running"
        row.attempt += 1
        row.writer_token = uuid4().hex
        row.cancel_requested = 0
        row.checkpoint = {**(row.checkpoint or {}), "parent_attempt_id": parent_attempt_id}
        row.updated_at = utcnow()
        db.add(PersistentSubtaskAttempt(id=uuid4().hex, subtask_id=row.id, number=row.attempt,
            writer_token=row.writer_token, state="running", checkpoint=deepcopy(row.checkpoint)))
        return _snapshot(row), False


def checkpoint(node_id, writer_token, payload, *, result=None, state="running"):
    with _write() as db:
        row = db.get(PersistentSubtask, node_id)
        if row is None or row.writer_token != writer_token or row.state != "running":
            raise SubtaskConflict("子任务写入者已失效")
        current = db.get(RuntimeRun, row.parent_run_id)
        parent_attempt = (row.checkpoint or {}).get("parent_attempt_id")
        if current and parent_attempt and current.current_attempt_id != parent_attempt:
            raise SubtaskConflict("父任务已由新尝试接管，旧子任务不能再写入")
        row.checkpoint = deepcopy(payload)
        if result is not None:
            row.result = deepcopy(result)
        row.state = state
        row.updated_at = utcnow()
        attempt = db.scalar(select(PersistentSubtaskAttempt).where(
            PersistentSubtaskAttempt.subtask_id == row.id, PersistentSubtaskAttempt.number == row.attempt))
        if attempt:
            attempt.checkpoint = deepcopy(payload)
            attempt.state = state
            attempt.result = deepcopy(result)
            attempt.updated_at = utcnow()


def invalidate_result(node_id, expected_attempt):
    """Keep the prior attempt for audit; restart only after changed inputs."""
    with _write() as db:
        row = db.get(PersistentSubtask, node_id)
        if row is None or row.state != "completed" or row.attempt != expected_attempt:
            raise SubtaskConflict("子任务已变化，不能覆盖当前执行")
        row.state = "stale"
        row.checkpoint = {}
        row.result = None


def request_cancel(parent_run_id):
    with _write() as db:
        rows = db.scalars(select(PersistentSubtask).where(PersistentSubtask.parent_run_id == parent_run_id,
                                                        PersistentSubtask.state != "completed")).all()
        for row in rows:
            row.cancel_requested = 1


def cancelled(node_id):
    with SessionLocal() as db:
        row = db.get(PersistentSubtask, node_id)
        return row is None or bool(row.cancel_requested)


def list_children(parent_run_id):
    with SessionLocal() as db:
        return [_snapshot(row) for row in db.scalars(select(PersistentSubtask)
            .where(PersistentSubtask.parent_run_id == parent_run_id)
            .order_by(PersistentSubtask.created_at, PersistentSubtask.id))]


def child_scope(db, binding, *, paper_id=None, workspace=None, tool_specs=None):
    """One canonical input scope for dispatch and crash-recovery decisions."""
    from .models import Paper, QARecord
    from .source_runtime import validate_frozen_sources
    from .workspace import validate_scope
    from .agent.subagent import _safe_tool_specs
    from .skill_store import list_skills, manifest
    binding = binding or {}
    workspace = workspace if workspace is not None else binding.get("workspace")
    if binding.get("source_revision_ids"):
        validate_frozen_sources(db, binding.get("conversation_id"), binding)
    if workspace:
        validate_scope(db, binding.get("conversation_id"), workspace)
    papers = db.query(Paper)
    if paper_id:
        papers = papers.filter(Paper.id == paper_id)
    history = [row.id for row in db.query(QARecord).filter(
        QARecord.conversation_id == binding.get("conversation_id"), QARecord.in_memory == 1)
        .order_by(QARecord.created_at, QARecord.id)] if binding.get("conversation_id") else []
    return {"paper_id": paper_id, "conversation_id": binding.get("conversation_id"),
        "workspace": deepcopy(workspace), "source_bindings": deepcopy(binding.get("source_bindings", [])),
        "source_revision_ids": list(binding.get("source_revision_ids", [])),
        "model_id": binding.get("model_id"), "profile_id": binding.get("profile_id"),
        "profile_version": binding.get("profile_version"), "policy_revision": binding.get("policy_revision"),
        "library_manifest": sorted((paper.id, paper.content_hash, paper.status) for paper in papers.all()),
        "history_manifest": history,
        'skill_manifest': [manifest(item) for item in list_skills(False, project_id=(workspace or {}).get('project_id'))],
        "available_tools": sorted(tool_specs if tool_specs is not None else _safe_tool_specs(), key=lambda item: item["name"])}


def resume_safety(db, *, parent_run_id, checkpoint):
    """Prove replay safety from durable child receipts, never from error text.

    Does not resume or change state. The parent must still fence its attempt,
    and the child runner rechecks live permissions/input hashes before reuse.
    """
    pending = checkpoint.get("pending_operation") if isinstance(checkpoint, dict) else None
    if not isinstance(pending, dict) or pending.get("kind") != "tool" or pending.get("name") != "spawn_subagent":
        return {"safe": False, "reason": "not_a_child_dispatch"}
    arguments = pending.get("arguments")
    tasks = arguments.get("tasks") if isinstance(arguments, dict) else None
    if not isinstance(tasks, list) or not tasks or any(not isinstance(task, dict) for task in tasks):
        return {"safe": False, "reason": "invalid_child_specification"}
    binding = checkpoint.get("runtime_config") or {}
    if binding.get("run_id") not in (None, parent_run_id):
        return {"safe": False, "reason": "parent_identity_mismatch"}
    try:
        scope = child_scope(db, {**binding, "conversation_id": checkpoint.get("conversation_id") or binding.get("conversation_id")},
            paper_id=checkpoint.get("paper_id"), workspace=checkpoint.get("workspace"))
    except Exception:
        return {"safe": False, "reason": "scope_no_longer_available"}
    allowed = {tool["name"] for tool in scope["available_tools"]}
    nodes, complete = [], 0
    for task in tasks:
        fingerprint = hashlib.sha256(json.dumps([task, scope], sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        node = db.scalar(select(PersistentSubtask).where(PersistentSubtask.parent_run_id == parent_run_id,
                                                       PersistentSubtask.fingerprint == fingerprint))
        if node is None or node.budget_id != binding.get("resource_budget_id"):
            return {"safe": False, "reason": "missing_or_mismatched_durable_child"}
        if node.state == "completed" and isinstance(node.result, dict):
            complete += 1
        else:
            state = node.checkpoint or {}
            operation = state.get("pending_operation")
            calls, index = state.get("calls", []), state.get("tool_index", 0)
            if not isinstance(calls, list) or type(index) is not int or not 0 <= index <= len(calls):
                return {"safe": False, "reason": "invalid_child_checkpoint"}
            if operation:
                name = operation.get("name") if isinstance(operation, dict) else None
                if (name not in allowed or name.startswith("mcp__") or index >= len(calls)
                        or calls[index] != operation):
                    return {"safe": False, "reason": "unknown_external_child_operation", "child_id": node.id}
        nodes.append(node.id)
    return {"safe": True, "reason": "durable_readonly_children", "child_ids": list(dict.fromkeys(nodes)),
        "completed_children": complete, "requires_live_input_revalidation": True}
