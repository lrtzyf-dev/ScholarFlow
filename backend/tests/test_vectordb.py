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
    assert reloaded.embedding_cache_matches_current()
    assert np.allclose(reloaded.vectors_for(["c2"])["c2"], [0.0, 1.0, 0.0])


def test_begin_build_and_fill_in_place():
    _clean()
    store = NumpyVectorStore()
    store.begin_build(total_new_rows=3, dim=2)
    assert store.matrix.shape == (3, 2)
    store.fill(["c1"], ["p1"], np.array([[1.0, 0.0]], dtype=np.float32))
    store.fill(["c2", "c3"], ["p1", "p1"],
               np.array([[0.0, 1.0], [0.5, 0.5]], dtype=np.float32))
    assert store.ids == ["c1", "c2", "c3"]
    assert store.matrix.shape == (3, 2)
    store.flush()
    assert VECTOR_PATH.exists()

    reloaded = NumpyVectorStore()
    assert reloaded.matrix.shape == (3, 2)
    assert reloaded.ids == ["c1", "c2", "c3"]
    _clean()


def test_begin_build_keeps_existing_rows():
    _clean()
    store = NumpyVectorStore()
    store.add(["old"], ["p0"], np.array([[1.0, 1.0]], dtype=np.float32))
    store.begin_build(total_new_rows=1, dim=2)
    assert store.matrix.shape == (2, 2)
    store.fill(["new"], ["p1"], np.array([[2.0, 2.0]], dtype=np.float32))
    assert store.ids == ["old", "new"]
    assert np.allclose(store.matrix[0], [1.0, 1.0])
    _clean()


def test_search_tolerates_length_mismatch():
    """矩阵行数多于元数据时，检索以共同长度为准，不抛 IndexError。"""
    _clean()
    store = NumpyVectorStore()
    store.add(["c1", "c2"], ["p1", "p1"],
              np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
    # 人为制造脏数据：矩阵多出一行，但 ids/paper_ids 不变
    store.matrix = np.vstack([store.matrix, np.array([[0.5, 0.5]], dtype=np.float32)])
    hits = store.search(np.array([1.0, 0.0], dtype=np.float32), topk=5)
    assert hits and hits[0][0] == "c1"
    _clean()


def test_embedding_cache_is_disabled_when_configuration_changes(tmp_path, monkeypatch):
    from app.config import settings

    path = tmp_path / "vectors.npy"
    store = NumpyVectorStore(path)
    store.add(["c1"], ["p1"], np.array([[1.0, 0.0]], dtype=np.float32))
    assert store.embedding_cache_matches_current()

    monkeypatch.setattr(settings, "embedding_api_key", "changed-key")
    reloaded = NumpyVectorStore(path)
    assert not reloaded.embedding_cache_matches_current()
    assert reloaded.search(np.array([1.0, 0.0], dtype=np.float32)) == []


def test_legacy_index_without_fingerprint_is_not_used_for_semantic_search(tmp_path):
    path = tmp_path / "legacy.npy"
    np.save(path, np.array([[1.0, 0.0]], dtype=np.float32))
    path.with_suffix(".json").write_text(
        json.dumps({"ids": ["c1"], "paper_ids": ["p1"]}), encoding="utf-8"
    )

    store = NumpyVectorStore(path)
    assert not store.embedding_cache_matches_current()
    assert store.search(np.array([1.0, 0.0], dtype=np.float32)) == []


def test_vectors_for_tracks_remove_and_rebuild():
    _clean()
    store = NumpyVectorStore()
    store.add(
        ["c1", "c2"], ["p1", "p2"],
        np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    store.remove_paper("p1")
    assert "c1" not in store.vectors_for(["c1", "c2"])
    assert np.allclose(store.vectors_for(["c2"])["c2"], [0.0, 1.0])

    store.remove_paper("p2")
    assert store.vectors_for(["c2"]) == {}
    assert not VECTOR_PATH.exists()
    assert not VECTOR_PATH.with_suffix(".json").exists()
    store.add(["c3"], ["p3"], np.array([[0.5, 0.5]], dtype=np.float32))
    assert store.embedding_cache_matches_current()
    assert np.allclose(store.vectors_for(["c3"])["c3"], [0.5, 0.5])
    _clean()


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
