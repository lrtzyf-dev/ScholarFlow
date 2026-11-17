"""Actual executor/audit/SQLite regression; deterministic model, zero network."""
from copy import deepcopy
import json
import re

import pytest

from app import goal_ledger, run_lifecycle, run_store, source_store as sources
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.config import settings
from app.llm import LLMResponse
from app.models import QARecord
from app.runtime_models import RunAttempt
from app.source_runtime import freeze_sources
from .test_research_children import parent
from .test_matrix_runs import CID


FACTS = [
    "The record enrolled 20 participants.",
    "The cohort used questionnaires.",
    "The follow-up lasted 2 weeks.",
    "The cohort was recruited in 2024.",
    "The analysis included 12 complete responses.",
    "The study did not use randomization.",
]


class ScientificModel:
    mode = "api"

    def __init__(self, *, repair_succeeds, unknown=False, false_fragment="2000"):
        self.repair_succeeds, self.unknown = repair_succeeds, unknown
        self.false_fragment = false_fragment
        self.audit_requests, self.repair_requests = [], []

    def chat(self, messages, **opts):
        prompt = messages[-1]["content"]
        claims = re.findall(r"^\[论断 (\d+)\] (.*)$", prompt, re.MULTILINE)
        if claims:
            self.audit_requests.append(deepcopy(messages))
            return LLMResponse(json.dumps({"results": [
                {"index": int(index), "supported": not ("2000" in text or self.false_fragment in text),
                 "reason": "Fixed original explicitly reports 20 participants; claim conflicts." if "2000" in text or self.false_fragment in text else "Matches fixed original.",
                 "support_kind": "uncertain" if self.unknown and ("2000" in text or self.false_fragment in text) else "source",
                 "process_scope": "none", "supports": []}
                for index, text in claims]}))
        self.repair_requests.append(deepcopy(messages))
        facts = FACTS if self.repair_succeeds else [FACTS[0].replace("20", "2000"), *FACTS[1:]]
        return LLMResponse("\n\n".join(fact + "[^e1]" for fact in facts))

    def stream_chat(self, messages, tools=None):
        raise AssertionError("Finalization must not launch an unrelated tool/model turn")
        yield


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k:
        {"ran": 0, "blocked": False, "feedback": ""})
    yield
    for run_id in list(run_store.active_runs()):
        run_store.finish(run_id, "interrupted")


def execute_fixed_scientific_draft(db, monkeypatch, model, *, structured_read=False, false_prefix="", false_fact=None):
    question = "Summarize the six study facts using the attached fixed record."
    binding = parent(db, question=question)
    scope = sources.SourceScope("conversation", CID)
    raw = "\n".join(FACTS)
    source = sources.capture_source(db, scope=scope,
        authorization=sources.SourceAuthorization(scope, "known-failure-fixed-record", "upload"),
        kind="upload", title="fixed-study.md", raw_bytes=raw.encode(), text=raw, media_type="text/markdown")
    locator = sources.add_text_locator(db, source["revision_id"], scope=scope, line_start=1, line_end=len(FACTS))
    binding = freeze_sources(db, CID, [source["revision_id"]], {**binding, "mode": "api"})
    if structured_read:
        from app.runtime_context import bind_runtime_config
        from app.source_tools import execute_source_tool
        arguments = {"source_revision_id": source["revision_id"], "line_start": 1, "line_end": len(FACTS)}
        before = goal_ledger.snapshot_goal(db, binding["goal_id"])
        updated = goal_ledger.revise_goal(db, binding["goal_id"], expected_revision=before["revision"],
            action="add_requirements", source_message_id="explicit-read-acceptance",
            requirements=[{"title": "Read the six-line fixed record", "phase": "read", "required": True,
                "tool_name": "read_attached_source", "tool_arguments": arguments}])
        with bind_runtime_config(binding):
            content = json.loads(execute_source_tool(db, "read_attached_source", arguments, conversation_id=CID))
        assert not content.get("error"), content
        requirement = next(item for item in updated["requirements"] if item["required"])
        goal_ledger.record_requirement_evidence(db, binding["goal_id"], requirement["id"],
            {"kind": "tool_result", "status": "completed", "tool_name": "read_attached_source", "arguments": arguments})
        assert all(item["status"] == "completed" for item in goal_ledger.snapshot_goal(db, binding["goal_id"])["requirements"])
    state = AgentState.model_validate(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    state.runtime_config = binding
    state.source_revision_ids = [source["revision_id"]]
    state.phase, state.plan_source = "finalize", "requirements"
    state.answer = "\n\n".join(fact + "[^e1]" for fact in [false_fact or FACTS[0].replace("20", "2000"), *FACTS[1:]])
    if false_prefix:
        state.answer = false_prefix + state.answer
    state.evidence = [{"id": "e1", "source_id": source["source_id"], "revision_id": source["revision_id"],
        "locator_id": locator["locator_id"], "quote": locator["quote"], "paper_title": "Fixed study record"}]
    from app.memory_governance import fingerprint
    state.memory_fingerprint = fingerprint(db, CID)
    state.runtime_config["memory_fingerprint"] = state.memory_fingerprint
    state.messages = [{"role": "user", "content": question, "_task_root": True,
        "_memory_fingerprint": state.memory_fingerprint}]
    run_lifecycle.save_checkpoint(db, binding["run_id"], state.snapshot()); db.commit()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(loop.run_agent(db, question, conversation_id=CID, run_id=binding["run_id"], checkpoint=state.snapshot()))
    result = next(value for kind, value in reversed(events) if kind == "answer")
    db.expire_all()
    goal = goal_ledger.snapshot_goal(db, binding["goal_id"])
    qa = db.get(QARecord, result["qa_id"])
    diagnostics = {"audit": result["audit"], "answer": result["answer"], "completed": result["completed"],
        "outcome": result["outcome"], "goal_outcome": result["goal_outcome"],
        "durable_goal_outcome": goal["outcome"], "goal_completed_at": goal["completed_at"],
        "incomplete_steps": result["incomplete_steps"], "repair_requests": len(model.repair_requests),
        "audit_requests": len(model.audit_requests), "persisted_answer": qa.answer,
        "goal_requirements": [{"required": item["required"], "status": item["status"], "title": item["title"]}
            for item in goal["requirements"]]}
    return result, goal, diagnostics


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_one_known_false_claim_cannot_be_delivered_or_complete_goal_among_six(db, monkeypatch, repair_succeeds, record_property):
    model = ScientificModel(repair_succeeds=repair_succeeds)
    result, goal, diagnostics = execute_fixed_scientific_draft(db, monkeypatch, model)
    record_property("scientific_delivery_diagnostics", json.dumps(diagnostics, ensure_ascii=False, default=str))
    # A ratio of 1/6 cannot excuse a known 100-fold sample-size fabrication.
    assert "2000" not in result["answer"], diagnostics
    if repair_succeeds:
        assert model.repair_requests and result["audit"]["known_failed_count"] == 0, diagnostics
        assert result["completed"], diagnostics
        # An empty structural ledger deliberately remains open even on success.
        assert goal["outcome"] == "open" and not goal["requirements"], diagnostics
    else:
        assert result["completed"] is False and result["outcome"] in ("partial", "blocked"), diagnostics
        assert result["goal_outcome"] != "completed" and goal["outcome"] != "completed", diagnostics
        assert goal["completed_at"] is None and result["incomplete_steps"], diagnostics


def test_one_unknown_review_remains_unknown_and_does_not_trigger_false_claim_refusal(db, monkeypatch, record_property):
    model = ScientificModel(repair_succeeds=False, unknown=True)
    result, _, diagnostics = execute_fixed_scientific_draft(db, monkeypatch, model)
    record_property("scientific_delivery_diagnostics", json.dumps(diagnostics, ensure_ascii=False, default=str))
    assert result["audit"]["status"] == "unverified", diagnostics
    assert result["audit"]["unknown_count"] == 1 and result["audit"]["known_failed_count"] == 0, diagnostics
    assert result["refusal"] == 0 and not model.repair_requests, diagnostics
    assert result["completed"] is False and result["outcome"] == "partial", diagnostics
    assert any(step.get("reason") == "citation_review_unresolved" for step in result["incomplete_steps"]), diagnostics
    assert result["goal_outcome"] != "completed", diagnostics


@pytest.mark.parametrize("unknown", [False, True])
def test_unresolved_citation_does_not_complete_goal_even_when_actual_read_requirement_is_done(db, monkeypatch, record_property, unknown):
    model = ScientificModel(repair_succeeds=False, unknown=unknown)
    result, goal, diagnostics = execute_fixed_scientific_draft(db, monkeypatch, model, structured_read=True)
    record_property("scientific_delivery_diagnostics", json.dumps(diagnostics, ensure_ascii=False, default=str))
    required = [item for item in goal["requirements"] if item["required"]]
    assert required and all(item["status"] == "completed" for item in required), diagnostics
    assert not result["completed"] and result["goal_outcome"] != "completed", diagnostics
    assert goal["outcome"] != "completed" and goal["completed_at"] is None, diagnostics
    assert any(step.get("reason") == "citation_review_unresolved" for step in result["incomplete_steps"]), diagnostics


@pytest.mark.parametrize("prefix", ["因此，", "数据表明，"])
def test_source_fact_cannot_bypass_semantic_review_with_meta_sounding_prefix(db, monkeypatch, record_property, prefix):
    model = ScientificModel(repair_succeeds=False)
    result, _, diagnostics = execute_fixed_scientific_draft(db, monkeypatch, model, false_prefix=prefix)
    record_property("scientific_delivery_diagnostics", json.dumps(diagnostics, ensure_ascii=False, default=str))
    assert "2000" not in result["answer"], diagnostics
    assert result["completed"] is False and model.repair_requests, diagnostics


def test_source_absence_statement_cannot_bypass_semantic_review_as_document_limitation(db, monkeypatch, record_property):
    false_fact = "原文未报告任何研究样本量或参与者人数。"
    model = ScientificModel(repair_succeeds=False, false_fragment=false_fact)
    result, _, diagnostics = execute_fixed_scientific_draft(db, monkeypatch, model, false_fact=false_fact)
    record_property("scientific_delivery_diagnostics", json.dumps(diagnostics, ensure_ascii=False, default=str))
    assert false_fact not in result["answer"] and "2000" not in result["answer"], diagnostics
    assert result["completed"] is False and model.repair_requests, diagnostics
