"""全局配置：云模型缺凭证保持待配置；离线演示必须明确选择。

配置优先级（低 → 高）：
1. 代码默认值
2. 环境变量 / .env
3. 数据目录下的 config.json（由应用内「模型与设置」页写入，可热更新）
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator

from .model_connection import normalize_setting, require_key_for_origin_change
from .atomic_files import atomic_write_json
from .credential_protection import CredentialProtectionError, protect, unprotect
from .model_readiness import configured_api_mode

APP_NAME = "ScholarFlow"
BACKEND_DIR = Path(__file__).resolve().parent.parent

# 可在应用内修改并持久化的字段（字符串型）
RUNTIME_FIELDS = (
    "llm_api_key",
    "llm_base_url",
    "llm_model",
    "embedding_api_key",
    "embedding_base_url",
    "embedding_model",
    "approval_mode",
    "work_rules",
    "reasoning_effort",
)

# 可在应用内修改并持久化的字段（布尔型）
RUNTIME_BOOL_FIELDS = (
    "llm_demo_mode",
    "desktop_notifications",
    "memory_recall",
    "memory_capture",
)

# 可在应用内修改并持久化的字段（浮点型）
RUNTIME_FLOAT_FIELDS = (
    "min_relevance_score",
)

RUNTIME_INT_FIELDS = ("max_agent_steps",)


def validate_agent_steps(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 128:
        raise ValueError("单次任务最多执行轮数必须是 1–128 之间的整数")
    return value


def _default_data_dir() -> str:
    if getattr(sys, "frozen", False):
        # 打包后：数据放 %APPDATA%/ScholarFlow
        base = os.environ.get("APPDATA") or str(Path.home())
        return str(Path(base) / APP_NAME)
    return str(BACKEND_DIR / "data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 大模型（OpenAI 兼容接口，httpx 直连）----
    llm_api_key: str = ""
    # Explicit opt-in only; missing credentials must never enable the demo.
    llm_demo_mode: bool = False
    # Internal revocation epoch for frozen legacy-default credentials. It is
    # stored beside the key, never accepted from the public settings form.
    legacy_credential_generation: int = Field(default=0, ge=0)
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-flash"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "text-embedding-v3"

    # ---- 基础设施 ----
    redis_url: str = ""
    data_dir: str = ""
    db_name: str = "scholarflow.db"
    vector_name: str = "vectors.npy"

    # ---- Agent / 检索参数 ----
    max_agent_steps: int = Field(default=24, ge=1, le=128)
    retrieve_topk: int = 6
    vector_dim: int = 512
    # 最低相关性阈值（查询向量与证据块向量的余弦相似度）：低于该值的候选视为
    # 不相关并被过滤，全部低于阈值则返回空证据，由 LLM 走抗幻觉拒答。
    # 离线哈希词袋下余弦普遍偏低，默认 0.1 保守拦截（零误伤）；接入在线语义
    # 模型后可在设置中调高（如 0.25–0.3），拒答更准确。
    min_relevance_score: float = 0.1
    # 审批模式：read_only（外联/写入均需确认）/ auto（默认，可本会话授权）/ full_access（全部自动，自担风险）
    approval_mode: str = "auto"
    # 我的工作规范（AGENTS.md）：课题组写作/引用/语言规范，持久注入每个任务
    work_rules: str = ""
    # 推理强度：""（不传，用模型默认）/ minimal / low / medium / high（OpenAI 兼容 reasoning_effort）
    reasoning_effort: str = ""

    # ---- 桌面通知：后台任务完成/失败/需审批时弹 Windows toast ----
    desktop_notifications: bool = True
    # ---- 记忆开关：recall=是否载入历史上下文；capture=是否让本轮进入未来记忆 ----
    memory_recall: bool = True
    memory_capture: bool = True

    @field_validator("llm_api_key", "llm_base_url", "llm_model", "embedding_api_key",
                     "embedding_base_url", "embedding_model", "reasoning_effort", "approval_mode")
    @classmethod
    def normalize_connection(cls, value: str, info) -> str:
        return normalize_setting(info.field_name, value)

    @property
    def llm_mode(self) -> str:
        """api、显式 mock 或 unconfigured；不是连通性测试结果。"""
        if self.llm_demo_mode:
            return "mock"
        return configured_api_mode(self.llm_base_url, bool(self.llm_api_key))

    @property
    def embedding_mode(self) -> str:
        """api 或 local（确定性哈希向量）。"""
        return "api" if self.embedding_api_key and self.embedding_model else "local"


settings = Settings()
if not settings.data_dir:
    settings.data_dir = _default_data_dir()

DATA_DIR = Path(settings.data_dir)
# Restore before configuration files, vector indexes, or database engines are
# opened. The runtime lock rejects a second process while the app is alive.
from .data_backup import apply_pending_restore
apply_pending_restore(DATA_DIR, expected_database=settings.db_name)
PDF_DIR = DATA_DIR / "pdf"
PARSED_DIR = DATA_DIR / "parsed"
CONFIG_PATH = DATA_DIR / "config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
PARSED_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / settings.db_name
VECTOR_PATH = DATA_DIR / settings.vector_name


def initialize_installation_defaults(instance: Settings, data_dir: Path, config_path: Path,
                                     database_path: Path) -> str:
    """Only an empty installation adopts the new defaults; no key/profile is edited.

    A non-secret marker distinguishes a new install on its second launch from
    an older install which used implicit defaults without saving a config.
    Explicit environment fields are always left to the normal precedence rules.
    """
    marker = data_dir / "installation-defaults.json"
    if marker.exists():
        return "current"
    if config_path.exists() or database_path.exists():
        legacy = {"llm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "llm_model": "qwen-flash"}
        for name, value in legacy.items():
            if name not in instance.model_fields_set:
                setattr(instance, name, value)
        return "legacy"
    atomic_write_json(marker, {"schema_version": 1, "default_generation": "deepseek-flash",
                               "independent_profile_bootstrap": True})
    return "new"


initialize_installation_defaults(settings, DATA_DIR, CONFIG_PATH, DB_PATH)

SECRET_FIELDS = ("llm_api_key", "embedding_api_key")
_settings_lock = threading.RLock()
_credential_errors: dict[str, str] = {}
_protected_credentials: dict[str, str] = {}
_credential_config_path: str | None = None


def credential_status() -> dict[str, str]:
    """Safe persistent-config warnings; neither ciphertext nor secrets are public."""
    return dict(_credential_errors) if _credential_config_path == str(CONFIG_PATH.resolve()) else {}


def require_available_credentials(*fields: str) -> None:
    errors = credential_status()
    if any(field in errors for field in fields):
        raise CredentialProtectionError("保存的密钥在当前Windows用户下不可用，请重新授权；本次未切换为演示模型")


def _storage_payload(values: dict, updates: dict | None = None) -> dict:
    """Protect and roundtrip-check every new credential before replacing the file."""
    result = dict(values)
    protected = {}
    updates = updates or {}
    same_path = _credential_config_path == str(CONFIG_PATH.resolve())
    for field in SECRET_FIELDS:
        value = result.get(field) or ""
        if same_path and field in _credential_errors and field not in updates:
            # A credential belonging to another OS user must not be erased by
            # changing an unrelated preference. Only an explicit key decision
            # may replace/clear that inaccessible value.
            protected[field] = _protected_credentials[field]
        elif value:
            encoded = protect(value)
            if unprotect(encoded) != value:
                raise CredentialProtectionError("系统凭证校验失败，原配置已保留")
            protected[field] = encoded
        result[field] = ""  # An explicit clear survives environment-value reload.
    result["protected_credentials"] = protected
    result["config_schema_version"] = 2
    return result


def _write_config(values: dict, updates: dict | None = None) -> dict:
    stored = _storage_payload(values, updates)
    try:
        atomic_write_json(CONFIG_PATH, stored)
    except OSError as exc:
        raise ValueError("配置未能安全保存，原配置和当前设置已保留，请检查磁盘空间与写入权限") from exc
    return stored


def _load_config_file() -> None:
    """启动时用 config.json 覆盖默认值/环境值。"""
    global _credential_config_path, _protected_credentials, _credential_errors
    if not CONFIG_PATH.exists():
        return
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    generation = data.get("legacy_credential_generation", 0)
    if type(generation) is int and generation >= 0:
        settings.legacy_credential_generation = generation
    _credential_config_path = str(CONFIG_PATH.resolve())
    _credential_errors = {}
    _protected_credentials = {}
    protected = data.get("protected_credentials")
    if isinstance(protected, dict):
        for key in SECRET_FIELDS:
            encoded = protected.get(key)
            if not isinstance(encoded, str) or not encoded:
                continue
            _protected_credentials[key] = encoded
            try:
                data[key] = unprotect(encoded)
            except CredentialProtectionError:
                data[key] = ""
                _credential_errors[key] = "系统保护的密钥无法读取，请在当前Windows用户下重新授权或填写密钥"
    legacy_secrets = any(isinstance(data.get(key), str) and data[key]
                         and key not in _protected_credentials for key in SECRET_FIELDS)
    if legacy_secrets:
        try:
            stored = _storage_payload(data)
            # Recovery contains only user-bound ciphertext, including legacy
            # unknown fields. Never create an additional plaintext .bak file.
            original = json.dumps(data, ensure_ascii=False)
            recovery = protect(original)
            if unprotect(recovery) != original:
                raise CredentialProtectionError("凭证迁移恢复副本校验失败")
            atomic_write_json(CONFIG_PATH.with_suffix(".credential-recovery.json"), {
                "schema_version": 1, "protected_config": recovery,
            })
            atomic_write_json(CONFIG_PATH, stored)
            _protected_credentials = stored["protected_credentials"]
        except (OSError, CredentialProtectionError):
            # Keep the original file and usable in-memory legacy configuration
            # for recovery; subsequent writes still require protected storage.
            _credential_errors["migration"] = "旧版密钥尚未完成系统保护迁移，原配置已保留；请检查磁盘权限后重新保存"
    for key in RUNTIME_FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            try:
                value = normalize_setting(key, value)
            except ValueError:
                # Keep an older invalid value editable in Settings instead of
                # preventing the desktop application from starting. Requests
                # still validate it before transmitting any credentials.
                pass
            setattr(settings, key, value)
    for key in RUNTIME_BOOL_FIELDS:
        if isinstance(data.get(key), bool):
            setattr(settings, key, data[key])
    for key in RUNTIME_FLOAT_FIELDS:
        if isinstance(data.get(key), (int, float)):
            setattr(settings, key, float(data[key]))
    for key in RUNTIME_INT_FIELDS:
        if key in data:
            try:
                setattr(settings, key, validate_agent_steps(data[key]))
            except ValueError:
                pass  # Keep a usable default when an old configuration is invalid.


_load_config_file()


def runtime_settings() -> dict:
    """返回当前运行时配置（API Key 脱敏）。"""
    result = {key: getattr(settings, key) for key in RUNTIME_FIELDS}
    for key in RUNTIME_BOOL_FIELDS:
        result[key] = bool(getattr(settings, key))
    for key in RUNTIME_FLOAT_FIELDS:
        result[key] = float(getattr(settings, key))
    for key in RUNTIME_INT_FIELDS:
        result[key] = getattr(settings, key)
    for field in ("llm_api_key", "embedding_api_key"):
        key = result.get(field) or ""
        result[f"{field}_set"] = bool(key) or field in credential_status()
        if key:
            result[field] = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
    result["llm_mode"] = settings.llm_mode
    result["embedding_mode"] = settings.embedding_mode
    result["credential_errors"] = credential_status()
    return result


def update_settings(updates: dict) -> dict:
    with _settings_lock:
        return _update_settings(updates)


def _update_settings(updates: dict) -> dict:
    """更新运行时字段并持久化到 config.json，随后热切换 LLM/Embedding 客户端。"""
    persisted = {key: getattr(settings, key) for key in RUNTIME_FIELDS}
    for key in RUNTIME_FIELDS:
        if key in updates and isinstance(updates[key], str):
            persisted[key] = normalize_setting(key, updates[key])
    # Validate the whole update before changing either runtime state or its file.
    errors = credential_status()
    require_key_for_origin_change(
        settings.llm_base_url, persisted["llm_base_url"], settings.llm_api_key or ("unavailable" if "llm_api_key" in errors else ""),
        updates.get("llm_api_key"), "大模型")
    require_key_for_origin_change(
        settings.embedding_base_url or settings.llm_base_url,
        persisted["embedding_base_url"] or persisted["llm_base_url"], settings.embedding_api_key or ("unavailable" if "embedding_api_key" in errors else ""),
        updates.get("embedding_api_key"), "Embedding")
    for key in RUNTIME_BOOL_FIELDS:
        persisted[key] = bool(getattr(settings, key))
        if key in updates and isinstance(updates[key], bool):
            persisted[key] = updates[key]
    for key in RUNTIME_FLOAT_FIELDS:
        persisted[key] = float(getattr(settings, key))
        if key in updates and isinstance(updates[key], (int, float)):
            persisted[key] = float(updates[key])
    for key in RUNTIME_INT_FIELDS:
        persisted[key] = validate_agent_steps(updates.get(key, getattr(settings, key)))
    # Clearing is an explicit revocation decision. Commit the empty key and
    # its next epoch in the same atomic file replacement, before mutating any
    # runtime values. Normal nonempty key edits retain admitted run bindings.
    clears_legacy_key = (isinstance(updates.get("llm_api_key"), str)
                         and persisted["llm_api_key"] == "")
    persisted["legacy_credential_generation"] = (
        settings.legacy_credential_generation + int(clears_legacy_key))
    global _credential_config_path, _protected_credentials, _credential_errors
    stored = _write_config(persisted, updates)
    _credential_config_path = str(CONFIG_PATH.resolve())
    _protected_credentials = stored["protected_credentials"]
    _credential_errors = {key: value for key, value in _credential_errors.items()
                          if key in _protected_credentials and key not in updates}
    for key, value in persisted.items():
        setattr(settings, key, value)

    # 延迟导入避免循环依赖
    from .llm import reset_llm
    from .pipeline.embeddings import reset_embedder

    reset_llm()
    reset_embedder()
    return runtime_settings()
