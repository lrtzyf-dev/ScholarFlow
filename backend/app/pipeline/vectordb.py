"""极简 numpy 向量库（文件持久化），接口与未来的 Chroma 版一致，可直接替换。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..config import VECTOR_PATH


class NumpyVectorStore:
    def __init__(self, path: str | Path = VECTOR_PATH) -> None:
        self.path = Path(path)
        # 历史兼容：旧默认配置 vector_name=vectors.npz 时，np.save 自动追加 .npy，
        # 实际写出固定名 vectors.npz.npy；加载时回退到该文件。
        self._legacy_matrix = self.path.with_name("vectors.npz.npy")
        self.ids: list[str] = []
        self.paper_ids: list[str] = []
        self.matrix: np.ndarray | None = None
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

    def _save(self) -> None:
        if self.matrix is None:
            return
        # 统一写规范路径（.npy 时 np.save 不再追加后缀），旧库首次保存即迁移到新格式
        np.save(self.path, self.matrix)
        self.path.with_suffix(".json").write_text(
            json.dumps({"ids": self.ids, "paper_ids": self.paper_ids}, ensure_ascii=False),
            encoding="utf-8",
        )

    def add(self, ids: list[str], paper_ids: list[str], vecs: np.ndarray) -> None:
        if self.matrix is None:
            self.matrix = vecs.astype(np.float32)
        else:
            self.matrix = np.vstack([self.matrix, vecs.astype(np.float32)])
        self.ids.extend(ids)
        self.paper_ids.extend(paper_ids)
        self._save()

    def remove_paper(self, paper_id: str) -> None:
        keep = [i for i, pid in enumerate(self.paper_ids) if pid != paper_id]
        if len(keep) == len(self.ids):
            return
        self.ids = [self.ids[i] for i in keep]
        self.paper_ids = [self.paper_ids[i] for i in keep]
        self.matrix = self.matrix[keep] if keep else None
        self._save()

    def search(self, vec: np.ndarray, topk: int = 10, paper_id: str | None = None) -> list[tuple[str, float]]:
        if self.matrix is None or not self.ids:
            return []
        v = vec.astype(np.float32)
        scores = self.matrix @ v
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        out = []
        for i, score in ranked:
            if paper_id and self.paper_ids[i] != paper_id:
                continue
            out.append((self.ids[i], float(score)))
            if len(out) >= topk:
                break
        return out
