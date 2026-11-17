"""dedup_service 纯逻辑单元测试：文本读取、版本组冲突与无冲突返回。"""
import json

from app.models import Paper
from app.pipeline.dedup_service import detect_version_conflict, load_paper_text


def test_load_paper_text_joins_pages(db, tmp_path):
    parsed = tmp_path / "p.json"
    parsed.write_text(json.dumps({
        "pages": [{"text": "alpha beta"}, {"text": "gamma delta"}],
    }), encoding="utf-8")
    paper = Paper(title="p", parsed_path=str(parsed))
    assert load_paper_text(paper) == "alpha beta\ngamma delta"


def test_load_paper_text_missing_file_returns_empty(db):
    paper = Paper(title="p", parsed_path="does-not-exist.json")
    assert load_paper_text(paper) == ""


def test_detect_none_when_no_group_and_empty_text(db):
    paper = Paper(title="incoming")
    db.add(paper)
    db.commit()
    assert detect_version_conflict(db, paper, "") is None


def test_detect_version_group_conflict(db):
    existing = Paper(title="old", arxiv_id="1706.03762v7", version_group="1706.03762")
    incoming = Paper(title="new", arxiv_id="1706.03762v1", version_group="1706.03762")
    db.add_all([existing, incoming])
    db.commit()
    conflict = detect_version_conflict(db, incoming, "")
    assert conflict is not None
    assert conflict["version_duplicate"] is True
    assert conflict["existing_id"] == existing.id
    assert conflict["incoming_version"] == 1
    assert conflict["existing_version"] == 7
    assert conflict["newer"] is False
    assert conflict["recommend"] == "existing"
