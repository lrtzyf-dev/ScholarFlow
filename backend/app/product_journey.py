"""Recoverable first-use state. Local observations never invoke providers."""
from datetime import datetime
from sqlalchemy import DateTime, Integer, JSON, String, func, select, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import QARecord, Paper
from .source_models import Source
from .artifact_models import ArtifactManifest
from .timeutil import utcnow


class OnboardingState(Base):
    __tablename__ = "onboarding_state"
    id: Mapped[str] = mapped_column(String(24), primary_key=True, default="first-research")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="in_progress")
    events: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def snapshot(db, conversation_id=None):
    from .model_profiles import default_profile_id, options_snapshot, public_profile
    row = db.get(OnboardingState, "first-research")
    try:
        profile_id = options_snapshot(db, conversation_id)["model_profile_id"] if conversation_id else default_profile_id(db)
        profile = public_profile(db, profile_id, usable=True)
        mode, configured = profile["mode"], profile["mode"] == "api" and not profile.get("configuration_error")
        model = {"id": profile_id, "model_id": profile["model_id"], "mode": mode, "configured": bool(configured)}
    except Exception:
        model = {"id": None, "model_id": "", "mode": "unavailable", "configured": False}
    sources = db.scalar(select(func.count()).select_from(Source).where(Source.kind.in_(("upload", "project_file", "local_file")))) or 0
    papers = db.scalar(select(func.count()).select_from(Paper)) or 0
    answers = db.scalar(select(func.count()).select_from(QARecord)) or 0
    # The milestone is source-backed output, not just any text or an offline
    # example. This does not certify the scientific correctness of the answer.
    cited = db.execute(text("""SELECT EXISTS(SELECT 1 FROM qa_records q, json_each(q.evidence) e
        WHERE json_extract(e.value,'$.locator_id') IS NOT NULL
          AND json_extract(e.value,'$.revision_id') IS NOT NULL
          AND COALESCE(json_extract(q.runtime_config,'$.mode'),'') = 'api')""")).scalar()
    artifacts = db.scalar(select(func.count()).select_from(ArtifactManifest)) or 0
    events = row.events if row else {}
    milestones = [
        {"id": "model", "title": "配置可用模型", "complete": model["configured"], "detail": "已填写当前模型配置；连接与能力需由你主动测试。" if model["configured"] else "可使用默认 DeepSeek、其他兼容接口或真实本地模型。"},
        {"id": "source", "title": "附加自己的资料", "complete": bool(sources or papers), "detail": "附件与入文献库分开；先用固定版本提问，之后可逐份选择入库。"},
        {"id": "answer", "title": "完成一次有来源的问答", "complete": bool(cited), "detail": "围绕已选资料提问；引用仍需你打开原文核对。"},
        {"id": "citation", "title": "打开回答中的原文位置", "complete": bool(events.get("citation_opened")), "detail": "打开引用看到对应版本与原句；仅打开不等于人工确认结论正确。"},
        {"id": "artifact", "title": "保存可编辑成果", "complete": bool(artifacts), "detail": "从答案“保存为可编辑成果”继续修改、导出并保留版本。"},
    ]
    return {"revision": row.revision if row else 0, "status": row.status if row else "available",
        "show_automatically": bool(row.status == "in_progress") if row else not bool(sources or papers or answers),
        "model": model, "milestones": milestones, "demo_seen": bool(events.get("demo_seen")),
        "all_observed": all(item["complete"] for item in milestones), "remote_requests": 0}


def transition(db, action, expected_revision, conversation_id=None):
    row = db.get(OnboardingState, "first-research")
    if expected_revision != (row.revision if row else 0):
        raise ValueError("引导状态已更新，请刷新后再操作")
    if row is None:
        row = OnboardingState(id="first-research", revision=0, events={})
        db.add(row)
    if action == "complete" and not snapshot(db, conversation_id)["all_observed"]:
        raise ValueError("尚有未完成的引导步骤；可以选择稍后再看")
    if action == "demo_seen":
        row.events = {**row.events, "demo_seen": utcnow().isoformat()}
    else:
        row.status = {"start": "in_progress", "skip": "dismissed", "reopen": "in_progress", "complete": "completed"}[action]
    row.revision += 1
    row.updated_at = utcnow()
    db.flush()


def citation_opened(db, conversation_id, revision_id, locator_id):
    from .source_runtime import conversation_scope
    from .source_store import read_locator
    locator = read_locator(db, locator_id, scope=conversation_scope(db, conversation_id, writing=False))
    if locator["revision_id"] != revision_id:
        raise ValueError("引用不属于这个资料版本")
    valid = db.execute(text("""SELECT EXISTS(SELECT 1 FROM qa_records q, json_each(q.evidence) e
        WHERE q.conversation_id=:cid AND json_extract(e.value,'$.locator_id')=:lid
          AND json_extract(e.value,'$.revision_id')=:rid
          AND COALESCE(json_extract(q.runtime_config,'$.mode'),'')='api')"""),
        {"cid": conversation_id, "lid": locator_id, "rid": revision_id}).scalar()
    if not valid:
        return False
    row = db.get(OnboardingState, "first-research")
    # Visiting a source in an established workspace must not enable the guide.
    if row is None:
        row = OnboardingState(id="first-research", status="dismissed", revision=0, events={})
        db.add(row)
    if not row.events.get("citation_opened"):
        row.events = {**row.events, "citation_opened": utcnow().isoformat()}
        row.revision += 1
        row.updated_at = utcnow()
        db.flush()
    return True
