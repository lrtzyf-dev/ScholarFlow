"""框架生成与严格校验测试。"""
import pytest

from app.llm import LLMResponse, MockLLM
from app.models import Paper
from app.pipeline.embeddings import LocalEmbedder
from app.writing.framework import _validate_output, build_framework


def _paper(db):
    db.add(Paper(
        title="Neural Retrieval", abstract="neural retrieval method",
        status="parsed", content_hash="p1", arxiv_id="2401.1",
    ))
    db.commit()


class FakeLLM:
    """模拟真实模型：返回预设内容。"""

    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    def chat(self, messages, temperature=0):
        self.calls += 1
        return LLMResponse(content=self._content)


def test_validate_drops_out_of_range_citation():
    raw = '{"sections":[{"key":"abstract","bullets":' \
          '[{"text":"claim","citations":[1,99]}]},{"key":"introduction","bullets":[]},{"key":"related_work","bullets":[]}]}'
    sections, warnings = _validate_output(raw, [1, 2])
    abstract = next(s for s in sections if s["key"] == "abstract")
    assert abstract["bullets"][0]["citations"] == [1]
    assert warnings  # 记录了剔除告警


def test_validate_bad_json_raises():
    with pytest.raises(ValueError):
        _validate_output("not a json", [1])


def test_offline_framework_citations_are_real(db):
    _paper(db)
    out = build_framework(
        db, "neural retrieval", llm=MockLLM(), embedder=LocalEmbedder()
    )
    assert out["mode"] == "offline"
    assert out["verified"] is False
    assert out["structure_valid"] is True
    valid = {r["ref_index"] for r in out["references"]}
    for section in out["sections"]:
        for bullet in section["bullets"]:
            assert set(bullet["citations"]) <= valid


def test_llm_success_path(db):
    _paper(db)
    good = '{"sections":[{"key":"abstract","bullets":' \
           '[{"text":"a","citations":[1]}]},' \
           '{"key":"introduction","bullets":[]},' \
           '{"key":"related_work","bullets":[]}]}'
    out = build_framework(
        db, "neural retrieval", llm=FakeLLM(good), embedder=LocalEmbedder()
    )
    assert out["mode"] == "llm"
    assert out["verified"] is False
    assert out["semantic_verification"] == "not_performed"


def test_llm_repeated_failure_degrades(db):
    _paper(db)
    broken = FakeLLM("### no json here")
    out = build_framework(
        db, "neural retrieval", llm=broken, embedder=LocalEmbedder()
    )
    assert broken.calls == 2  # 重试一次
    assert out["mode"] == "offline"
    assert out["verified"] is False
    assert out["warnings"]
