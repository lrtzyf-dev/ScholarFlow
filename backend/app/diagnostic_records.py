"""Persistent, deliberately content-free diagnostic and recovery receipts."""
from datetime import datetime
import re
from uuid import uuid4
from sqlalchemy import DateTime, JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .timeutil import utcnow


class DiagnosticRecord(Base):
    __tablename__ = "diagnostic_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    run_id: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    source_revision_id: Mapped[str | None] = mapped_column(String(64), default=None)
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiagnosticRecoveryAction(Base):
    __tablename__ = "diagnostic_recovery_actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    diagnostic_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24), default="offered")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


CHECK_LABELS = {"runtime_mode":"运行模式","config_sources":"配置来源","llm":"模型配置","embedding":"向量配置",
    "database":"数据库完整性","data_dir":"数据目录可写性","disk":"磁盘空间","vectors":"向量索引","mcp":"MCP连接","params":"参数"}
FAILURES = {"authentication":"凭证未通过认证", "permission":"供应商拒绝权限", "parameters":"模型或请求参数不兼容",
    "rate_limit":"供应商限流", "quota":"供应商额度不足", "timeout":"请求超时，结果可能未知", "network":"连接未完成",
    "certificate_or_proxy":"证书或代理连接失败", "protocol":"响应协议不兼容", "configuration_changed":"配置已变化",
    "upstream":"供应商服务异常", "unknown":"接口测试未完成", "none":"测试通过"}
RECOVERY = {"inspect_local":"重新运行本地检查", "configure_model":"检查模型配置", "review_receipts":"先核对调用回执与费用",
    "open_run":"返回原运行处理", "open_source":"返回固定资料版本"}


def _id(value, length=96):
    return value if isinstance(value,str) and len(value)<=length and re.fullmatch(r"[\w:.-]+",value) else None


def save(db, report, *, kind, conversation_id=None, run_id=None, source_revision_id=None):
    if kind == "local":
        checks=[]
        for item in report.get("checks",[]):
            key=item.get("id")
            if key not in CHECK_LABELS: continue
            checks.append({"id":key,"label":CHECK_LABELS[key],"status":item.get("status") if item.get("status") in ("ok","warn","error") else "error"})
        status=report.get("overall") if report.get("overall") in ("ok","warn","error") else "error"
        payload={"checks":checks,"remote_requests":0,"stage":"local_inspection"}
        actions=["inspect_local"] + (["configure_model"] if any(c["id"] in ("llm","config_sources") and c["status"]!="ok" for c in checks) else [])
    else:
        failure=report.get("failure_kind","none" if report.get("status")=="ok" else "unknown")
        if failure not in FAILURES: failure="unknown"
        status="ok" if report.get("status")=="ok" else "error"
        # No raw endpoint, provider body, path, arbitrary exception text, model
        # output or document text enters the persisted/exportable receipt.
        payload={"service":report.get("service") if report.get("service") in ("llm","embedding") else "unknown",
            "stage":"explicit_provider_probe","failure_kind":failure,"detail":FAILURES[failure],
            "http_status":report.get("http_status") if type(report.get("http_status")) is int else None,
            "call_ids":[value for item in report.get("calls",[]) if (value:=_id(item.get("call_id")))],
            "request_receipt_count":len(report.get("calls",[])),"dispatch_count":"not_inferred_from_receipts",
            "usage_and_cost":"see_call_ledger"}
        actions=["inspect_local"] if status=="ok" else ["review_receipts","configure_model","inspect_local"]
    if run_id: actions.append("open_run")
    if source_revision_id: actions.append("open_source")
    row=DiagnosticRecord(id="diag_"+uuid4().hex,kind=kind,status=status,payload=payload,
        conversation_id=conversation_id,run_id=run_id,source_revision_id=source_revision_id)
    db.add(row)
    for action in actions:
        db.add(DiagnosticRecoveryAction(id=uuid4().hex,diagnostic_id=row.id,kind=action))
    db.flush()
    return public(db,row)


def public(db,row):
    actions=db.scalars(select(DiagnosticRecoveryAction).where(DiagnosticRecoveryAction.diagnostic_id==row.id)).all()
    return {"id":row.id,"kind":row.kind,"status":row.status,"conversation_id":row.conversation_id,
        "run_id":row.run_id,"source_revision_id":row.source_revision_id,"created_at":row.created_at.isoformat(),
        "payload":row.payload,"actions":[{"id":a.id,"kind":a.kind,"label":RECOVERY[a.kind],"state":a.state} for a in actions],
        "redaction":"no_keys_no_document_text_no_local_paths_no_raw_provider_payload"}
