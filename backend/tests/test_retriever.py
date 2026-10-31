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
