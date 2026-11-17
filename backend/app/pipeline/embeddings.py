"""
向量化：
- local：确定性哈希 n-gram 向量（英文单词 + 中文单字/二元组），numpy 实现，零依赖零 API
- api：配置 EMBEDDING_API_KEY 后通过 httpx 直连 OpenAI 兼容 Embedding 接口
两者输出接口一致，切换不需要改检索代码。
"""
from __future__ import annotations

import hashlib
import re

import httpx
import numpy as np

from ..config import settings
from ..model_connection import normalize_base_url, single_line, origin

_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = _WORD_RE.findall(text)
    cjk = _CJK_RE.findall(text)
    tokens.extend(cjk)
    tokens.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


def _hash_bucket(token: str, dim: int) -> int:
    h = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "little") % dim


class LocalEmbedder:
    """确定性哈希向量，L2 归一化。仅用于无 API Key 的开发/演示与单测。"""

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.vector_dim

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in _tokenize(text):
                vecs[row, _hash_bucket(token, self.dim)] += 1.0
            norm = np.linalg.norm(vecs[row])
            if norm > 0:
                vecs[row] /= norm
        return vecs


def embedding_connection() -> dict:
    """Capture one connection; never lend a chat key to a different origin."""
    from .. import config
    with config._settings_lock:
        config.require_available_credentials("embedding_api_key")
        base = normalize_base_url(settings.embedding_base_url or settings.llm_base_url or "", "embeddings")
        key = settings.embedding_api_key
        if not key and (not settings.embedding_base_url or origin(base) == origin(settings.llm_base_url)):
            config.require_available_credentials("llm_api_key")
            key = settings.llm_api_key
        return {"base_url": base, "model_id": settings.embedding_model, "api_key": key,
                "mode": settings.embedding_mode}


class APIEmbedder:
    """OpenAI 兼容 Embeddings，httpx 直连。"""

    def __init__(self, timeout: float = 60.0, *, connection: dict | None = None,
                 synthetic: bool = False, authorization_check=None) -> None:
        self._timeout = timeout
        self._connection = dict(connection) if connection is not None else None
        self._synthetic = synthetic
        self._authorization_check = authorization_check

    def encode(self, texts: list[str]) -> np.ndarray:
        from ..call_ledger import begin_call, finish_call, record_usage
        from ..runtime_context import current_runtime_config
        from ..request_resources import request_admission
        import hashlib
        from urllib.parse import urlsplit
        connection = dict(self._connection or embedding_connection())
        base = normalize_base_url(connection["base_url"], "embeddings")
        if not base:
            raise ValueError("请先填写 Embedding 接口地址")
        url = f"{base}/embeddings"
        model = single_line(connection["model_id"], "Embedding 模型名称")
        if not model:
            raise ValueError("请先填写 Embedding 模型名称")
        body = {"model": model, "input": texts}
        headers = {"Content-Type": "application/json"}
        key = single_line(connection["api_key"], "API Key")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        from ..agent.query_vector_cache import lookup, publish
        cache_ticket, cached_vector = lookup(connection, texts, synthetic=self._synthetic,
            authorization_check=self._authorization_check)
        if cached_vector is not None:
            return cached_vector
        binding = current_runtime_config() or {}
        try:
            with request_admission(provider=urlsplit(url).hostname or "configured", endpoint=url,
                    credential_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
                    service="embedding", runtime_config=binding) as admission:
                call_id = begin_call(provider=urlsplit(url).hostname or "configured", service="embedding",
                                     endpoint=url, model_id=model, run_id=binding.get("run_id"),
                                     attempt_id=binding.get("attempt_id"), synthetic=self._synthetic)
                admission.bind_call(call_id)
                try:
                    if self._authorization_check is not None and self._authorization_check() is not True:
                        raise ValueError("向量配置或凭据已变化，测试请求未发送")
                    with httpx.Client(timeout=self._timeout, follow_redirects=False) as client:
                        resp = client.post(url, headers=headers, json=body)
                        resp.raise_for_status()
                        data = resp.json()
                    if isinstance(data, dict) and data.get("usage") is not None:
                        record_usage(call_id, data["usage"])
                    items = data.get("data") if isinstance(data, dict) else None
                    if (not isinstance(items, list) or len(items) != len(texts)
                            or any(not isinstance(item, dict) or type(item.get("index")) is not int for item in items)
                            or sorted(item["index"] for item in items) != list(range(len(texts)))):
                        raise ValueError("Embedding 响应条目无效")
                    if any(not isinstance(item.get("embedding"), list) or not item["embedding"]
                           or any(type(value) not in (int, float) for value in item["embedding"]) for item in items):
                        raise ValueError("Embedding 响应向量无效")
                    items.sort(key=lambda item: item["index"])
                    vectors = np.asarray([item["embedding"] for item in items], dtype=np.float32)
                    if vectors.ndim != 2 or vectors.shape[1] == 0 or not np.isfinite(vectors).all():
                        raise ValueError("Embedding 响应向量无效")
                except BaseException as exc:
                    cancelled = isinstance(exc, (GeneratorExit, KeyboardInterrupt)) or type(exc).__name__ == "CancelledError"
                    finish_call(call_id, state="cancelled" if cancelled else "failed", error_kind=type(exc).__name__)
                    raise
                finish_call(call_id)
        except httpx.HTTPError as exc:
            from ..llm import _safe_http_error
            raise _safe_http_error(exc) from None
        publish(cache_ticket, vectors)
        return vectors


_embedder = None


def get_embedder():
    global _embedder
    from ..config import require_available_credentials
    require_available_credentials("embedding_api_key")
    if _embedder is not None:
        return _embedder
    if settings.embedding_mode in ("api", "litellm"):
        _embedder = APIEmbedder()
    else:
        _embedder = LocalEmbedder()
    return _embedder


def reset_embedder() -> None:
    """配置变更后调用，丢弃缓存的 embedder，下次 get_embedder 重新创建。"""
    global _embedder
    _embedder = None
