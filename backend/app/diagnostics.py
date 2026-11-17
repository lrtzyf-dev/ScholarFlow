"""配置层诊断（codex doctor）：只读检查运行环境，分级输出 ok/warn/error。

检查范围：
- 运行模式与配置来源（config.json / .env）；
- LLM / Embedding 本地配置（不发起接口请求）；
- 数据库完整性、数据目录可写性、磁盘空间、向量库；
- MCP 服务器连接状态；
- 关键参数合法性与配置冲突。

任何单项检查的异常都被隔离，不会让整个诊断崩溃。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime


from .config import (BACKEND_DIR, CONFIG_PATH, DB_PATH, DATA_DIR, VECTOR_PATH,
                     settings)
from .model_connection import normalize_effort

# 连通性检查的短超时，避免 doctor 长时间挂起
PROBE_TIMEOUT = 8.0
VALID_APPROVAL = ("always", "read_only", "auto", "full_access")


def _check(check_id: str, label: str, status: str, detail: str) -> dict:
    return {"id": check_id, "label": label, "status": status, "detail": detail}


# ---------- A. 运行模式与配置来源 ----------

def _check_mode() -> dict:
    detail = (f"Embedding：{settings.embedding_mode}；应用默认审批：{settings.approval_mode}。"
              "实际任务使用该会话绑定的模型和权限。")
    return _check("runtime_mode", "运行模式", "ok", detail)


def _check_config_sources() -> dict:
    from .config import credential_status
    cfg = CONFIG_PATH.exists()
    env = (BACKEND_DIR / ".env").exists()
    parts = [f"config.json：{'存在' if cfg else '无'}", f".env：{'存在' if env else '无'}"]
    status = "ok" if cfg or env else "warn"
    if not cfg and not env:
        parts.append("未发现旧配置文件；独立模型档案另存于本地数据库")
    warnings = credential_status()
    if warnings:
        status = "error" if any(key != "migration" for key in warnings) else "warn"
        parts.extend(warnings.values())
    return _check("config_sources", "配置来源", status, "；".join(parts))


# ---------- B. 连通性 ----------

def _check_llm() -> dict:
    mode = settings.llm_mode
    return _check("llm", "模型配置（本地）", "warn" if mode == "unconfigured" else "ok",
                  "模型待配置，未发起接口请求。" if mode == "unconfigured" else
                  "已读取本地模型配置；接口连通性尚未测试。")


def _check_embedding() -> dict:
    return _check("embedding", "向量化配置（本地）", "ok",
                  "本地哈希向量；本次检查无外部调用。" if settings.embedding_mode == "local" else
                  "已配置远端 Embedding；本次检查未发起接口请求。")


# ---------- C. 存储 ----------

def _check_database() -> dict:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for tbl in ("papers", "chunks", "qa_records", "conversations", "tasks"):
            counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        conn.close()
        if integrity != "ok":
            return _check("database", "数据库", "error",
                          f"完整性检查异常：{integrity}")
        return _check("database", "数据库", "ok",
                      f"完整性 ok；论文 {counts['papers']}、分块 {counts['chunks']}、"
                      f"问答 {counts['qa_records']}、会话 {counts['conversations']}。")
    except sqlite3.Error as exc:
        return _check("database", "数据库", "error", f"无法打开数据库：{exc}")


def _check_data_dir() -> dict:
    try:
        with tempfile.NamedTemporaryFile(dir=str(DATA_DIR), delete=True):
            pass
        return _check("data_dir", "数据目录可写性", "ok", f"目录可写：{DATA_DIR}")
    except OSError as exc:
        return _check("data_dir", "数据目录可写性", "error", f"目录不可写：{exc}")


def _check_disk() -> dict:
    usage = shutil.disk_usage(str(DATA_DIR))
    free_gb = usage.free / (1024 ** 3)
    if free_gb < 0.2:
        return _check("disk", "磁盘剩余空间", "error", f"仅剩 {free_gb:.2f} GB，空间严重不足。")
    if free_gb < 1.0:
        return _check("disk", "磁盘剩余空间", "warn", f"仅剩 {free_gb:.2f} GB，建议清理。")
    return _check("disk", "磁盘剩余空间", "ok", f"剩余 {free_gb:.1f} GB（总容量 {usage.total/1024**3:.0f} GB）。")


def _check_vectors() -> dict:
    from .pipeline.vectordb import NumpyVectorStore

    store = NumpyVectorStore()
    matrix_path = store._matrix_file()
    if store.matrix is None or not matrix_path.exists():
        return _check("vectors", "本地向量库", "warn",
                      "向量库尚未生成或无法从磁盘加载，首次建库后出现；离线检索依赖该文件。")
    return _check("vectors", "本地向量库", "ok",
                  f"向量库可正常加载（{store.matrix.shape[0]} 条向量、"
                  f"维度 {store.matrix.shape[1]}，文件 {matrix_path.name}）。")


# ---------- D. MCP ----------

def _check_mcp() -> dict:
    from .mcp_bridge import bridge

    servers = bridge.list_servers()
    if not servers:
        return _check("mcp", "MCP 服务器", "ok", "未配置任何 MCP 服务器。")
    problems = []
    enabled_n = 0
    for s in servers:
        if not s["enabled"]:
            continue
        enabled_n += 1
        if s["error"] or s["status"] in ("error", "failed"):
            problems.append(f"{s['name']}：连接异常，请检查该服务器配置")
        elif s["status"] not in ("connected", "ready"):
            problems.append(f"{s['name']}：连接中" if s["status"] in ("connecting", "starting") else
                            f"{s['name']}：当前未连接")
    if problems:
        return _check("mcp", "MCP 服务器", "warn",
                      f"已启用 {enabled_n} 个；异常：{'；'.join(problems)[:200]}")
    detail = "；".join(f"{s['name']}（{s['tool_count']} 工具）"
                       for s in servers if s["enabled"])
    return _check("mcp", "MCP 服务器", "ok",
                  f"已启用 {enabled_n} 个；本地连接状态：{detail[:200]}（未主动探测远端）")


# ---------- E. 参数合法性 ----------

def _check_params() -> list[dict]:
    out: list[dict] = []
    if settings.approval_mode not in VALID_APPROVAL:
        out.append(_check("param_approval", "审批模式", "error",
                          f"非法取值 {settings.approval_mode!r}，应为 {VALID_APPROVAL}"))
    try:
        normalize_effort(settings.reasoning_effort)
    except ValueError:
        out.append(_check("param_effort", "推理强度", "warn",
                          f"非标准取值 {settings.reasoning_effort!r}，部分服务可能忽略"))
    if settings.max_agent_steps < 1 or settings.max_agent_steps > 128:
        out.append(_check("param_steps", "推理步数", "warn",
                          f"max_agent_steps={settings.max_agent_steps} 超出合法范围 1–128"))
    if settings.retrieve_topk < 1 or settings.retrieve_topk > 20:
        out.append(_check("param_topk", "检索条数", "warn",
                          f"retrieve_topk={settings.retrieve_topk} 超出建议范围 1–20"))
    if not out:
        out.append(_check("params", "参数合法性", "ok",
                          "审批模式、推理强度、检索条数与推理步数均在合法范围内。"))
    return out


def run_diagnostics(conversation_id=None) -> dict:
    """Local-only inspection; one failing check cannot abort the report."""
    checks = []
    functions = (_check_mode, _check_config_sources, _check_llm, _check_embedding,
                 _check_database, _check_data_dir, _check_disk, _check_vectors, _check_mcp, _check_params)
    identifiers = ("runtime_mode", "config_sources", "llm", "embedding", "database",
                   "data_dir", "disk", "vectors", "mcp", "params")
    for check_id, function in zip(identifiers, functions):
        try:
            value = function()
            checks.extend(value if isinstance(value, list) else [value])
        except Exception:
            checks.append(_check(check_id, "本地检查失败", "error",
                                 "此项暂时无法检查；其余检查已继续。请查看存储、配置或运行状态。"))
    from .diagnostic_probes import probe_targets
    targets = probe_targets(conversation_id)
    selected = next((target for target in targets if target["service"] == "llm"), None)
    if selected:
        for index, check in enumerate(checks):
            if check["id"] == "llm":
                checks[index] = _check("llm", "当前会话模型（本地）" if conversation_id else "默认模型（本地）",
                                       "warn" if selected["mode"] in {"unconfigured", "unavailable"} else "ok",
                                       selected["detail"] if not selected["available"] else
                                       f"模型 {selected['model_id']}；连通性尚未测试。")
    summary = {status: sum(1 for check in checks if check["status"] == status)
               for status in ("ok", "warn", "error")}
    return {"overall": "error" if summary["error"] else "warn" if summary["warn"] else "ok",
            "summary": summary, "checks": checks,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_dir": str(DATA_DIR), "scope": "local", "remote_requests": 0,
            "conversation_id": conversation_id, "probe_targets": targets}
