"""可溯源文献收集测试。"""
from app.models import Paper
from app.pipeline.embeddings import LocalEmbedder
from app.writing.sources import collect_sources


def _add(db, title, abstract, status="parsed", arxiv_id=None):
    db.add(Paper(
        title=title, abstract=abstract, status=status,
        content_hash=f"h-{title}", arxiv_id=arxiv_id,
    ))
    db.commit()


def test_collect_numbers_and_ranks(db):
    _add(db, "Neural Retrieval Method",
         "a neural network approach to retrieval", arxiv_id="2401.1")
    _add(db, "Unrelated Cooking Study", "recipes and cuisine chemistry")
    refs = collect_sources(db, "neural retrieval", LocalEmbedder())
    assert len(refs) == 2
    assert [r.ref_index for r in refs] == [1, 2]
    # 与 idea 更相关的排第一
    assert refs[0].title == "Neural Retrieval Method"
    assert refs[0].url == "https://arxiv.org/abs/2401.1"


def test_only_parsed_papers_collected(db):
    _add(db, "Parsed Work", "relevant content", status="parsed")
    _add(db, "Uploaded Work", "relevant content", status="uploaded")
    refs = collect_sources(db, "relevant content", LocalEmbedder())
    assert len(refs) == 1
    assert refs[0].title == "Parsed Work"


def test_no_papers_returns_empty(db):
    assert collect_sources(db, "idea", LocalEmbedder()) == []
