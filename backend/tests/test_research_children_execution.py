"""Parent executor -> real research bridge/adapters -> durable proofs; no real API."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import research_children as children, research_runs as runs, run_lifecycle, run_store
from app.agent import loop
from app.agent.checkpoint import AgentState, CheckpointNeedsReview, prepare_resume
from app.config import settings
from app.models import QARecord
from app.research_models import ResearchChildLink, ResearchChildRetryConsumption
from app.runtime_models import RuntimeRun, RunAttempt
from .test_research_children import parent, arguments, grant_for
from .test_matrix_runs import seed, api_embeddings, CID


class PowerLoss(BaseException):
    pass


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "approval_mode", "full_access")
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k:
        {"ran": 0, "blocked": False, "feedback": ""})
    runs._stopping.clear()
    yield
    for rid in list(run_store.active_runs()):
        run_store.finish(rid, "interrupted")


def parent_state(db, args, *, question="完成研究子任务并交付结果"):
    binding = parent(db, question=question)
    state = AgentState.model_validate(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    state.runtime_config["approval_mode"] = "full_access"
    state.runtime_config["version"] = 1
    state.messages = [{"role": "system", "content": "Execute the exact requested research child."},
        {"role": "user", "content": question, "_task_root": True}]
    state.todo = [{"id": "child", "title": "执行指定研究子任务", "phase": "analyze", "status": "pending",
        "tool_name": children.START, "tool_arguments": deepcopy(args), "evidence": []},
        {"id": "final", "title": "交付父任务结果", "phase": "final", "status": "pending", "evidence": []}]
    state.plan_source = "model"
    state.planning_started = True
    from app.memory_governance import fingerprint
    state.memory_fingerprint = fingerprint(db, CID)
    state.messages[-1]["_memory_fingerprint"] = state.memory_fingerprint
    state.runtime_config["memory_fingerprint"] = state.memory_fingerprint
    run_lifecycle.save_checkpoint(db, binding["run_id"], state.snapshot())
    db.commit()
    return binding, state


class ParentModel:
    mode = "mock"
    def __init__(self, args):
        self.args, self.requests, self.results = deepcopy(args), [], []
    def stream_chat(self, messages, tools=None):
        from app.llm import LLMResponse
        self.requests.append(deepcopy(messages))
        results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"
                   and m.get("tool_call_id") == "real-child-call"]
        if not results:
            yield "final", LLMResponse("", tool_calls=[{"id": "real-child-call", "name": children.START,
                "arguments": deepcopy(self.args)}])
            return
        self.results.append(deepcopy(results[-1]))
        fixed = results[-1].get("evidence", [])
        answer = "\n\n".join(e["quote"] + "[^" + e["id"] + "]" for e in fixed)
        yield "final", LLMResponse(answer or "研究子任务已交付。")


def execute_parent(db, binding, state, *, writer=None):
    return list(loop.run_agent(db, state.question, conversation_id=CID, run_id=binding["run_id"],
        checkpoint=state.snapshot(), save_checkpoint=writer))


def last_answer(events):
    return next(value for kind, value in reversed(events) if kind == "answer")


def test_parent_matrix_full_chain_uses_fixed_parent_ids_and_closes_exact_goal(db, monkeypatch):
    from app import goal_ledger
    item, paper_ids = seed(db)
    args = arguments(item)
    binding, state = parent_state(db, args, question="比较两篇论文并提供研究结果")
    model = ParentModel(args)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    result = last_answer(execute_parent(db, binding, state))
    assert len(model.requests) == 2 and len(model.results) == 1
    payload = model.results[0]
    assert result["completed"] and payload["outcome"] == "completed"
    assert result["evidence"] and payload["parent_citation_map"]
    assert all(e["id"].startswith("e") and e["child_citation_id"] == "M1" for e in result["evidence"])
    db.expire_all()
    child = children.public(db, payload["run_id"], CID, parent_run_id=binding["run_id"])
    assert payload["result"] == child["result"]
    assert payload["result_sha256"] == runs.digest(child["result"])
    persisted = AgentState.model_validate(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    step = next(step for step in persisted.todo if step.get("tool_name") == children.START)
    proof = step["evidence"][0]
    assert step["status"] == "done" and proof["research_delivery"]["completed"]
    assert proof["research_delivery"]["child_run_id"] == child["run_id"]
    snapshot = goal_ledger.snapshot_goal(db, binding["goal_id"])
    compare = next(r for r in snapshot["requirements"]
        if (r.get("tool_arguments") or {}).get("contract", {}).get("kind") == "compare_source_count")
    assert compare["status"] == "completed" and set(compare["evidence"][0]["source_ids"]) == set(paper_ids)
    assert snapshot["outcome"] == "completed"


def test_parent_translation_adapter_preserves_fixed_source_result_and_proof(db, monkeypatch):
    from app import source_store as sources
    from app.llm import LLMResponse
    from app.translate import translation_run
    seed(db)
    scope = sources.SourceScope("conversation", CID)
    text = "A fixed synthetic research passage."
    fixed = sources.capture_source(db, scope=scope,
        authorization=sources.SourceAuthorization(scope, "child-translation-exact", "upload"),
        kind="upload", title="source.md", raw_bytes=text.encode(), text=text, media_type="text/markdown")
    loc = sources.add_text_locator(db, fixed["revision_id"], scope=scope, line_start=1, line_end=1)
    db.commit()
    args = {"kind": "translation", "request_id": "parent-translation-exact", "inputs": {
        "text": text, "source": "English", "target": "Chinese", "display_mode": "whole",
        "source_ref": {"revision_id": fixed["revision_id"], "locator_id": loc["locator_id"]}}}
    calls = []
    class Translator:
        def chat(self, messages, **opts):
            calls.append(deepcopy(messages))
            return LLMResponse("固定合成研究段落。")
    monkeypatch.setattr(translation_run, "get_llm", Translator)
    binding, state = parent_state(db, args)
    model = ParentModel(args)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    result = last_answer(execute_parent(db, binding, state))
    assert result["completed"] and len(calls) == 1 and len(model.requests) == 2
    payload = model.results[0]
    assert payload["kind"] == "translation" and payload["result"]["translation"] == "固定合成研究段落。"
    assert payload["result"]["source_evidence"][0]["id"] == "T1"
    assert result["evidence"][0]["id"] == "e1" and result["evidence"][0]["child_citation_id"] == "T1"
    assert payload["result_sha256"] == runs.digest(payload["result"])
    db.expire_all()
    persisted = AgentState.model_validate(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    proof = next(step for step in persisted.todo if step.get("tool_name") == children.START)["evidence"][0]
    assert proof["research_delivery"]["research_kind"] == "translation"
    assert proof["research_delivery"]["result_sha256"] == payload["result_sha256"]


def lose_parent_tool_result(db, binding):
    """Durably save intent but simulate power loss before committing its response."""
    def writer(snapshot):
        for key, operation in snapshot["operations"].items():
            if key.startswith("tool/") and operation.get("executed") and not snapshot.get("pending_operation"):
                raise PowerLoss()
        run_lifecycle.save_checkpoint(db, binding["run_id"], snapshot)
        db.commit()
    return writer


def replace_parent_attempt(db, binding, checkpoint):
    run_store.finish(binding["run_id"], "interrupted", attempt_id=binding["attempt_id"])
    db.expire_all()
    old = db.get(RunAttempt, binding["attempt_id"])
    run_lifecycle.finish_attempt(db, binding["run_id"], old.id, old.writer_token, "interrupted")
    run = db.get(RuntimeRun, binding["run_id"])
    attempt, _ = run_lifecycle.admit_attempt(db, run.id, CID, "继续原研究", resume=True,
        request_id="e2e-parent-resume-" + uuid4().hex, expected_revision=run.revision,
        checkpoint=checkpoint)
    state = AgentState.model_validate(attempt.checkpoint)
    state.runtime_config["attempt_id"] = attempt.id
    run_lifecycle.save_checkpoint(db, run.id, state.snapshot())
    db.commit()
    assert run_store.begin(run.id, CID, attempt_id=attempt.id)
    return {**state.runtime_config, "writer_token": state.writer_token}, state


def test_parent_pending_known_child_recovers_next_attempt_without_rebuying_first_result(db, monkeypatch):
    item, _ = seed(db)
    calls = api_embeddings(db, item, monkeypatch)
    args = arguments(item)
    binding, state = parent_state(db, args)
    model = ParentModel(args)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    original = runs.Execution.set_payload
    def interrupt_after_one(context, payload):
        original(context, payload)
        if payload.get("progress", {}).get("completed") == 1:
            raise OSError("Synthetic interruption after durable result")
    monkeypatch.setattr(runs.Execution, "set_payload", interrupt_after_one)
    with pytest.raises(PowerLoss):
        execute_parent(db, binding, state, writer=lose_parent_tool_result(db, binding))
    db.expire_all()
    disk = deepcopy(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    assert disk["pending_operation"]["name"] == children.START and len(calls) == 1
    old_disk = deepcopy(disk)
    prepared, _ = prepare_resume(disk)
    assert prepared.resolution == "retry"
    monkeypatch.setattr(runs.Execution, "set_payload", original)
    newer, restored = replace_parent_attempt(db, binding, prepared.snapshot())
    result = last_answer(execute_parent(db, newer, restored))
    assert result["completed"] and len(calls) == 2
    db.expire_all()
    assert db.get(RunAttempt, binding["attempt_id"]).checkpoint == old_disk
    link = db.scalar(select(ResearchChildLink))
    child = children.public(db, link.child_run_id, CID, parent_run_id=binding["run_id"])
    assert child["attempt_number"] == 2 and child["outcome"] == "completed"


def test_parent_pending_unknown_child_blocks_then_exact_grant_recovers_across_attempt(db, monkeypatch):
    item, _ = seed(db)
    calls = api_embeddings(db, item, monkeypatch, fail_at=1)
    args = arguments(item)
    binding, state = parent_state(db, args)
    model = ParentModel(args)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    with pytest.raises(PowerLoss):
        execute_parent(db, binding, state, writer=lose_parent_tool_result(db, binding))
    db.expire_all()
    disk = deepcopy(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    assert disk["pending_operation"]["name"] == children.START and len(calls) == 1
    prepared, _ = prepare_resume(disk)
    assert prepared.resolution is None
    with pytest.raises(CheckpointNeedsReview):
        execute_parent(db, binding, prepared)
    assert len(calls) == 1 and len(model.requests) == 1
    db.expire_all()
    child = children.public(db, db.scalar(select(ResearchChildLink)).child_run_id, CID,
        parent_run_id=binding["run_id"])
    assert child["requires_unknown_confirmation"]
    grant_id = grant_for(db, binding, child, request_id="e2e-human-known-duplicate-cost")
    prepared, _ = prepare_resume(disk)
    assert prepared.resolution == "retry"
    newer, restored = replace_parent_attempt(db, binding, prepared.snapshot())
    result = last_answer(execute_parent(db, newer, restored))
    assert result["completed"] and len(calls) == 3
    db.expire_all()
    consumed = db.get(ResearchChildRetryConsumption, grant_id)
    assert consumed and consumed.parent_attempt_id == newer["attempt_id"]
    assert db.query(ResearchChildRetryConsumption).count() == 1
    assert db.query(QARecord).count() == 1


def test_terminal_partial_child_without_model_plan_cannot_complete_parent(db, monkeypatch):
    """A real offline translation is partial, even if the parent says done."""
    from app import goal_ledger
    from app.llm import MockLLM
    from app.translate import translation_run
    seed(db)
    args = {"kind": "translation", "request_id": "terminal-partial-child", "inputs": {
        "text": "A fixed synthetic passage.", "source": "English", "target": "Chinese",
        "display_mode": "whole", "source_ref": None}}
    monkeypatch.setattr(translation_run, "get_llm", MockLLM)
    binding, state = parent_state(db, args)
    state.todo, state.plan_source, state.planning_started = [], "requirements", False
    run_lifecycle.save_checkpoint(db, binding["run_id"], state.snapshot()); db.commit()
    model = ParentModel(args)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    result = last_answer(execute_parent(db, binding, state))
    db.expire_all()
    child = children.public(db, db.scalar(select(ResearchChildLink)).child_run_id, CID,
        parent_run_id=binding["run_id"])
    assert child["outcome"] == "partial" and not child["delivery_verified"]
    assert result["completed"] is False and result["outcome"] == "partial"
    assert result["incomplete_steps"]
    assert result["goal_outcome"] != "completed"
    snapshot = goal_ledger.snapshot_goal(db, binding["goal_id"])
    assert snapshot["outcome"] != "completed"
    assert snapshot["completed_at"] is None
    saved = AgentState.model_validate(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    assert any(item.get("status") == "open" for item in saved.tool_failures)


def test_successful_resume_of_same_child_closes_original_start_plan_step(db, monkeypatch):
    """The immutable child link, rather than tool-name equality, proves delivery."""
    from app.llm import LLMResponse
    item, _ = seed(db)
    calls = api_embeddings(db, item, monkeypatch)
    args = arguments(item)
    binding, state = parent_state(db, args)
    original = runs.Execution.set_payload
    interrupted = []
    def interrupt_once(context, payload):
        original(context, payload)
        if payload.get("progress", {}).get("completed") == 1 and not interrupted:
            interrupted.append(True)
            raise OSError("Synthetic durable known interruption")
    monkeypatch.setattr(runs.Execution, "set_payload", interrupt_once)
    class ResumeModel(ParentModel):
        def stream_chat(self, messages, tools=None):
            receipts = [(m.get("tool_call_id"), json.loads(m["content"])) for m in messages
                if m.get("role") == "tool" and m.get("tool_call_id") in ("real-child-call", "real-child-resume")]
            self.requests.append(deepcopy(messages))
            if not receipts:
                yield "final", LLMResponse("", tool_calls=[{"id": "real-child-call", "name": children.START,
                    "arguments": deepcopy(args)}]); return
            if not any(call == "real-child-resume" for call, _ in receipts):
                first = receipts[-1][1]
                yield "final", LLMResponse("", tool_calls=[{"id": "real-child-resume", "name": children.RESUME,
                    "arguments": {"child_run_id": first["run_id"], "request_id": "parent-known-resume-once",
                        "expected_revision": first["revision"]}}]); return
            last = receipts[-1][1]
            self.results.append(deepcopy(last))
            yield "final", LLMResponse("\n\n".join(e["quote"] + "[^" + e["id"] + "]"
                for e in last["evidence"]))
    model = ResumeModel(args)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    result = last_answer(execute_parent(db, binding, state))
    assert len(calls) == 2 and model.results[-1]["outcome"] == "completed"
    assert result["completed"] is True
    db.expire_all()
    saved = AgentState.model_validate(db.get(RunAttempt, binding["attempt_id"]).checkpoint)
    step = next(step for step in saved.todo if step.get("tool_name") == children.START)
    assert step["status"] == "done"
    assert step["evidence"][-1]["research_delivery"]["child_run_id"] == model.results[-1]["run_id"]
