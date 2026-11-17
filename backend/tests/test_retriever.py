"""混合检索与 RRF 测试。"""
from app.rag.retriever import retrieve, rrf_fuse


def test_rrf_fusion_order():
    a = ["x1", "x2", "x3"]
    b = ["x2", "x4", "x1"]
    fused = rrf_fuse([a, b], topk=4)
    ids = [i for i, _ in fused]
    assert ids[0] == "x2"  # 两路都靠前 -> 融合第一
    assert set(ids) == {"x1", "x2", "x3", "x4"}


def test_retrieve_returns_evidence(seeded_paper, db):
    ev = retrieve(db, "how many attention heads and BLEU score", topk=5)
    assert ev, "应当检索到证据"
    assert all(e.page >= 1 for e in ev)
    assert all(e.quote for e in ev)
    joined = " ".join(e.quote for e in ev)
    assert "head" in joined.lower() or "BLEU" in joined


def test_retrieve_chinese(seeded_paper, db):
    ev = retrieve(db, "注意力机制 机器翻译", topk=5)
    assert ev
    assert any("注意力" in e.quote for e in ev)


def test_retrieve_paper_scope(seeded_paper, db):
    ev = retrieve(db, "Transformer", topk=5, paper_id="nonexistent")
    assert ev == []


def test_retrieve_irrelevant_question_returns_empty(seeded_paper, db):
    # 完全无关的问题（纯中文，库内为英文文献），无字面强信号匹配，
    # 应返回空列表，由上层抗幻觉逻辑触发拒答，而非硬凑证据
    ev = retrieve(db, "如何做西红柿炒鸡蛋的具体做法", topk=5)
    assert ev == []


def test_evidence_carries_relevance(seeded_paper, db):
    ev = retrieve(db, "attention mechanism", topk=5)
    assert ev
    # 余弦相关性被记录，且落在合法区间
    assert all(0.0 <= e.relevance <= 1.0 for e in ev)


def test_retrieve_encodes_query_once_and_reuses_indexed_vectors(
    seeded_paper, db, monkeypatch
):
    from app.pipeline.embeddings import LocalEmbedder
    import app.rag.retriever as retriever

    class CountingEmbedder(LocalEmbedder):
        def __init__(self):
            super().__init__()
            self.inputs = []

        def encode(self, texts):
            self.inputs.append(list(texts))
            return super().encode(texts)

    embedder = CountingEmbedder()
    monkeypatch.setattr(retriever, "get_embedder", lambda: embedder)
    query = "attention mechanism"

    evidence = retrieve(db, query, topk=5)

    assert evidence
    assert embedder.inputs == [[query]]


def test_stale_embedding_cache_falls_back_to_lexical_retrieval(
    seeded_paper, db, monkeypatch
):
    from app.config import settings
    import app.rag.retriever as retriever

    monkeypatch.setattr(settings, "embedding_api_key", "changed-key")
    monkeypatch.setattr(
        retriever,
        "get_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("must skip Embedding request")),
    )

    evidence = retrieve(db, "attention mechanism", topk=5)

    assert evidence
    assert any("attention" in item.quote.lower() for item in evidence)


def test_missing_candidate_vector_keeps_lexical_results(seeded_paper, db, monkeypatch):
    from app.pipeline.vectordb import NumpyVectorStore

    monkeypatch.setattr(NumpyVectorStore, "vectors_for", lambda self, ids: {})

    evidence = retrieve(db, "attention mechanism", topk=5)

    assert evidence
    assert any("attention" in item.quote.lower() for item in evidence)
