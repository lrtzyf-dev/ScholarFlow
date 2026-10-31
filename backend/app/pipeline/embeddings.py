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


class APIEmbedder:
    """OpenAI 兼容 Embeddings，httpx 直连。"""

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout

    def encode(self, texts: list[str]) -> np.ndarray:
        base = (settings.embedding_base_url or settings.llm_base_url or "").rstrip("/")
        url = f"{base}/embeddings"
        body = {"model": settings.embedding_model, "input": texts}
        headers = {"Content-Type": "application/json"}
        key = settings.embedding_api_key or settings.llm_api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        items = sorted(data["data"], key=lambda d: d["index"])
        return np.asarray([d["embedding"] for d in items], dtype=np.float32)


_embedder = None


def get_embedder():
    global _embedder
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
