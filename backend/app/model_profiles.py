"""Named model settings and secret-free, immutable-at-use run bindings."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .credential_protection import protect, unprotect
from .model_connection import normalize_base_url, normalize_effort, origin, single_line
from .model_readiness import MISSING_CLOUD_KEY, configured_api_mode
from .model_context_limits import context_limits, public_context_limits
from .model_capability_policy import CAPABILITIES, statuses, notices
from .models import Conversation, ConversationOptions, DeletedConversation, DesktopState, ModelCredential, ModelProfile
from .timeutil import utcnow

LEGACY_PROFILE_ID = "legacy-default"
DEMO_PROFILE_ID = "offline-demo"
DEFAULT_KEY = "model_profiles:default"
BOOTSTRAP_KEY = "model_profiles:installation-bootstrap"
_CID = re.compile(r"^[\w-]{1,64}$")


class ModelConfigurationError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def normalize_approval(value: str | None) -> str:
    mode = (value or "auto").strip().lower()
    if mode == "read_only":
        mode = "always"
    if mode not in ("always", "auto", "full_access"):
        raise ModelConfigurationError("审批方式无效，请重新选择", 422)
    return mode


def unknown_capabilities() -> dict:
    return {name: "unknown" for name in CAPABILITIES}


def profile_snapshot(profile: ModelProfile) -> dict:
    mode = configured_api_mode(profile.base_url, bool(profile.credential_ref))
    return {
        "id": profile.id, "name": profile.name, "base_url": profile.base_url,
        "model_id": profile.model_id, "reasoning_effort": profile.reasoning_effort,
        **public_context_limits({"context_window_tokens": profile.context_window_tokens,
                                "max_output_tokens": profile.max_output_tokens}),
        "enabled": bool(profile.enabled and not profile.deleted_at), "version": profile.version,
        "api_key_set": bool(profile.credential_ref),
        "capabilities": statuses(profile.capabilities),
        "protocol": "openai_chat", "legacy": False,
        "mode": mode, "configuration_error": MISSING_CLOUD_KEY if mode == "unconfigured" else "",
    }


def legacy_profile() -> dict:
    mode = settings.llm_mode
    return {
        "id": LEGACY_PROFILE_ID, "name": "离线演示模型" if mode == "mock" else "默认模型配置",
        "base_url": settings.llm_base_url, "model_id": settings.llm_model,
        "reasoning_effort": settings.reasoning_effort or "", "enabled": True, "version": 0,
        **public_context_limits(),
        "api_key_set": bool(settings.llm_api_key), "capabilities": unknown_capabilities(),
        "protocol": "openai_chat", "legacy": True,
        "mode": mode, "configuration_error": MISSING_CLOUD_KEY if mode == "unconfigured" else "",
    }


def demo_profile() -> dict:
    return {
        "id": DEMO_PROFILE_ID, "name": "离线演示（示例响应）", "base_url": "",
        "model_id": "offline-demo", "reasoning_effort": "", "enabled": True,
        **public_context_limits(),
        "version": 1, "api_key_set": False, "capabilities": unknown_capabilities(),
        "protocol": "demo", "legacy": False, "builtin": True,
        "mode": "mock", "configuration_error": "",
    }


def default_profile_id(db: Session) -> str:
    row = db.get(DesktopState, DEFAULT_KEY)
    return (row.value or {}).get("profile_id") or LEGACY_PROFILE_ID if row else LEGACY_PROFILE_ID


def initialize_installation_profile() -> None:
    """Create one editable default only for installs marked by this version.

    Existing installations and old marker files are not migrated by inference.
    Marker intent survives a failed DB transaction so the next boot can retry.
    """
    from . import config
    from .db import SessionLocal

    try:
        marker = json.loads((config.DATA_DIR / "installation-defaults.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(marker, dict) or marker.get("independent_profile_bootstrap") is not True:
        return
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if db.get(DesktopState, BOOTSTRAP_KEY):
            return
        existing = (db.get(DesktopState, DEFAULT_KEY) is not None
                    or db.query(ModelProfile.id).first() is not None
                    or db.query(ConversationOptions.conversation_id).first() is not None
                    or db.query(Conversation.id).first() is not None)
        profile_id = None
        if not existing and not config.settings.llm_demo_mode:
            config.require_available_credentials("llm_api_key")
            row = save_profile(db, {
                "name": config.settings.llm_model or "默认模型",
                "base_url": config.settings.llm_base_url,
                "model_id": config.settings.llm_model,
                "api_key": config.settings.llm_api_key,
                "reasoning_effort": config.settings.reasoning_effort,
            })
            profile_id = row.id
            set_default(db, row.id)
        db.add(DesktopState(key=BOOTSTRAP_KEY, revision=1,
                            value={"profile_id": profile_id, "preserved_existing": bool(existing),
                                   "explicit_demo": bool(config.settings.llm_demo_mode)}))
        db.commit()


def get_profile(db: Session, profile_id: str, *, usable: bool = False) -> ModelProfile:
    row = db.get(ModelProfile, profile_id)
    if row is None or row.deleted_at:
        raise ModelConfigurationError("所选模型配置已删除，请选择其他模型", 404)
    if usable and not row.enabled:
        raise ModelConfigurationError("所选模型已停用，请选择其他模型")
    return row


def public_profile(db: Session, profile_id: str, *, usable: bool = False) -> dict:
    if profile_id == DEMO_PROFILE_ID:
        return demo_profile()
    return legacy_profile() if profile_id == LEGACY_PROFILE_ID else profile_snapshot(get_profile(db, profile_id, usable=usable))


def set_default(db: Session, profile_id: str | None) -> None:
    profile_id = profile_id or LEGACY_PROFILE_ID
    public_profile(db, profile_id, usable=True)
    row = db.get(DesktopState, DEFAULT_KEY)
    if row is None:
        row = DesktopState(key=DEFAULT_KEY, value={"profile_id": profile_id}, revision=1)
        db.add(row)
    else:
        row.value = {"profile_id": profile_id}
        row.revision += 1
        row.updated_at = utcnow()


def _credential(db: Session, key: str, profile_id: str | None, *, legacy_generation: int = 0) -> str | None:
    if not key:
        return None
    row = ModelCredential(profile_id=profile_id, protected_value=protect(key),
                          legacy_generation=legacy_generation)
    db.add(row)
    db.flush()
    return row.id


def read_credential(db: Session, credential_ref: str | None) -> str:
    if not credential_ref:
        return ""
    row = db.get(ModelCredential, credential_ref)
    if row is None or row.revoked_at:
        raise ModelConfigurationError("该模型密钥已撤销或不可用，请重新配置")
    if row.profile_id is None:
        from . import config
        # Serialize resolution with an explicit clear. A request which already
        # resolved its key may be in flight; this fences future resolutions.
        with config._settings_lock:
            if row.legacy_generation != config.settings.legacy_credential_generation:
                raise ModelConfigurationError("原有默认模型密钥已清除，该任务的旧密钥已撤销，请重新配置")
            return unprotect(row.protected_value)
    return unprotect(row.protected_value)


def save_profile(db: Session, fields: dict, profile: ModelProfile | None = None) -> ModelProfile:
    creating = profile is None
    if creating:
        profile = ModelProfile(name="", base_url="", model_id="", capabilities=unknown_capabilities())
        db.add(profile)
        db.flush()
    name = single_line(fields.get("name", profile.name), "模型显示名称")
    model = single_line(fields.get("model_id", profile.model_id), "模型名称")
    base = normalize_base_url(fields.get("base_url", profile.base_url))
    effort = normalize_effort(fields.get("reasoning_effort", profile.reasoning_effort) or "")
    existing_limits = {key: getattr(profile, key) for key in ("context_window_tokens", "max_output_tokens")}
    limit_values = {key: fields.get(key, value) for key, value in existing_limits.items()}
    if any(key in fields and fields[key] is None for key in existing_limits):
        raise ModelConfigurationError("模型 token 限制不能为null", 422)
    limits = context_limits(limit_values)
    if not name or not base or not model:
        raise ModelConfigurationError("请填写模型显示名称、接口地址和模型ID", 422)
    supplied_key = fields.get("api_key")
    if supplied_key is not None:
        supplied_key = single_line(supplied_key, "API Key")
    if (profile.credential_ref and origin(profile.base_url) != origin(base) and supplied_key is None):
        raise ModelConfigurationError("模型接口来源已更换，请重新填写密钥或明确留空保存", 422)
    changed = ((base, model, effort) != (profile.base_url, profile.model_id, profile.reasoning_effort)
               or limits != context_limits(existing_limits))
    if supplied_key is not None:
        profile.credential_ref = _credential(db, supplied_key, profile.id)
        changed = True
    profile.name, profile.base_url, profile.model_id, profile.reasoning_effort = name, base, model, effort
    for key, value in limits.items():
        if creating or key in fields:
            setattr(profile, key, value)
    if "enabled" in fields:
        profile.enabled = int(fields["enabled"])
    if changed or not creating:
        profile.capabilities = unknown_capabilities()
    profile.updated_at = utcnow()
    if not creating:
        profile.version += 1
    db.flush()
    return profile


def require_conversation(db: Session, conversation_id: str, *, write: bool = False) -> None:
    if not isinstance(conversation_id, str) or not _CID.fullmatch(conversation_id):
        raise ModelConfigurationError("会话编号无效", 422)
    if db.get(DeletedConversation, conversation_id):
        raise ModelConfigurationError("该会话已删除，请新建会话", 410)
    row = db.get(Conversation, conversation_id)
    if write and row and row.archived_at:
        raise ModelConfigurationError("该会话已归档，请先恢复会话", 409)


def options_snapshot(db: Session, conversation_id: str) -> dict:
    require_conversation(db, conversation_id)
    row = db.get(ConversationOptions, conversation_id)
    selected = row.model_profile_id if row else default_profile_id(db)
    selected = selected or default_profile_id(db)
    result = {
        "revision": row.revision if row else 0,
        "model_profile_id": selected,
        "approval_mode": normalize_approval(row.approval_mode if row else settings.approval_mode),
        "reasoning_effort": row.reasoning_effort if row else None,
        "effective_model": None, "model_error": "",
    }
    try:
        result["effective_model"] = public_profile(db, selected, usable=True)
        result["model_error"] = result["effective_model"].get("configuration_error", "")
        if row and row.reasoning_effort is not None:
            result["effective_model"]["reasoning_effort"] = row.reasoning_effort
    except ModelConfigurationError as exc:
        result["model_error"] = str(exc)
    return result


def update_options(db: Session, conversation_id: str, fields: dict, expected_revision: int) -> dict:
    require_conversation(db, conversation_id, write=True)
    row = db.get(ConversationOptions, conversation_id)
    if expected_revision != (row.revision if row else 0):
        raise ModelConfigurationError("会话设置已在其他窗口改变，请刷新后重试")
    if row is None:
        row = ConversationOptions(conversation_id=conversation_id, revision=0,
                                  model_profile_id=default_profile_id(db),
                                  approval_mode=normalize_approval(settings.approval_mode))
        db.add(row)
    if "model_profile_id" in fields:
        selection = fields["model_profile_id"] or default_profile_id(db)
        public_profile(db, selection, usable=True)
        row.model_profile_id = selection
        # A previous model's unsupported reasoning preference must not leak.
        if "reasoning_effort" not in fields:
            row.reasoning_effort = None
    if "approval_mode" in fields:
        row.approval_mode = normalize_approval(fields["approval_mode"])
    if "reasoning_effort" in fields:
        row.reasoning_effort = (normalize_effort(fields["reasoning_effort"])
                                if fields["reasoning_effort"] is not None else None)
    row.revision += 1
    row.updated_at = utcnow()
    db.flush()
    return options_snapshot(db, conversation_id)


def current_policy(db: Session, conversation_id: str | None, fallback: dict | str | None = None) -> dict:
    if conversation_id:
        row = db.query(ConversationOptions).populate_existing().filter_by(conversation_id=conversation_id).first()
        if row:
            return {"approval_mode": normalize_approval(row.approval_mode), "policy_revision": row.revision}
    fallback = fallback if isinstance(fallback, dict) else {"approval_mode": fallback}
    return {"approval_mode": normalize_approval(fallback.get("approval_mode") or settings.approval_mode),
            "policy_revision": fallback.get("policy_revision", 0)}


def capture_run_config(db: Session, conversation_id: str | None, *, selected_profile_id: str | None = None) -> dict:
    if conversation_id is not None and selected_profile_id is not None:
        raise ModelConfigurationError("会话运行必须使用该会话自己的模型选择")
    options = options_snapshot(db, conversation_id) if conversation_id else {
        "model_profile_id": selected_profile_id if selected_profile_id is not None else default_profile_id(db),
        "approval_mode": normalize_approval(settings.approval_mode),
        "reasoning_effort": None, "revision": 0,
    }
    profile_id = options["model_profile_id"]
    selected_profile = public_profile(db, profile_id, usable=True)
    if selected_profile.get("configuration_error"):
        raise ModelConfigurationError(selected_profile["configuration_error"])
    # Freeze a new conversation's inherited selection before the default moves.
    if conversation_id and not db.get(ConversationOptions, conversation_id):
        row = ConversationOptions(conversation_id=conversation_id, revision=1,
                                  model_profile_id=profile_id, approval_mode=options["approval_mode"])
        db.add(row)
        db.flush()
        options["revision"] = 1
    if profile_id == DEMO_PROFILE_ID:
        name, base, model, version, effort = (selected_profile["name"], "", "offline-demo", 1, "")
        credential_ref, mode = None, "mock"
    elif profile_id == LEGACY_PROFILE_ID:
        from . import config
        # A key and its generation must be captured together: a concurrent
        # clear must never stamp an old key with the newly authorized epoch.
        with config._settings_lock:
            config.require_available_credentials("llm_api_key")
            legacy_key = settings.llm_api_key
            legacy_generation = config.settings.legacy_credential_generation
            mode = settings.llm_mode
            if mode == "unconfigured":
                raise ModelConfigurationError(MISSING_CLOUD_KEY)
            name, base, model, version, effort = (legacy_profile()["name"], settings.llm_base_url,
                                                settings.llm_model, 0, settings.reasoning_effort or "")
        # Do not hold the settings lock while acquiring SQLite's write lock.
        # If a clear overtakes persistence, this captured old generation still
        # fails the next resolution rather than reviving the old key.
        credential_ref = _credential(db, legacy_key, None, legacy_generation=legacy_generation)
    else:
        profile = get_profile(db, profile_id, usable=True)
        name, base, model, version, effort = (profile.name, profile.base_url, profile.model_id,
                                            profile.version, profile.reasoning_effort)
        credential_ref, mode = profile.credential_ref, "api"
        read_credential(db, credential_ref)  # Fail before accepting work if revoked.
    from .resource_policy import snapshot_policy
    from .execution_environments import snapshot_policy as snapshot_execution_policy
    from .history_revisions import pending_policy
    revision_policy = pending_policy(db, conversation_id)
    from .account_quotas import capture_binding, credential_hash
    quota_binding = capture_binding(db, base, credential_hash(read_credential(db, credential_ref))) if mode == "api" else None
    capability_state = db.get(DesktopState, f"model-capabilities:{profile_id}")
    capability_observations = {}
    for feature, observation in ((capability_state.value or {}).get("results", {}) if capability_state else {}).items():
        if (observation.get("profile_version") == version and observation.get("model_id") == model
                and observation.get("endpoint") == base + "/chat/completions"):
            capability_observations[feature] = {key: observation.get(key) for key in
                ("diagnostic_id", "status", "tested_at", "profile_version", "model_id", "endpoint", "protocol", "failure_kind")}
    return {
        **({"quota_binding": quota_binding} if quota_binding else {}),
        **({"history_revision": revision_policy} if revision_policy else {}),
        "resource_policy": snapshot_policy(db, conversation_id),
        "execution_environment": snapshot_execution_policy(db,conversation_id),
        "version": 1, "profile_id": profile_id, "profile_version": version, "profile_name": name,
        "base_url": base, "model_id": model,
        "capabilities": deepcopy(selected_profile["capabilities"]),
        "capability_notices": notices(selected_profile),
        "capability_observations": capability_observations,
        **{key: selected_profile[key] for key in ("context_window_tokens", "max_output_tokens",
                                                 "context_limits_source", "context_limits_verified")},
        "reasoning_effort": options["reasoning_effort"] if options["reasoning_effort"] is not None else effort,
        "credential_ref": credential_ref, "mode": mode,
        "approval_mode": options["approval_mode"], "policy_revision": options["revision"],
    }


def resolve_model_config(snapshot: dict, db: Session | None = None) -> dict:
    from .db import SessionLocal
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ModelConfigurationError("此任务的模型配置版本不兼容，请重新选择模型")
    if snapshot.get("mode") not in ("mock", "api"):
        raise ModelConfigurationError("任务模型模式无效")
    if db is not None:
        key = read_credential(db, snapshot.get("credential_ref"))
    else:
        with SessionLocal() as session:
            key = read_credential(session, snapshot.get("credential_ref"))
    return {
        "base_url": snapshot.get("base_url", ""), "model_id": snapshot.get("model_id", ""),
        "api_key": key, "reasoning_effort": snapshot.get("reasoning_effort", ""),
        "mode": snapshot["mode"],
        **context_limits(snapshot),
    }


def public_runtime_config(snapshot: dict | None) -> dict | None:
    """Allowlisted display metadata; no internal credential identifiers."""
    if not snapshot:
        return None
    return {**{key: deepcopy(snapshot.get(key)) for key in (
        "version", "profile_id", "profile_version", "profile_name", "base_url", "model_id",
        "reasoning_effort", "mode", "approval_mode", "policy_revision", "execution_environment",
        "context_window_tokens", "max_output_tokens", "context_limits_source", "context_limits_verified",
        "capabilities", "capability_notices", "capability_observations",
    )}, **public_context_limits(snapshot)}
