"""Read-only union of legacy caches and the current committed DB generation."""
import hashlib

import numpy as np
from sqlalchemy import select

from .library_index_models import LibraryIndexState, LibraryIndexVector
from .library_index_store import checked_receipt
from .pipeline.vectordb import current_embedding_fingerprint
from .source_store import SourceCorruption


def query_store(db, base):
    states = list(db.scalars(select(LibraryIndexState).where(LibraryIndexState.current_build_id.is_not(None))
        .execution_options(populate_existing=True)))
    if not states:
        return base  # Existing providers/tests and untouched library keep their path.
    return CurrentIndexVectors(db, base, states)


class CurrentIndexVectors:
    def __init__(self, db, base, states):
        self.db, self.base = db, base
        self.excluded = {s.paper_id for s in states}
        self.receipts = {}
        for state in states:
            receipt = checked_receipt(db, state.current_build_id)
            if receipt.paper_id != state.paper_id or receipt.revision_id != state.indexed_revision_id:
                raise SourceCorruption("当前索引指针与固定版本回执不一致")
            self.receipts[receipt.id] = receipt
        self.fingerprint = current_embedding_fingerprint()
        self.semantic = {r.id: r for r in self.receipts.values() if r.index_mode == "semantic" and r.embedding_fingerprint == self.fingerprint}
        self.base_available = any(p not in self.excluded for p in base.paper_ids) and base.embedding_cache_matches_current()

    def embedding_cache_matches_current(self):
        return bool(self.base_available or self.semantic)

    def has_compatible_dimension(self, vec):
        return bool((self.base_available and self.base.has_compatible_dimension(vec)) or any(r.dimension == vec.shape[-1] for r in self.semantic.values()))

    def _rows(self, *, ids=None, dimension=None, paper_id=None):
        if not self.semantic: return []
        query = select(LibraryIndexVector).where(LibraryIndexVector.build_id.in_(self.semantic))
        if ids is not None: query = query.where(LibraryIndexVector.chunk_id.in_(ids))
        if dimension is not None: query = query.where(LibraryIndexVector.dimension == dimension)
        if paper_id is not None: query = query.where(LibraryIndexVector.paper_id == paper_id)
        return self.db.scalars(query)

    def _vector(self, row):
        receipt = self.semantic[row.build_id]
        if (row.paper_id != receipt.paper_id or row.chunk_id not in receipt.chunk_locators or row.dimension != receipt.dimension
                or len(row.data) != row.dimension * 4 or hashlib.sha256(row.data).hexdigest() != row.sha256):
            raise SourceCorruption("当前索引向量校验失败，未改用旧版本向量")
        value = np.frombuffer(row.data, dtype="<f4")
        if not np.isfinite(value).all(): raise SourceCorruption("当前索引向量包含无效数值")
        return value

    def search(self, vec, topk=10, paper_id=None):
        hits = (self.base.search(vec, topk=topk, paper_id=paper_id, exclude_paper_ids=self.excluded)
            if self.base_available else [])
        rows = list(self._rows(dimension=vec.shape[-1], paper_id=paper_id))
        if rows:
            matrix = np.asarray([self._vector(r) for r in rows], dtype=np.float32)
            scores = matrix @ vec.astype(np.float32)
            hits.extend((row.chunk_id, float(score)) for row, score in zip(rows, scores))
        return sorted(hits, key=lambda item: item[1], reverse=True)[:topk]

    def vectors_for(self, ids):
        allowed = [ident for ident in ids if (index := self.base._id_to_index.get(ident)) is not None
                   and self.base.paper_ids[index] not in self.excluded]
        result = self.base.vectors_for(allowed) if self.base_available else {}
        result.update({row.chunk_id: self._vector(row) for row in self._rows(ids=ids)})
        return result
