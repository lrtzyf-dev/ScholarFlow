"""极简 numpy 向量库（文件持久化），接口与未来的 Chroma 版一致，可直接替换。"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import numpy as np

from ..config import VECTOR_PATH


def current_embedding_fingerprint() -> str:
    """Identify the embedding configuration without storing its API key."""
    from ..config import settings

    if settings.embedding_mode in ("api", "litellm"):
        from ..model_connection import normalize_base_url

        configured_url = settings.embedding_base_url or settings.llm_base_url or ""
        try:
            base_url = normalize_base_url(configured_url, "embeddings")
        except ValueError:
            # Invalid legacy configuration must disable vector search without
            # preventing the lexical retrieval path from running.
            base_url = f"invalid:{configured_url}"
        identity = {
            "mode": "api",
            "base_url": base_url,
            "model": settings.embedding_model.strip(),
            # A key change may select a different account or model alias.
            "key_sha256": hashlib.sha256(
                (settings.embedding_api_key or settings.llm_api_key).encode("utf-8")
            ).hexdigest(),
        }
    else:
        identity = {
            "mode": "local",
            "algorithm": "hash-ngram-v1",
            "dimension": settings.vector_dim,
        }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class NumpyVectorStore:
    def __init__(self, path: str | Path = VECTOR_PATH) -> None:
        self.path = Path(path)
        # 历史兼容：旧默认配置 vector_name=vectors.npz 时，np.save 自动追加 .npy，
        # 实际写出固定名 vectors.npz.npy；加载时回退到该文件。
        self._legacy_matrix = self.path.with_name("vectors.npz.npy")
        self.ids: list[str] = []
        self.paper_ids: list[str] = []
        self.matrix: np.ndarray | None = None
        self._id_to_index: dict[str, int] = {}
        self.embedding_fingerprint: str | None = None
        self._load()

    def _matrix_file(self) -> Path:
        """实际矩阵文件路径：规范路径优先，回退到历史 vectors.npz.npy。"""
        if self.path.exists():
            return self.path
        if self._legacy_matrix.exists():
            return self._legacy_matrix
        return self.path

    def _load(self) -> None:
        matrix_path = self._matrix_file()
        meta_path = self.path.with_suffix(".json")
        if matrix_path.exists() and meta_path.exists():
            self.matrix = np.load(matrix_path)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.ids = meta["ids"]
            self.paper_ids = meta["paper_ids"]
            self.embedding_fingerprint = meta.get("embedding_fingerprint")
            self._reindex()

    def _reindex(self) -> None:
        self._id_to_index = {vector_id: index for index, vector_id in enumerate(self.ids)}

    def _save(self) -> None:
        if self.matrix is None:
            for path in (self.path, self._legacy_matrix, self.path.with_suffix(".json")):
                if path.exists():
                    path.unlink()
            return
        # 统一写规范路径（.npy 时 np.save 不再追加后缀），旧库首次保存即迁移到新格式
        np.save(self.path, self.matrix)
        self.path.with_suffix(".json").write_text(
            json.dumps({
                "ids": self.ids,
                "paper_ids": self.paper_ids,
                "embedding_fingerprint": self.embedding_fingerprint,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def embedding_cache_matches_current(self) -> bool:
        """Return true only when persisted vectors are known to use current settings."""
        return bool(
            self.matrix is not None
            and self.embedding_fingerprint
            and self.embedding_fingerprint == current_embedding_fingerprint()
        )

    def has_compatible_dimension(self, vec: np.ndarray) -> bool:
        return bool(
            self.embedding_cache_matches_current()
            and self.matrix is not None
            and self.matrix.ndim == 2
            and self.matrix.shape[1] == vec.shape[-1]
        )

    def _record_write_fingerprint(self, had_rows: bool) -> None:
        current = current_embedding_fingerprint()
        if not had_rows:
            self.embedding_fingerprint = current
        elif self.embedding_fingerprint != current:
            # Existing rows may have come from another embedder. A mixed matrix
            # must remain unavailable to semantic retrieval until fully rebuilt.
            self.embedding_fingerprint = None

    def add(self, ids: list[str], paper_ids: list[str], vecs: np.ndarray,
            persist: bool = True) -> None:
        self._record_write_fingerprint(bool(self.ids))
        if self.matrix is None:
            self.matrix = vecs.astype(np.float32)
        else:
            self.matrix = np.vstack([self.matrix, vecs.astype(np.float32)])
        start = len(self.ids)
        self.ids.extend(ids)
        self.paper_ids.extend(paper_ids)
        for offset, vector_id in enumerate(ids):
            self._id_to_index[vector_id] = start + offset
        if persist:
            self._save()

    def begin_build(self, total_new_rows: int, dim: int) -> None:
        """预分配容纳既有行 + 新增行的整矩阵，供后续 fill 就地写入。

        相比逐批 add 的 np.vstack（每次都复制整张矩阵），预分配只分配一次，
        避免大批量摄入时反复出现整张矩阵的临时副本。
        """
        existing_n = len(self.ids)
        self._record_write_fingerprint(bool(existing_n))
        if existing_n:
            grown = np.empty((existing_n + total_new_rows, dim), dtype=np.float32)
            grown[:existing_n] = self.matrix
            self.matrix = grown
        else:
            self.matrix = np.empty((total_new_rows, dim), dtype=np.float32)

    def fill(self, ids: list[str], paper_ids: list[str], vecs: np.ndarray) -> None:
        """把一批向量写入预分配矩阵的当前空位，不产生整张副本。"""
        start = len(self.ids)
        self._record_write_fingerprint(bool(start))
        n = len(ids)
        self.matrix[start:start + n] = vecs.astype(np.float32)
        self.ids.extend(ids)
        self.paper_ids.extend(paper_ids)
        for offset, vector_id in enumerate(ids):
            self._id_to_index[vector_id] = start + offset

    def flush(self) -> None:
        """分批构建后显式落盘一次。"""
        self._save()

    def remove_paper(self, paper_id: str) -> None:
        keep = [i for i, pid in enumerate(self.paper_ids) if pid != paper_id]
        if len(keep) == len(self.ids):
            return
        self.ids = [self.ids[i] for i in keep]
        self.paper_ids = [self.paper_ids[i] for i in keep]
        self.matrix = self.matrix[keep] if keep else None
        if not keep:
            self.embedding_fingerprint = None
        self._reindex()
        self._save()

    def search(self, vec: np.ndarray, topk: int = 10, paper_id: str | None = None,
               exclude_paper_ids=None) -> list[tuple[str, float]]:
        if not self.has_compatible_dimension(vec) or not self.ids:
            return []
        # 以三者共同长度为准，任何历史脏数据导致的长度不一致都不应让检索崩溃
        n = min(len(self.ids), len(self.paper_ids), self.matrix.shape[0])
        if n == 0:
            return []
        v = vec.astype(np.float32)
        scores = self.matrix[:n] @ v
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        out = []
        for i, score in ranked:
            if self.paper_ids[i] in (exclude_paper_ids or ()):
                continue
            if paper_id and self.paper_ids[i] != paper_id:
                continue
            out.append((self.ids[i], float(score)))
            if len(out) >= topk:
                break
        return out

    def vectors_for(self, ids: list[str]) -> dict[str, np.ndarray]:
        """Return persisted vectors for known IDs without re-embedding their text."""
        if self.matrix is None:
            return {}
        n = min(len(self.ids), len(self.paper_ids), self.matrix.shape[0])
        return {
            vector_id: self.matrix[index]
            for vector_id in ids
            if (index := self._id_to_index.get(vector_id)) is not None and index < n
        }
