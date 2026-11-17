"""Effective memory views, CAS edits and a provider-dispatch revocation fence."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from .config import settings
from .memory_governance_models import MemoryItem, MemoryLoad, MemoryPolicy
from .models import Conversation, ConversationMemory, DeletedConversation, QARecord
from .timeutil import utcnow


class MemoryScopeChanged(ValueError):
    """No provider request was dispatched with the revoked memory snapshot."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def authorize(db, cid, *, writing=False):
    if db.get(DeletedConversation, cid):
        raise HTTPException(410, "会话已删除")
    conv = db.get(Conversation, cid)
    if conv is None and db.scalar(select(QARecord.id).where(QARecord.conversation_id == cid).limit(1)) is None:
        raise HTTPException(404, "会话不存在")
    if writing and conv and conv.archived_at:
        raise HTTPException(409, "请先恢复会话后修改记忆")


def policy_revision(db, cid):
    item = db.get(MemoryPolicy, cid, populate_existing=True)
    return item.revision if item else 0


def effective_rows(db, cid):
    rows = list(db.scalars(select(QARecord).where(QARecord.conversation_id == cid)
        .order_by(QARecord.created_at, QARecord.id).execution_options(populate_existing=True)))
    policies = {p.record_id: p for p in db.scalars(select(MemoryItem).where(MemoryItem.conversation_id == cid))}
    result = []
    for row in rows:
        item = policies.get(row.id)
        if item and item.state in ("excluded", "deleted"):
            continue
        if not item and row.in_memory != 1:
            continue
        values = {column.name: getattr(row, column.name) for column in QARecord.__table__.columns}
        if item and item.replacement is not None:
            values.update(question="用户编辑的历史记忆", answer=item.replacement,
                          evidence=[], provider_context={})
        values["memory_edited"] = bool(item and item.replacement is not None)
        result.append(SimpleNamespace(**values))
    return result


def fingerprint(db, cid):
    if not cid:
        return hashlib.sha256(b"no-conversation-memory").hexdigest()
    payload = {"conversation_id": cid, "policy_revision": policy_revision(db, cid),
               "recall_enabled": bool(settings.memory_recall),
               "deleted": db.get(DeletedConversation, cid) is not None}
    payload["records"] = [{"id": r.id, "question": r.question, "answer": r.answer,
                           "evidence": r.evidence, "provider_context": r.provider_context}
                          for r in effective_rows(db, cid)] if settings.memory_recall else []
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def record_load(db, cid, run_id, fingerprint_value, rows, summarized=0, attempt_id=None):
    if not cid or not run_id:
        return
    revision = policy_revision(db, cid)
    item = db.get(MemoryLoad, run_id)
    if item is None:
        item = MemoryLoad(run_id=run_id, conversation_id=cid, generation=0)
        db.add(item)
    elif item.conversation_id != cid:
        raise MemoryScopeChanged("记忆回执不属于当前会话，已保留任务")
    item.attempt_id = attempt_id
    item.fingerprint, item.policy_revision = fingerprint_value, revision
    item.entries = [{"id": r.id, "digest": hashlib.sha256(_json([r.question, r.answer, r.evidence]).encode()).hexdigest(),
                     "mode": "summary" if i < summarized else "recent", "edited": r.memory_edited}
                    for i, r in enumerate(rows)]
    item.summary_count = summarized
    item.generation += 1
    item.updated_at = utcnow()
    db.flush()


def assert_request_memory(binding):
    """Called after provider queue admission, immediately before HTTP dispatch.

    Main runs have exactly one writer; its latest load receipt corresponds to
    the prepared request. Children must retain their own inherited fingerprint.
    """
    binding = binding or {}
    cid, run_id = binding.get("conversation_id"), binding.get("run_id")
    if not cid:
        return
    from .db import SessionLocal
    with SessionLocal() as db:
        expected = binding.get("memory_fingerprint")
        if not binding.get("subtask_id") and not binding.get("memory_request_isolated") and run_id:
            loaded = db.get(MemoryLoad, run_id)
            if loaded and loaded.conversation_id == cid:
                if loaded.attempt_id and binding.get("attempt_id") != loaded.attempt_id:
                    raise MemoryScopeChanged("执行尝试已变化，旧记忆请求未发送")
                expected = loaded.fingerprint
        if expected is not None and expected != fingerprint(db, cid):
            raise MemoryScopeChanged("记忆已修改或撤回，旧上下文请求未发送；任务已保留，可继续应用新记忆")


def inherit_loaded_memory(db, binding):
    """Freeze the parent's accepted snapshot, never silently adopt a new policy."""
    binding = dict(binding or {})
    if binding.get("subtask_id") or binding.get("memory_request_isolated"):
        return binding
    receipt = db.get(MemoryLoad, binding.get("run_id")) if binding.get("run_id") else None
    if receipt and receipt.conversation_id == binding.get("conversation_id"):
        binding["memory_fingerprint"] = receipt.fingerprint
    return binding


def copy_item_policy(db, original_id, new_id, conversation_id):
    """Branching original chat does not restore explicitly revoked memory."""
    item = db.get(MemoryItem, original_id)
    if item:
        db.add(MemoryItem(record_id=new_id, conversation_id=conversation_id,
                          revision=1, state=item.state, replacement=item.replacement))


def mutate(db, cid, rid, *, expected_revision, action, replacement=None):
    # End only the endpoint's authorization read, then serialize across windows.
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        authorize(db, cid, writing=True)
        row = db.get(QARecord, rid)
        if row is None or row.conversation_id != cid:
            raise HTTPException(404, "记忆条目不存在或不属于当前会话")
        policy = db.get(MemoryPolicy, cid)
        current = policy.revision if policy else 0
        if current != expected_revision:
            raise HTTPException(409, "记忆已在其他窗口修改，请重新载入")
        if action not in ("edit", "include", "exclude", "delete"):
            raise HTTPException(422, "记忆操作无效")
        if action == "edit" and (not isinstance(replacement, str) or not replacement.strip() or len(replacement) > 12000):
            raise HTTPException(422, "记忆文本须为1至12000字")
        if policy is None:
            policy = MemoryPolicy(conversation_id=cid, revision=0)
            db.add(policy)
        item = db.get(MemoryItem, rid)
        if item is None:
            item = MemoryItem(record_id=rid, conversation_id=cid, revision=0)
            db.add(item)
        item.state = {"edit": "included", "include": "included", "exclude": "excluded", "delete": "deleted"}[action]
        if action == "edit":
            item.replacement = replacement.strip()
        elif action == "delete":
            item.replacement = None
        item.revision += 1
        item.updated_at = utcnow()
        policy.revision += 1
        policy.updated_at = utcnow()
        db.execute(delete(ConversationMemory).where(ConversationMemory.conversation_id == cid))
        db.commit()
        return {"revision": policy.revision, "record_id": rid, "state": item.state}
    except BaseException:
        db.rollback()
        raise


def items(db, cid, *, offset=0, limit=50):
    authorize(db, cid)
    all_rows = list(db.scalars(select(QARecord).where(QARecord.conversation_id == cid)
        .order_by(QARecord.created_at.desc(), QARecord.id.desc())))
    policies = {p.record_id: p for p in db.scalars(select(MemoryItem).where(MemoryItem.conversation_id == cid))}
    rows = []
    for row in all_rows[offset:offset + limit]:
        p = policies.get(row.id)
        state = p.state if p else "included" if row.in_memory == 1 else "not_captured"
        rows.append({"id": row.id, "question": row.question[:300], "state": state,
                     "edited": bool(p and p.replacement is not None),
                     "text": p.replacement if p and p.replacement is not None else
                         f"用户：{row.question}\n助手：{row.answer}" if state != "deleted" else "",
                     "revision": p.revision if p else 0})
    current = fingerprint(db, cid)
    loads = list(db.scalars(select(MemoryLoad).where(MemoryLoad.conversation_id == cid)
                           .order_by(MemoryLoad.updated_at.desc()).limit(5)))
    return {"revision": policy_revision(db, cid), "fingerprint": current,
            "items": rows, "total": len(all_rows),
            "next_offset": offset + limit if offset + limit < len(all_rows) else None,
            "loads": [{"run_id": r.run_id, "attempt_id": r.attempt_id, "generation": r.generation,
                       "stale": r.fingerprint != current, "entries": r.entries,
                       "updated_at": r.updated_at.isoformat()} for r in loads]}
