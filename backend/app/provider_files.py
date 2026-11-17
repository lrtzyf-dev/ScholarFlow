"""Conversation-owned cloud files with durable no-reupload receipts.

The local immutable original is authoritative. A lost upload response cannot
be reconstructed from a filename alone: reconciliation exposes matching remote
metadata for cleanup, never upgrades it to verified source bytes.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import PurePath
from functools import wraps
import threading
from sqlalchemy import select, text
from .db import SessionLocal
from . import source_store, service_tool_config as config
from .model_profiles import current_policy
from .provider_models import ProviderFileBinding
from .providers.deepseek_files import FilesClient, FilesError
from .source_runtime import conversation_scope
from .timeutil import utcnow


class FileBindingError(ValueError):
    def __init__(self, message, status_code=409):
        self.status_code = status_code
        super().__init__(message)


_operation_lock = threading.RLock()
_active_operations = set()


def _single_operation(function):
    @wraps(function)
    def guarded(cid, file_id, *args, **kwargs):
        # Prevent a manual cleanup acknowledgement while an in-process upload
        # can still create a remote object after the user's confirmation.
        with _operation_lock:
            if file_id in _active_operations:
                raise FileBindingError("此云文件操作仍在运行，请等待结果后再核查或确认清理")
            _active_operations.add(file_id)
        try:
            return function(cid, file_id, *args, **kwargs)
        finally:
            with _operation_lock:
                _active_operations.discard(file_id)
    return guarded


def _filename(row):
    extension = PurePath(row.title).suffix.casefold()
    if extension == ".markdown":
        extension = ".md"
    return "sf-" + row.id + extension


def _state(row):
    if row.state == "available" and row.expires_at and row.expires_at <= utcnow():
        return "expired"
    return row.state


def snapshot(row):
    metadata = row.remote_metadata or {}
    remote_size = metadata.get("bytes")
    receipt = metadata.get("upload_receipt") or {}
    return {"id": row.id, "conversation_id": row.conversation_id, "source_revision_id": row.revision_id,
            "title": row.title, "raw_sha256": row.raw_sha256, "size_bytes": row.size_bytes,
            "purpose": row.purpose, "expires_seconds": row.expires_seconds, "version": row.version,
            "state": _state(row), "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None,
            "remote_file_id": row.remote_file_id, "identity_verified": bool(row.identity_verified),
            "identity_basis": "direct_upload_receipt" if receipt else "legacy_matching_upload_metadata" if row.identity_verified else "unconfirmed",
            "remote_size_bytes": remote_size, "remote_size_matches_original": remote_size == row.size_bytes if type(remote_size) is int else None,
            "remote_original_bytes_verified": False,
            "verification_note": "已确认上传回执与本地固定原件的关联；服务端可能转换或解析文件。远端不提供原件下载，不能核对远端原件哈希，模型回答仍需本地原文核验。",
            "error": row.error, "call_id": row.call_id, "candidates": row.candidates,
            "cleanup_verification": "user_reported_unverified" if row.state == "manual_cleanup_acknowledged" else "provider_confirmed" if row.state == "deleted" and row.remote_file_id else "not_applicable" if row.state == "deleted" else "unconfirmed",
            "local_original_retained": True, "automatic_reupload": False}


def _get(db, file_id, cid, *, writable=False):
    conversation_scope(db, cid, writing=writable)
    row = db.get(ProviderFileBinding, file_id, populate_existing=True)
    if row is None or row.conversation_id != cid:
        raise FileBindingError("此云文件不属于当前会话", 404)
    return row


def _authorize(db, row, *, cleanup=False, check_policy=True):
    conversation_scope(db, row.conversation_id, writing=not cleanup)
    if cleanup:
        from .service_tool_models import ServiceToolCredential
        from .credential_protection import unprotect
        credential = db.get(ServiceToolCredential, row.credential_ref, populate_existing=True)
        if credential is None or credential.revoked_at is not None or credential.provider != "deepseek":
            raise FileBindingError("原上传凭据已撤销，不能再访问远端；请在服务账户中清理并核对过期时间")
        return {"api_key": unprotect(credential.protected_value)}
    if check_policy and current_policy(db, row.conversation_id, row.policy) != row.policy:
        raise FileBindingError("会话权限已改变，请重新审阅云文件操作")
    return config.require_configuration(db, "files", version=row.configuration_version,
        credential_ref=row.credential_ref, reveal_key=True)


def list_bindings(cid):
    with SessionLocal() as db:
        conversation_scope(db, cid, writing=False)
        return {"items": [snapshot(v) for v in db.scalars(select(ProviderFileBinding).where(
            ProviderFileBinding.conversation_id == cid).order_by(ProviderFileBinding.created_at.desc())).all()],
            "network_requested": False}


def prepare(cid, revision_id, purpose, ttl, request_id):
    with SessionLocal.begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        scope = conversation_scope(db, cid)
        record = source_store.read_revision(db, revision_id, scope=scope, include_raw=True)
        selected = config.require_configuration(db, "files")
        extension = PurePath(record["title"]).suffix.casefold()
        document = extension in (".pdf", ".doc", ".docx", ".ppt", ".pptx")
        basic = extension in (".txt", ".md", ".markdown", ".csv", ".json", ".xlsx", ".xls", ".epub", ".mobi")
        image = extension in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")
        if not (document or basic or image) or purpose not in ("user_data", "text_recognition", "rich_layout_recognition"):
            raise FileBindingError("此文件格式或解析用途不在已核实的云文件范围", 422)
        if (document and purpose == "user_data" or basic and purpose != "user_data"
                or extension in (".bmp", ".tif", ".tiff") and purpose == "user_data"):
            raise FileBindingError("PDF/Word/PPT 需文字或图文解析；文本/表格需 user_data 用途", 422)
        if type(ttl) is not int or not 3600 <= ttl <= 2592000:
            raise FileBindingError("云文件有效期须为 1 小时至 30 天", 422)
        maximum = (100 if document else 64 if image else 32) * 1024 * 1024
        if not 0 < len(record["raw_bytes"]) <= maximum:
            raise FileBindingError("此类型的固定原件超过供应商大小限制", 422)
        old = db.scalar(select(ProviderFileBinding).where(ProviderFileBinding.conversation_id == cid,
            ProviderFileBinding.request_id == request_id))
        identity = (revision_id, record["raw_sha256"], purpose, ttl, selected["credential_ref"], selected["version"])
        if old is not None:
            if (old.revision_id, old.raw_sha256, old.purpose, old.expires_seconds, old.credential_ref, old.configuration_version) != identity:
                raise FileBindingError("上传请求编号已经绑定其他文件或配置")
            return snapshot(old)
        # Repeated clicks or model retries cannot create a second paid upload
        # while a previous same-version result is unresolved.
        for old in db.scalars(select(ProviderFileBinding).where(ProviderFileBinding.conversation_id == cid,
                ProviderFileBinding.revision_id == revision_id, ProviderFileBinding.purpose == purpose,
                ProviderFileBinding.credential_ref == selected["credential_ref"])).all():
            if _state(old) in ("uploading", "unknown", "remote_found_unknown", "delete_pending"):
                return snapshot(old)
            if old.configuration_version == selected["version"] and old.expires_seconds == ttl and _state(old) in ("prepared", "available"):
                return snapshot(old)
        row = ProviderFileBinding(conversation_id=cid, revision_id=revision_id, raw_sha256=record["raw_sha256"],
            title=record["title"], media_type=record["media_type"], size_bytes=len(record["raw_bytes"]), purpose=purpose,
            expires_seconds=ttl, request_id=request_id, configuration_version=selected["version"],
            credential_ref=selected["credential_ref"], policy=current_policy(db, cid, {}))
        db.add(row)
        db.flush()
        return snapshot(row)


def _client(row, selected, *, cleanup=False):
    def authorized():
        with SessionLocal() as check:
            current = _get(check, row.id, row.conversation_id)
            _authorize(check, current, cleanup=cleanup)
            return current.version == row.version
    return FilesClient(selected["api_key"], authorization_check=authorized)


def _match(row, info):
    # Listing/adoption only creates a cleanup candidate, never a read binding.
    # Processed size can differ, including after a lost upload response.
    return info["filename"] == _filename(row) and info["purpose"] == row.purpose


def _apply_remote(row, info, *, verified, direct_upload=False):
    # The official Files API returns stored/processed size, not a content
    # digest, and offers no original download. The synchronous HTTPS upload
    # response binds its handle to the submitted fixed original; equal byte
    # counts would not prove equal remote content. Never infer that binding
    # from a listing or a later response to an uncertain upload.
    if info["filename"] != _filename(row) or info["purpose"] != row.purpose or verified and "expires_at" not in info:
        raise FilesError("contract", dispatched=True)
    if verified and info["expires_at"] - info["created_at"] > row.expires_seconds + 60:
        raise FilesError("contract", dispatched=True)
    previous = row.remote_metadata or {}
    receipt = previous.get("upload_receipt")
    if direct_upload:
        receipt = {"basis": "direct_https_upload_response", "original_sha256": row.raw_sha256,
                   "original_size_bytes": row.size_bytes, "remote_size_bytes": info["bytes"],
                   "remote_original_bytes_verified": False}
    elif verified:
        expected_size = receipt.get("remote_size_bytes") if receipt else row.size_bytes
        if info["bytes"] != expected_size or receipt and (
                receipt.get("original_sha256") != row.raw_sha256 or receipt.get("original_size_bytes") != row.size_bytes):
            raise FilesError("contract", dispatched=True)
    elif not _match(row, info):
        raise FilesError("contract", dispatched=True)
    row.remote_file_id, row.remote_metadata = info["id"], {**info, **({"upload_receipt": receipt} if receipt else {})}
    row.expires_at = datetime.fromtimestamp(info["expires_at"], timezone.utc).replace(tzinfo=None) if "expires_at" in info else None
    row.identity_verified = int(verified)
    row.state = "available" if verified else "remote_found_unknown"
    row.error = "" if verified else "仅匹配到远端元数据，无法证明其原件字节相同；只可用于清理，不能当作可用来源。"


@_single_operation
def upload(cid, file_id, version):
    with SessionLocal.begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _get(db, file_id, cid, writable=True)
        if row.state == "available":
            return snapshot(row)
        if row.version != version or row.state != "prepared":
            raise FileBindingError("云文件版本或状态已变化；结果不明的上传不会自动重试")
        selected = _authorize(db, row)
        record = source_store.read_revision(db, row.revision_id, scope=conversation_scope(db, cid), include_raw=True)
        if record["raw_sha256"] != row.raw_sha256:
            raise FileBindingError("固定原件与上传审阅版本不一致")
        row.state, row.version, row.updated_at = "uploading", row.version + 1, utcnow()
        db.flush()
        db.expunge(row)
    client = _client(row, selected)
    try:
        info = client.upload(record["raw_bytes"], _filename(row), row.purpose, row.expires_seconds)
        with SessionLocal.begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            current = _get(db, file_id, cid)
            if current.version != row.version:
                raise FileBindingError("上传结果版本已变化，请核查远端")
            # Retain the remote handle even if a post-response revocation or
            # metadata mismatch prevents using it; cleanup remains possible.
            current.remote_file_id, current.remote_metadata = info["id"], info
            try:
                _authorize(db, current)
                _apply_remote(current, info, verified=True, direct_upload=True)
            except (ValueError, config.ServiceConfigurationError):
                current.state, current.error = "unknown", "远端已返回文件，但授权或元数据尚未通过复核；请核查或删除。"
            current.call_id, current.version, current.updated_at = client.last_call_id, current.version + 1, utcnow()
            return snapshot(current)
    except BaseException as exc:
        with SessionLocal.begin() as db:
            current = db.get(ProviderFileBinding, file_id)
            if current and current.version == row.version:
                current.state = "unknown" if client.dispatched else "prepared"
                current.error = "上传结果待核对，未自动重传。" if client.dispatched else "请求尚未发送，可在授权与预算可用后继续。"
                current.call_id, current.version, current.updated_at = client.last_call_id, current.version + 1, utcnow()
        from .request_resources import raise_if_resource_boundary
        raise_if_resource_boundary(exc)
        if isinstance(exc, (GeneratorExit, KeyboardInterrupt)):
            raise
        raise FileBindingError("云文件上传未完成；已保存当前状态，请刷新后核查。") from None


@_single_operation
def operate(cid, file_id, version, action, *, cursor=None, candidate_id=None, confirm_manual_cleanup=False):
    with SessionLocal.begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = _get(db, file_id, cid)
        if row.version != version:
            raise FileBindingError("云文件状态已变化，请刷新后重试")
        if action == "acknowledge_manual_cleanup":
            if confirm_manual_cleanup is not True:
                raise FileBindingError("需由用户明确声明已在供应商账户手工清理；此声明不能当作服务端验证", 422)
            row.state, row.version, row.updated_at = "manual_cleanup_acknowledged", row.version + 1, utcnow()
            row.remote_metadata = {**(row.remote_metadata or {}), "manual_cleanup_confirmation": {
                "actor": "local_user", "at": utcnow().isoformat() + "Z", "provider_verified": False}}
            row.error = "用户声明已在供应商账户手工清理；研序未通过服务端验证。"
            row.candidates = []
            return snapshot(row)
        selected = _authorize(db, row, cleanup=True)
        if action == "adopt_for_cleanup":
            candidate = next((v for v in row.candidates if v["id"] == candidate_id), None)
            if candidate is None or row.state not in ("unknown", "uploading"):
                raise FileBindingError("请先核查并选择当前记录中已返回的远端候选")
            _apply_remote(row, candidate, verified=False)
            row.candidates, row.version, row.updated_at = [], row.version + 1, utcnow()
            return snapshot(row)
        if action not in ("refresh", "reconcile", "delete"):
            raise FileBindingError("云文件操作无效", 422)
        if action in ("refresh", "delete") and not row.remote_file_id:
            if action == "delete" and row.state == "prepared":
                row.state, row.version = "deleted", row.version + 1
                return snapshot(row)
            raise FileBindingError("尚无远端文件标识，请先核查上传结果")
        if action == "reconcile" and row.state not in ("unknown", "uploading"):
            raise FileBindingError("只有结果不明的上传需要核查列表")
        if action == "delete":
            row.state = "delete_pending"
        row.version, row.updated_at = row.version + 1, utcnow()
        db.flush()
        db.expunge(row)
    client = _client(row, selected, cleanup=True)
    try:
        if action == "delete":
            client.delete(row.remote_file_id)
            result = None
        elif action == "refresh":
            result = client.retrieve(row.remote_file_id)
        else:
            result = client.list(purpose=row.purpose, after=cursor)
        with SessionLocal.begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            current = _get(db, file_id, cid)
            if current.version != row.version:
                raise FileBindingError("云文件在请求期间发生变化，请刷新状态")
            if action == "delete":
                current.state, current.error, current.candidates = "deleted", "", []
            elif action == "refresh":
                if result["id"] != current.remote_file_id:
                    raise FilesError("contract", dispatched=True)
                _apply_remote(current, result, verified=bool(current.identity_verified))
            else:
                current.candidates = [v for v in result["data"] if _match(current, v)]
                current.state, current.error = "unknown", "仅按独立文件名与用途找到清理候选；服务端大小可能变化，不自动认定上传成功或重传。"
            current.call_id, current.version, current.updated_at = client.last_call_id, current.version + 1, utcnow()
            response = snapshot(current)
            if action == "reconcile":
                response.update(has_more=result["has_more"], next_cursor=result["next_cursor"])
            return response
    except Exception as exc:
        from .request_resources import raise_if_resource_boundary
        raise_if_resource_boundary(exc)
        with SessionLocal.begin() as db:
            current = db.get(ProviderFileBinding, file_id)
            if current and current.version == row.version:
                current.error, current.call_id = "远端状态未确认，请重试查询；删除失败不会被标为已删除。", client.last_call_id
                if isinstance(exc, FilesError) and exc.status_code == 404 and action == "refresh":
                    current.state, current.error = "deleted", "远端已不存在此文件。"
                elif isinstance(exc, FilesError) and exc.code == "contract" and action == "refresh":
                    current.state, current.identity_verified = "unknown", 0
                current.version, current.updated_at = current.version + 1, utcnow()
        raise FileBindingError("远端操作未确认，请刷新查看状态。") from None


def require_for_read(db, file_id, cid, binding):
    from .source_runtime import require_attached_revision
    row = _get(db, file_id, cid)
    selected = _authorize(db, row, check_policy=False)
    if _state(row) != "available" or not row.identity_verified or not row.remote_file_id:
        raise FileBindingError("云文件不可用、已过期或身份未确认，未发送给模型")
    original = require_attached_revision(db, row.revision_id, conversation_id=cid, binding=binding)
    if original["raw_sha256"] != row.raw_sha256:
        raise FileBindingError("云文件与本轮固定原件不一致")
    return row, selected


def require_for_revision(db, revision_id, cid, binding):
    candidates = db.scalars(select(ProviderFileBinding).where(ProviderFileBinding.conversation_id == cid,
        ProviderFileBinding.revision_id == revision_id, ProviderFileBinding.state == "available").order_by(
        ProviderFileBinding.created_at.desc())).all()
    for row in candidates:
        try:
            return require_for_read(db, row.id, cid, binding)
        except (FileBindingError, config.ServiceConfigurationError):
            continue
    raise FileBindingError("当前附件没有可用且已确认的云文件，请在会话资料中逐份审阅上传")
