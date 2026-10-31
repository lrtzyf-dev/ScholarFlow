"""会话（Conversation）存取：聚合多轮问答、标题、时间。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Conversation, DeletedConversation, QARecord


class ConversationDeleted(ValueError):
    pass


class ConversationArchived(ValueError):
    pass


def ordered_pins(db: Session) -> list[Conversation]:
    return db.query(Conversation).filter(
        Conversation.pinned_at.isnot(None),
        Conversation.archived_at.is_(None),
        ~Conversation.id.in_(db.query(DeletedConversation.id)),
    ).order_by(Conversation.pin_position.asc().nulls_first(),
               Conversation.pinned_at.desc(), Conversation.id.desc()).all()


def check_conversation_available(db: Session, conversation_id: str | None) -> None:
    if conversation_id and db.get(DeletedConversation, conversation_id) is not None:
        raise ConversationDeleted("该会话已删除，请新建会话后重试")
    conv = db.get(Conversation, conversation_id) if conversation_id else None
    if conv is not None and conv.archived_at is not None:
        raise ConversationArchived("该会话已归档，请先恢复会话后继续")


def materialize_history(db: Session, conversation_id: str) -> Conversation | None:
    """Upgrade just the selected legacy conversation inside the caller's transaction."""
    if db.get(DeletedConversation, conversation_id) is not None:
        return None
    conv = db.get(Conversation, conversation_id)
    if conv is not None:
        return conv
    first_at, last_at = db.query(func.min(QARecord.created_at), func.max(QARecord.created_at)).filter(
        QARecord.conversation_id == conversation_id).one()
    first = db.query(func.substr(QARecord.question, 1, 200).label("question")).filter(QARecord.conversation_id == conversation_id).order_by(
        QARecord.created_at.asc(), QARecord.id.asc()).first()
    if first is None:
        return None
    conv = Conversation(id=conversation_id, title=(first.question or "历史会话")[:200],
                        created_at=first_at or datetime.utcnow(), updated_at=last_at or datetime.utcnow())
    db.add(conv)
    db.flush()
    return conv


def ensure_conversation(db: Session, conversation_id: str | None,
                        title: str | None = None) -> Conversation | None:
    """确保会话行存在；首次用问题作为标题，每次刷新 updated_at。"""
    if not conversation_id:
        return None
    check_conversation_available(db, conversation_id)
    conv = db.get(Conversation, conversation_id)
    now = datetime.utcnow()
    if conv is None:
        conv = Conversation(
            id=conversation_id,
            title=(title or "新会话")[:200],
            created_at=now, updated_at=now,
        )
        db.add(conv)
        # flush 使其进入身份映射，避免同一会话内重复创建
        db.flush()
    else:
        conv.updated_at = now
        if title and (not conv.title or conv.title == "新会话"):
            conv.title = title[:200]
    return conv


def backfill_conversations(db: Session) -> int:
    """为历史 qa_records 补建会话行（升级老数据库）。返回新建数量。"""
    rows = (
        db.query(
            QARecord.conversation_id,
            func.min(QARecord.created_at),
            func.max(QARecord.created_at),
        )
        .filter(QARecord.conversation_id.isnot(None))
        .group_by(QARecord.conversation_id)
        .all()
    )
    created = 0
    for cid, first_at, last_at in rows:
        if db.get(DeletedConversation, cid) is not None:
            continue
        if db.get(Conversation, cid) is not None:
            continue
        first_q = (
            db.query(QARecord.question)
            .filter(QARecord.conversation_id == cid)
            .order_by(QARecord.created_at.asc())
            .first()
        )
        db.add(Conversation(
            id=cid,
            title=(first_q[0] if first_q and first_q[0] else "历史会话")[:200],
            created_at=first_at or datetime.utcnow(),
            updated_at=last_at or datetime.utcnow(),
        ))
        created += 1
    if created:
        db.commit()
    return created
