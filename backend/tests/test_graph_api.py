"""图谱接口 PageRank 集成测试。"""
from app.api.graph import get_graph
from app.models import Paper, Reference


def _add_paper(db, title: str) -> Paper:
    paper = Paper(title=title, status="parsed", content_hash=f"h-{title}")
    db.add(paper)
    db.commit()
    return paper


def test_graph_returns_pagerank_and_ranking(db):
    b = _add_paper(db, "Attention Is All You Need")
    a = _add_paper(db, "A Survey of Transformer Models")
    db.add(
        Reference(
            paper_id=a.id,
            ref_index=1,
            raw_text="[1] Attention Is All You Need",
            title="Attention Is All You Need",
        )
    )
    db.commit()

    result = get_graph(db=db)
    assert "ranking" in result
    node_by_id = {n["id"]: n for n in result["nodes"]}
    for node in result["nodes"]:
        assert "pagerank" in node
        assert "in_centrality" in node
        assert "out_centrality" in node

    # b 被 a 引用，PageRank 与入度中心性应高于引用方
    assert node_by_id[b.id]["pagerank"] > node_by_id[a.id]["pagerank"]
    assert node_by_id[b.id]["in_centrality"] > 0
    # ranking 首项为被引节点，且整体按 pagerank 降序
    assert result["ranking"][0]["id"] == b.id
    scores = [r["pagerank"] for r in result["ranking"]]
    assert scores == sorted(scores, reverse=True)


def test_graph_empty_library_returns_empty_ranking(db):
    result = get_graph(db=db)
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["ranking"] == []


def test_same_version_group_merges_into_one_node(db):
    v1 = _add_paper(db, "Deep Learning v1")
    v1.version_group = "vg-dl"
    v2 = _add_paper(db, "Deep Learning v2")
    v2.version_group = "vg-dl"
    db.commit()

    result = get_graph(db=db)
    assert len(result["nodes"]) == 1
    node = result["nodes"][0]
    assert set(node["member_ids"]) == {v1.id, v2.id}
    assert node["version_count"] == 2


def test_arxiv_id_matches_edge(db):
    target = _add_paper(db, "Attention Is All You Need")
    target.arxiv_id = "1706.03762"
    source = _add_paper(db, "Citing Paper")
    db.add(
        Reference(
            paper_id=source.id,
            ref_index=1,
            raw_text="Vaswani et al. arXiv:1706.03762",
            title="Attention Is All You Need",
        )
    )
    db.commit()

    result = get_graph(db=db)
    assert len(result["edges"]) == 1
    edge = result["edges"][0]
    assert edge["target"] == target.id
    assert edge["match_type"] == "arxiv_id"


def test_natural_sentence_does_not_false_link(db):
    _add_paper(db, "Deep Learning Networks")
    source = _add_paper(db, "Sentence Paper")
    db.add(
        Reference(
            paper_id=source.id,
            ref_index=1,
            raw_text="We discuss deep learning networks are applied in vision tasks.",
            title="We discuss deep learning networks are applied in vision tasks.",
        )
    )
    db.commit()

    result = get_graph(db=db)
    assert result["edges"] == []


def test_subset_graph_restricts_nodes(db):
    b = _add_paper(db, "Attention Is All You Need")
    a = _add_paper(db, "A Survey of Transformer Models")
    extra = _add_paper(db, "Unrelated Standalone Work")
    db.add(
        Reference(
            paper_id=a.id,
            ref_index=1,
            raw_text="[1] Attention Is All You Need",
            title="Attention Is All You Need",
        )
    )
    db.commit()

    result = get_graph(paper_ids=f"{a.id},{b.id}", db=db)
    node_ids = {n["id"] for n in result["nodes"]}
    assert extra.id not in node_ids
    assert node_ids == {a.id, b.id}
