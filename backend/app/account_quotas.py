"""Explicit account membership, CAS changes and dispatch-time policy resolution."""
from __future__ import annotations
import hashlib
import json
import math
import time
from urllib.parse import urlsplit
from sqlalchemy import select, text
from .quota_models import QuotaPolicy, QuotaGroup, QuotaMember
from .timeutil import utcnow


class QuotaPolicyError(ValueError):
    def __init__(self, message, status_code=422):
        self.status_code = status_code
        super().__init__(message)


def origin(endpoint):
    try:
        value = urlsplit(endpoint)
        if value.scheme.lower() not in ("http", "https") or not value.hostname or value.username or value.password:
            raise ValueError()
        host = value.hostname.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = "[" + host + "]"
        return f"{value.scheme.lower()}://{host}:{value.port or (443 if value.scheme.lower() == 'https' else 80)}"
    except (ValueError, UnicodeError, TypeError, AttributeError):
        raise QuotaPolicyError("账号配额需要有效的 HTTP(S) 接口地址，不能包含用户名或密码") from None


def credential_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def policy_snapshot(db):
    policy = db.get(QuotaPolicy, "accounts", populate_existing=True)
    groups = db.scalars(select(QuotaGroup).order_by(QuotaGroup.created_at, QuotaGroup.id)).all()
    if groups and policy is None:
        raise QuotaPolicyError("账号配额缺少策略版本，不能按无分组配置派发；请恢复完整备份", 409)
    return {"store_id": policy.store_id if policy else "unconfigured", "revision": policy.revision if policy else 0,
        "groups": [{"id": g.id, "name": g.name, "origin": g.origin, "concurrency": g.concurrency, "state": g.state}
                   for g in groups]}


def resolve_request(db, endpoint, fingerprint):
    """An inherited chat binding never chooses another service's account."""
    actual_origin = origin(endpoint)
    policy = policy_snapshot(db)
    member = db.scalar(select(QuotaMember).where(QuotaMember.origin == actual_origin,
        QuotaMember.credential_fingerprint == fingerprint)) if fingerprint else None
    group = next((g for g in policy["groups"] if member and g["id"] == member.group_id), None)
    if member and (group is None or group["origin"] != actual_origin):
        raise QuotaPolicyError("账号配额成员与来源不一致，请在设置中核对；本次未派发", 409)
    return {"policy": policy, "origin": actual_origin, "group_id": group["id"] if group else None}


def capture_binding(db, endpoint, fingerprint):
    resolved = resolve_request(db, endpoint, fingerprint)
    return {"origin": resolved["origin"], "credential_fingerprint": fingerprint,
            "initial_group_id": resolved["group_id"], "policy_store_id": resolved["policy"]["store_id"],
            "captured_revision": resolved["policy"]["revision"], "routing": "current_authoritative_membership"}


def accounts(db, *, internal=False):
    """Resolve only configured local credential sources; this performs no HTTP."""
    from .models import ModelProfile
    from . import model_profiles
    result = []

    def add(account_id, name, endpoint, key, version):
        if not key:
            return
        actual_origin, fp = origin(endpoint), credential_hash(key)
        result.append({"id": account_id, "name": name, "origin": actual_origin,
            "config_fingerprint": _fingerprint([account_id, actual_origin, fp, version]),
            "credential_fingerprint": fp})

    for profile in db.scalars(select(ModelProfile).where(ModelProfile.deleted_at.is_(None)).order_by(ModelProfile.created_at, ModelProfile.id)):
        try:
            add("profile:" + profile.id, profile.name, profile.base_url,
                model_profiles.read_credential(db, profile.credential_ref), profile.version)
        except (ValueError, RuntimeError):
            continue
    try:
        from . import config
        with config._settings_lock:
            config.require_available_credentials("llm_api_key")
            if config.settings.llm_api_key:
                add("legacy", "默认模型连接", config.settings.llm_base_url, config.settings.llm_api_key,
                    config.settings.legacy_credential_generation)
    except (ValueError, RuntimeError):
        pass
    try:
        from .pipeline.embeddings import embedding_connection
        embedding = embedding_connection()
        add("embedding", "Embedding 连接", embedding["base_url"], embedding["api_key"],
            _fingerprint([embedding["base_url"], embedding["model_id"]]))
    except (ValueError, RuntimeError):
        pass
    try:
        from .service_tool_models import ServiceToolConfiguration
        from .service_tool_config import require_configuration, SERVICE_ENDPOINTS
        selected = db.get(ServiceToolConfiguration, "deepseek", populate_existing=True)
        if selected and selected.enabled_services:
            service = selected.enabled_services[0]
            selected_key = require_configuration(db, service, reveal_key=True)
            add("deepseek-services", "DeepSeek 独立工具连接", SERVICE_ENDPOINTS[service], selected_key["api_key"], selected_key["version"])
    except (ValueError, RuntimeError):
        pass
    if internal:
        return result
    members = {(m.origin, m.credential_fingerprint): m.group_id for m in db.scalars(select(QuotaMember))}
    return [{k: v for k, v in item.items() if k != "credential_fingerprint"} |
            {"group_id": members.get((item["origin"], item["credential_fingerprint"])),
             "same_credential_accounts": [other["name"] for other in result if
                (other["origin"], other["credential_fingerprint"]) == (item["origin"], item["credential_fingerprint"])]} for item in result]


def public_snapshot(db):
    from .request_resources import scheduler, quota_key
    value = policy_snapshot(db)
    from sqlalchemy import func
    counts = dict(db.execute(select(QuotaMember.group_id, func.count()).group_by(QuotaMember.group_id)).all())
    with scheduler.condition:
        for group in value["groups"]:
            queue = scheduler.groups.get(quota_key(provider="", endpoint=group["origin"], quota_group_id=group["id"]))
            group.update(active=len(queue.active) if queue else 0,
                queued=sum(len(t) for t in queue.waiting.values()) if queue else 0,
                cooldown_seconds=math.ceil(max(0, queue.cooldown_until - time.monotonic())) if queue else 0,
                member_count=counts.get(group["id"], 0))
    return {"revision": value["revision"], "groups": value["groups"], "accounts": accounts(db),
        "default_concurrency": scheduler.default_limit, "scheduler_scope": "single_backend_process",
        "policy": "round_robin_sessions", "migration_requires_idle_groups": True}


def change(db, *, expected_revision, operation, group_id=None, fields=None):
    """DB writer lock + scheduler condition close late-resolution/migration races."""
    from .request_resources import scheduler, quota_key
    fields = fields or {}
    db.rollback()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(QuotaPolicy, "accounts", populate_existing=True)
        revision = row.revision if row else 0
        if type(expected_revision) is not int or revision != expected_revision:
            raise QuotaPolicyError("账号配额设置已在其他窗口修改，请刷新后核对", 409)
        if row is None:
            row = QuotaPolicy(id="accounts", revision=0); db.add(row); db.flush()
        group = db.get(QuotaGroup, group_id, populate_existing=True) if group_id else None
        if operation != "create" and group is None:
            raise QuotaPolicyError("账号配额组不存在", 404)
        affected = set()
        if operation == "create":
            if db.scalar(select(QuotaGroup.id).limit(101).offset(99)):
                raise QuotaPolicyError("最多保留100个账号配额组，请管理已有分组")
            selected_origin = origin(fields["origin"])
            if fields["origin"].rstrip("/").lower() not in (selected_origin, selected_origin.removesuffix(":443"), selected_origin.removesuffix(":80")):
                raise QuotaPolicyError("分组来源只填写接口来源，例如 https://api.deepseek.com，不包含路径")
            group = QuotaGroup(name=fields["name"].strip(), origin=selected_origin, concurrency=fields["concurrency"], state="active")
            db.add(group)
        elif operation == "update":
            group.name, group.concurrency, group.state = fields["name"].strip(), fields["concurrency"], fields["state"]
            group.updated_at = utcnow()
        elif operation == "bind":
            if group.state != "active":
                raise QuotaPolicyError("已退休分组不能接受新绑定；已有成员继续按该组限额执行", 409)
            account = next((a for a in accounts(db, internal=True) if a["id"] == fields["account_id"]), None)
            if account is None or account["config_fingerprint"] != fields["config_fingerprint"]:
                raise QuotaPolicyError("连接或凭据已变化，请刷新后重新选择账号", 409)
            if account["origin"] != group.origin:
                raise QuotaPolicyError("只能把同一接口来源的连接明确归为一个账号组")
            member = db.scalar(select(QuotaMember).where(QuotaMember.origin == account["origin"],
                QuotaMember.credential_fingerprint == account["credential_fingerprint"]))
            old_group = member.group_id if member else None
            if old_group != group.id:
                affected = {quota_key(provider="", endpoint=group.origin, quota_group_id=group.id),
                    quota_key(provider="", endpoint=account["origin"], credential_fingerprint=account["credential_fingerprint"], quota_group_id=old_group)}
                if member:
                    member.group_id, member.updated_at = group.id, utcnow()
                else:
                    db.add(QuotaMember(origin=account["origin"], credential_fingerprint=account["credential_fingerprint"], group_id=group.id))
        else:
            raise QuotaPolicyError("未知配额修改操作")
        if not group.name or len(group.name) > 120 or type(group.concurrency) is not int or not 1 <= group.concurrency <= 64 or group.state not in ("active", "retired"):
            raise QuotaPolicyError("账号组名称、状态或并发数无效；并发须在1到64之间")
        row.revision += 1
        db.flush()
        snapshot = policy_snapshot(db)
        with scheduler.condition:
            if any(key in scheduler.groups and (scheduler.groups[key].active or scheduler.groups[key].waiting
                    or scheduler.groups[key].cooldown_until > time.monotonic()) for key in affected):
                raise QuotaPolicyError("原组或目标组仍有执行、排队请求或限流冷却，请等待完成后再迁移；没有取消现有请求", 409)
            db.commit()
            scheduler.apply_policy(snapshot)
        return public_snapshot(db)
    except BaseException:
        db.rollback()
        raise
