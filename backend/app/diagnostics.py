"""配置层诊断（codex doctor）：只读检查运行环境，分级输出 ok/warn/error。

检查范围：
- 运行模式与配置来源（config.json / .env）；
- LLM / Embedding 连通性（短超时，不阻塞）；
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

import httpx

from .config import (BACKEND_DIR, CONFIG_PATH, DB_PATH, DATA_DIR, VECTOR_PATH,
                     settings)

# 连通性检查的短超时，避免 doctor 长时间挂起
PROBE_TIMEOUT = 8.0
VALID_APPROVAL = ("read_only", "auto", "full_access")
VALID_EFFORT = ("", "minimal", "low", "medium", "high")


def _check(check_id: str, label: str, status: str, detail: str) -> dict:
    return {"id": check_id, "label": label, "status": status, "detail": detail}


# ---------- A. 运行模式与配置来源 ----------

def _check_mode() -> dict:
    detail = (f"LLM：{settings.llm_mode}（模型 {settings.llm_model}）；"
              f"Embedding：{settings.embedding_mode}；审批：{settings.approval_mode}；"
              f"推理强度：{settings.reasoning_effort or '默认'}")
    return _check("runtime_mode", "运行模式", "ok", detail)


def _check_config_sources() -> dict:
    cfg = CONFIG_PATH.exists()
    env = (BACKEND_DIR / ".env").exists()
    parts = [f"config.json：{'存在' if cfg else '无'}", f".env：{'存在' if env else '无'}"]
    status = "ok" if cfg or env else "warn"
    if not cfg and not env:
        parts.append("当前仅使用代码默认值，未持久化任何配置")
    return _check("config_sources", "配置来源", status, "；".join(parts))


# ---------- B. 连通性 ----------

def _check_llm() -> dict:
    if settings.llm_mode == "mock":
        return _check("llm", "大模型连通性", "ok",
                      "离线 Mock 模式：使用本地确定性应答，无需网络。")
    base = (settings.llm_base_url or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        return _check("llm", "大模型连通性", "error",
                      f"接口地址非法：{settings.llm_base_url!r}")
    url = f"{base}/chat/completions"
    body = {"model": settings.llm_model,
            "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code in (401, 403):
            return _check("llm", "大模型连通性", "error",
                          f"认证失败（HTTP {resp.status_code}）：API Key 无效或无权限。")
        if resp.status_code >= 400:
            return _check("llm", "大模型连通性", "error",
                          f"请求被拒（HTTP {resp.status_code}）：{resp.text[:160]}")
        return _check("llm", "大模型连通性", "ok",
                      f"接口可达，模型 {settings.llm_model} 正常响应。")
    except httpx.TimeoutException:
        return _check("llm", "大模型连通性", "error",
                      f"连接超时（{PROBE_TIMEOUT}s）：检查网络或接口地址 {base}")
    except httpx.HTTPError as exc:
        return _check("llm", "大模型连通性", "error",
                      f"无法连接：{type(exc).__name__}: {exc}")


def _check_embedding() -> dict:
    if settings.embedding_mode == "local":
        return _check("embedding", "向量化能力", "ok",
                      f"本地哈希向量（维度 {settings.vector_dim}），零依赖可用。")
    base = (settings.embedding_base_url or settings.llm_base_url or "").rstrip("/")
    url = f"{base}/embeddings"
    body = {"model": settings.embedding_model, "input": ["ping"]}
    key = settings.embedding_api_key or settings.llm_api_key
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            return _check("embedding", "向量化能力", "error",
                          f"HTTP {resp.status_code}：{resp.text[:160]}")
        dim = len(resp.json()["data"][0]["embedding"])
        return _check("embedding", "向量化能力", "ok",
                      f"Embedding 接口正常，返回向量维度 {dim}。")
    except httpx.HTTPError as exc:
        return _check("embedding", "向量化能力", "error",
                      f"无法连接：{type(exc).__name__}: {exc}")


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
            problems.append(f"{s['name']}：{s['error'] or s['status']}")
    if problems:
        return _check("mcp", "MCP 服务器", "warn",
                      f"已启用 {enabled_n} 个；异常：{'；'.join(problems)[:200]}")
    detail = "；".join(f"{s['name']}（{s['tool_count']} 工具）"
                       for s in servers if s["enabled"])
    return _check("mcp", "MCP 服务器", "ok",
                  f"已启用 {enabled_n} 个，全部正常：{detail[:200]}")


# ---------- E. 参数合法性 ----------

def _check_params() -> list[dict]:
    out: list[dict] = []
    if settings.approval_mode not in VALID_APPROVAL:
        out.append(_check("param_approval", "审批模式", "error",
                          f"非法取值 {settings.approval_mode!r}，应为 {VALID_APPROVAL}"))
    if settings.reasoning_effort not in VALID_EFFORT:
        out.append(_check("param_effort", "推理强度", "warn",
                          f"非标准取值 {settings.reasoning_effort!r}，部分服务可能忽略"))
    if settings.max_agent_steps < 1 or settings.max_agent_steps > 20:
        out.append(_check("param_steps", "推理步数", "warn",
                          f"max_agent_steps={settings.max_agent_steps} 超出建议范围 1–20"))
    if settings.retrieve_topk < 1 or settings.retrieve_topk > 20:
        out.append(_check("param_topk", "检索条数", "warn",
                          f"retrieve_topk={settings.retrieve_topk} 超出建议范围 1–20"))
    if not out:
        out.append(_check("params", "参数合法性", "ok",
                          "审批模式、推理强度、检索条数与推理步数均在合法范围内。"))
    return out


def run_diagnostics() -> dict:
    """运行全部检查并汇总，返回结构化诊断报告。"""
    checks = [
        _check_mode(),
        _check_config_sources(),
        _check_llm(),
        _check_embedding(),
        _check_database(),
        _check_data_dir(),
        _check_disk(),
        _check_vectors(),
        _check_mcp(),
        *_check_params(),
    ]
    summary = {
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "error": sum(1 for c in checks if c["status"] == "error"),
    }
    if summary["error"]:
        overall = "error"
    elif summary["warn"]:
        overall = "warn"
    else:
        overall = "ok"
    return {
        "overall": overall,
        "summary": summary,
        "checks": checks,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_dir": str(DATA_DIR),
    }
