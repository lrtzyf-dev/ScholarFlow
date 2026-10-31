"""全局配置：未配置 API Key 时自动降级为离线 Mock LLM + 本地向量。

配置优先级（低 → 高）：
1. 代码默认值
2. 环境变量 / .env
3. 数据目录下的 config.json（由应用内「模型与设置」页写入，可热更新）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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
    "desktop_notifications",
    "memory_recall",
    "memory_capture",
)


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
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-flash"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "text-embedding-v3"

    # ---- 基础设施 ----
    redis_url: str = ""
    data_dir: str = ""
    db_name: str = "scholarflow.db"
    vector_name: str = "vectors.npy"

    # ---- Agent / 检索参数 ----
    max_agent_steps: int = 6
    retrieve_topk: int = 6
    vector_dim: int = 512
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

    @property
    def llm_mode(self) -> str:
        """api 或 mock。"""
        return "api" if self.llm_api_key else "mock"

    @property
    def embedding_mode(self) -> str:
        """api 或 local（确定性哈希向量）。"""
        return "api" if self.embedding_api_key and self.embedding_model else "local"


settings = Settings()
if not settings.data_dir:
    settings.data_dir = _default_data_dir()

DATA_DIR = Path(settings.data_dir)
PDF_DIR = DATA_DIR / "pdf"
PARSED_DIR = DATA_DIR / "parsed"
CONFIG_PATH = DATA_DIR / "config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
PARSED_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / settings.db_name
VECTOR_PATH = DATA_DIR / settings.vector_name


def _load_config_file() -> None:
    """启动时用 config.json 覆盖默认值/环境值。"""
    if not CONFIG_PATH.exists():
        return
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key in RUNTIME_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value:
            setattr(settings, key, value)
    for key in RUNTIME_BOOL_FIELDS:
        if isinstance(data.get(key), bool):
            setattr(settings, key, data[key])


_load_config_file()


def runtime_settings() -> dict:
    """返回当前运行时配置（API Key 脱敏）。"""
    result = {key: getattr(settings, key) for key in RUNTIME_FIELDS}
    for key in RUNTIME_BOOL_FIELDS:
        result[key] = bool(getattr(settings, key))
    key = result.get("llm_api_key") or ""
    result["llm_api_key_set"] = bool(key)
    if key:
        result["llm_api_key"] = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
    result["llm_mode"] = settings.llm_mode
    result["embedding_mode"] = settings.embedding_mode
    return result


def update_settings(updates: dict) -> dict:
    """更新运行时字段并持久化到 config.json，随后热切换 LLM/Embedding 客户端。"""
    for key in RUNTIME_FIELDS:
        if key in updates and isinstance(updates[key], str):
            setattr(settings, key, updates[key].strip())
    for key in RUNTIME_BOOL_FIELDS:
        if key in updates and isinstance(updates[key], bool):
            setattr(settings, key, bool(updates[key]))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    persisted = {key: getattr(settings, key) for key in RUNTIME_FIELDS}
    for key in RUNTIME_BOOL_FIELDS:
        persisted[key] = bool(getattr(settings, key))
    CONFIG_PATH.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")

    # 延迟导入避免循环依赖
    from .llm import reset_llm
    from .pipeline.embeddings import reset_embedder

    reset_llm()
    reset_embedder()
    return runtime_settings()
