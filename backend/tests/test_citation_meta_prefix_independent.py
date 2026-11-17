"""Independent broad citation candidates and actual-read process scope checks."""
from copy import deepcopy
import json

import pytest

from app.agent.audit import audit_answer, review_citation_scope
from app.agent.citation_scope import process_receipts
from app.llm import LLMResponse
from app.rag.citation import citation_gate
from .test_source_process_coverage_integration import document_history
from .test_known_failed_scientific_delivery_independent import ScientificModel


FACT = "The fixed record enrolled 20 participants.[^e1]"
EVIDENCE = [{"id": "e1", "paper_title": "Fixed record", "quote": "The fixed record enrolled 20 participants."}]


@pytest.mark.parametrize("text", [
    "因此，样本量为2000人。",
    "- 因此，样本量为2000人。",
    "原文未报告任何研究样本量或参与者人数。",
    "- 原文未报告任何研究样本量或参与者人数。",
    "数据表明，样本量为2000人。",
    "# 样本量2000",
    "样本量2000",
    "| 指标 | 数值 |\n|---|---|\n| 样本量 | 2000 |",
    "样本量为 `2000`。",
])
def test_uncited_source_claims_are_scope_candidates_despite_prefix_layout_or_short_length(text):
    # Put the uncited block first: a preceding cited list introduction can
    # legitimately lend its markers to its own items under the public contract.
    gate = citation_gate(text + "\n\n" + FACT, ["e1"])
    assert not gate["passed"] and gate["unsupported"], gate
    assert any("2000" in candidate or "原文未报告" in candidate for candidate in gate["unsupported"]), gate


@pytest.mark.parametrize("text", ["# 样本量2000[^e1]", "样本量为 `2000`。[^e1]", "因此，样本量为2000人。[^e1]"])
def test_all_cited_source_assertions_reach_typed_auditor(db, text):
    model = ScientificModel(repair_succeeds=False)
    result = audit_answer(db, text, EVIDENCE, llm=model, messages=[])
    assert model.audit_requests and result["known_failed_count"] == 1 and result["status"] == "failed", result


def test_literal_code_marker_cannot_cite_the_surrounding_numeric_claim():
    gate = citation_gate("样本量为 `2000`，唯一标记是代码 `[^e1]`。", ["e1"])
    assert not gate["passed"] and gate["marked_claims"] == 0, gate
    assert any("2000" in candidate for candidate in gate["unsupported"]), gate
    only_code = citation_gate("```text\n样本量2000[^e1]\n```", ["e1"])
    assert only_code["total_claims"] == 0 and not only_code["invalid_markers"]


class ScopeModel:
    mode = "api"

    def __init__(self, *, process=False):
        self.process, self.calls = process, []

    def chat(self, messages, **options):
        data = json.loads(messages[-1]["content"])
        self.calls.append(deepcopy(data))
        support = []
        if self.process:
            receipt = data["process_receipts"][0]
            for excerpt in ('"line_start": 1', '"line_end": 2'):
                assert excerpt in receipt["content"]
                support.append({"receipt_id": receipt["id"], "quote": excerpt})
        return LLMResponse(json.dumps({"results": [
            {"index": item["index"], "requires_citation": False,
             "reason": "Actual read range receipt" if self.process else "Independent future reading advice",
             "kind": "process" if self.process else "independent", "process_scope": "bounded" if self.process else "none",
             "supports": support}
            for item in data["uncited_candidates"]]}))


def test_genuine_first_person_process_claim_can_use_actual_read_receipts(db):
    history = document_history(db, "read-proof.md", b"20 participants.\nQuestionnaire cohort.",
        lambda source: [{"line_start": 1, "line_end": 2}])
    receipts, _ = process_receipts(history)
    assert len(receipts) == 1 and receipts[0]["whole_extracted_text_returned"]
    answer = FACT + "\n\n本轮我实际读取了这个附件的第1至2行。"
    gate = citation_gate(answer, ["e1"])
    assert gate["unsupported"], gate
    model = ScopeModel(process=True)
    result = review_citation_scope("Describe the attached record and actual read range.", answer, EVIDENCE, gate, model, messages=history)
    assert result["passed"] and model.calls, result
    exemptions = result["scoped_review"]["exemptions"]
    assert len(exemptions) == 1 and exemptions[0]["kind"] == "process" and exemptions[0]["supports"]


def test_recommendation_prefix_is_independent_only_after_scope_review_without_reading_sources():
    answer = FACT + "\n\n建议先从短段落开始，每天安排20分钟阅读。"
    gate = citation_gate(answer, ["e1"])
    assert gate["unsupported"], gate
    model = ScopeModel()
    result = review_citation_scope("Summarize the record and give daily reading advice.", answer, EVIDENCE, gate, model, messages=[])
    assert result["passed"] and model.calls, result
    assert not model.calls[0]["process_receipts"]
    assert result["scoped_review"]["exemptions"][0]["kind"] == "independent"


def test_daily_answer_with_selected_attachment_does_not_enter_citation_gate_or_read_tools(db, monkeypatch):
    from app import run_lifecycle, run_store, source_store as sources
    from app.agent import loop
    from app.agent.checkpoint import AgentState
    from app.memory_governance import fingerprint
    from app.runtime_models import RunAttempt
    from app.source_runtime import freeze_sources
    from .test_research_children import parent
    from .test_matrix_runs import CID
    question = "给我安排每天20分钟的英文阅读建议。"
    binding = parent(db, question=question)
    scope = sources.SourceScope("conversation", CID)
    raw = b"An attached research note need not be read for independent daily advice."
    source = sources.capture_source(db, scope=scope,
        authorization=sources.SourceAuthorization(scope, "daily-selected-attachment", "upload"), kind="upload",
        title="research-note.md", raw_bytes=raw, text=raw.decode(), media_type="text/markdown")
    state = AgentState.model_validate(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    state.runtime_config = freeze_sources(db, CID, [source["revision_id"]], {**binding, "mode": "api"})
    state.source_revision_ids = [source["revision_id"]]
    state.phase, state.plan_source = "finalize", "requirements"
    state.answer = "建议先从短段落开始，每天安排20分钟阅读。"
    state.memory_fingerprint = fingerprint(db, CID)
    state.runtime_config["memory_fingerprint"] = state.memory_fingerprint
    state.messages = [{"role": "user", "content": question, "_task_root": True,
        "_memory_fingerprint": state.memory_fingerprint}]
    class IndependentModel:
        mode = "api"
        def __init__(self): self.calls = []
        def chat(self, messages, **options):
            payload = json.loads(messages[-1]["content"])
            self.calls.append(deepcopy(payload))
            return LLMResponse(json.dumps({"source_required": False, "quality_issues": [], "results": [
                {"index": item["index"], "kind": "independent", "claims": []} for item in payload["blocks"]]}))
    model = IndependentModel()
    def no_source_flow(*args, **kwargs):
        pytest.fail("Independent daily advice must not force source tools or citation checks")
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "execute_tool", no_source_flow)
    monkeypatch.setattr(loop, "citation_gate", no_source_flow)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    run_lifecycle.save_checkpoint(db, binding["run_id"], state.snapshot()); db.commit()
    try:
        events = list(loop.run_agent(db, question, conversation_id=CID, run_id=binding["run_id"], checkpoint=state.snapshot()))
        result = next(value for kind, value in reversed(events) if kind == "answer")
        assert result["completed"] and result["refusal"] == 0 and result["evidence"] == [], result
        assert result["answer"] == state.answer and result["source_attribution"]["status"] == "not_applicable"
        assert len(model.calls) == 1 and not any(kind == "tool_call" for kind, _ in events)
    finally:
        run_store.finish(binding["run_id"], "interrupted")
