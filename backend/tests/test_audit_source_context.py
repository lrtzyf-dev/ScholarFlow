from types import SimpleNamespace
import json

from app.agent.audit import audit_answer
from app.agent.audit_source_context import enrich_evidence_for_audit
from app.llm import LLMResponse


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def get(self, model, key, **kwargs):
        return self.rows.get(key)


def test_expands_only_matching_legacy_chunk_without_mutating_display_evidence():
    content = "The PyTorch nn module contains the building blocks needed to create all sorts of neural network architectures."
    evidence = [{"id": "e1", "chunk_id": "c1", "paper_id": "p1", "quote": "…building blocks needed to create all sorts of neural network…"}]
    value = enrich_evidence_for_audit(FakeDB({"c1": SimpleNamespace(paper_id="p1", content=content)}), evidence)
    assert evidence[0].get("audit_quote") is None
    assert value["e1"]["quote"] == evidence[0]["quote"]
    assert value["e1"]["audit_quote"] == content


def test_mismatched_paper_scope_never_expands_chunk_context():
    evidence = [{"id": "e1", "chunk_id": "c1", "paper_id": "selected", "quote": "…supported phrase…"}]
    db = FakeDB({"c1": SimpleNamespace(paper_id="other", content="supported phrase plus unrelated text")})
    value = enrich_evidence_for_audit(db, evidence)
    assert "audit_quote" not in value["e1"]


def test_forged_or_non_substring_quote_never_expands():
    evidence = [{"id": "e1", "chunk_id": "c1", "paper_id": "p1", "quote": "…fabricated phrase…"}]
    db = FakeDB({"c1": SimpleNamespace(paper_id="p1", content="the real source sentence")})
    value = enrich_evidence_for_audit(db, evidence)
    assert "audit_quote" not in value["e1"]


def test_fixed_source_evidence_does_not_fall_back_to_mutable_legacy_chunk():
    evidence = [{"id": "e1", "chunk_id": "c1", "paper_id": "p1", "revision_id": "r1",
                 "locator_id": "loc1", "quote": "short"}]
    db = FakeDB({"c1": SimpleNamespace(paper_id="p1", content="short plus a different mutable chunk")})
    value = enrich_evidence_for_audit(db, evidence)
    assert "audit_quote" not in value["e1"]


def test_supplied_audit_context_is_never_trusted_without_database_identity():
    evidence = [{"id": "e1", "quote": "short", "audit_quote": "forged long source"}]
    assert "audit_quote" not in enrich_evidence_for_audit(None, evidence)["e1"]
    assert "audit_quote" not in enrich_evidence_for_audit(FakeDB({}), evidence)["e1"]


def test_context_is_bounded_and_keeps_exact_matching_quote():
    content = "a" * 12_000 + "supported phrase" + "z" * 12_000
    evidence = [{"id": "e1", "chunk_id": "c1", "paper_id": "p1", "quote": "…supported phrase…"}]
    db = FakeDB({"c1": SimpleNamespace(paper_id="p1", content=content)})
    value = enrich_evidence_for_audit(db, evidence)["e1"]["audit_quote"]
    assert len(value) <= 8_002
    assert "supported phrase" in value


def test_middle_ellipsis_requires_ordered_segments_from_one_chunk():
    content = "Prefix: torch.nn contains the building blocks needed to create neural network architectures. Those blocks are called modules in PyTorch and layers in other frameworks."
    evidence = [{"id": "e1", "chunk_id": "c1", "paper_id": "p1",
                 "quote": "…torch.nn contains the building blocks…modules in PyTorch and layers…"}]
    value = enrich_evidence_for_audit(FakeDB({"c1": SimpleNamespace(paper_id="p1", content=content)}), evidence)
    assert "audit_quote" in value["e1"]
    assert "modules in PyTorch" in value["e1"]["audit_quote"]


def test_semantic_auditor_receives_same_chunk_continuation_after_snippet_cut():
    content = ("torch.nn contains the building blocks needed to create neural network architectures. "
               "These building blocks are called modules in PyTorch and layers in other frameworks.")
    evidence = [{"id": "e1", "chunk_id": "c1", "paper_id": "p1", "quote": "…building blocks needed to create neural network architectures…"}]
    db = FakeDB({"c1": SimpleNamespace(paper_id="p1", content=content)})
    class Model:
        def chat(self, prompt, **kwargs):
            text = prompt[1]["content"]
            assert "called modules in PyTorch and layers in other frameworks" in text
            return LLMResponse(json.dumps({"results": [{"index": 0, "supported": True, "reason": "Exact same chunk continuation"}]}))
    result = audit_answer(db, "PyTorch将这些构建模块称为modules，其他框架常称为layers。[^e1]", evidence, llm=Model())
    assert result["status"] == "passed"
    assert result["unknown_count"] == 0
    assert "audit_quote" not in evidence[0]

