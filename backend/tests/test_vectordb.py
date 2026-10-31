"""向量库持久化与历史 .npz.npy 格式兼容回归。"""
import json

import numpy as np

from app.config import VECTOR_PATH
from app.pipeline.vectordb import NumpyVectorStore


def _clean():
    for p in (VECTOR_PATH,
              VECTOR_PATH.with_name("vectors.npz.npy"),
              VECTOR_PATH.with_suffix(".json")):
        if p.exists():
            p.unlink()


def test_save_and_reload_roundtrip():
    _clean()
    vecs = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    store = NumpyVectorStore()
    store.add(["c1", "c2"], ["p1", "p1"], vecs)
    assert VECTOR_PATH.exists(), "规范向量文件应落盘"

    reloaded = NumpyVectorStore()
    assert reloaded.matrix is not None
    assert reloaded.matrix.shape == (2, 3)
    assert reloaded.ids == ["c1", "c2"]
    assert reloaded.paper_ids == ["p1", "p1"]


def test_legacy_npz_npy_fallback():
    _clean()
    # 构造历史场景：矩阵在 vectors.npz.npy，元数据在 vectors.json
    legacy = VECTOR_PATH.with_name("vectors.npz.npy")
    np.save(legacy, np.eye(2, dtype=np.float32))
    VECTOR_PATH.with_suffix(".json").write_text(
        json.dumps({"ids": ["a", "b"], "paper_ids": ["x", "x"]}),
        encoding="utf-8")

    store = NumpyVectorStore()
    assert store._matrix_file() == legacy
    assert store.matrix is not None
    assert store.matrix.shape == (2, 2)
    assert store.ids == ["a", "b"]

    # 保存后迁移到规范路径
    store._save()
    assert VECTOR_PATH.exists()
    _clean()
