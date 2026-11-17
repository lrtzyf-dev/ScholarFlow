"""把实际混合检索器包装成评测所需的 search_fn。

search_fn(query, limit) 返回按检索顺序排列的 chunk id 列表，
从而可直接交给 runner.evaluate / grid_search。

说明：当前 retriever 的 top_k 已可参数化；RRF_K 与 MMR 相关参数在
retriever 内为固定常量，网格框架已支持多参数对比，若需对这些常量调参，
应先让 retriever 暴露对应入参，再在此处透传。
"""
from __future__ import annotations

from app.rag.retriever import retrieve


def make_real_search_fn(db):
    """返回绑定数据库会话的 search_fn。"""

    def search_fn(query: str, limit: int) -> list[str]:
        evidences = retrieve(db, query, topk=limit)
        return [evidence.chunk_id for evidence in evidences]

    return search_fn
