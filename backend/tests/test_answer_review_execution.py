"""Paid-review journal identity through actual executor restart; no paid requests."""
from copy import deepcopy

import pytest

from app.agent import loop, source_attribution
from app.agent.checkpoint import AgentState
from .test_answer_quality_execution import QUESTION, BAD, ISSUE, verdict


class PowerLoss(BaseException):
    pass


class ReviewerModel:
    mode = "api"
    def stream_chat(self, *a, **kw):
        pytest.fail("a finalized answer must not buy another generation")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(loop, "get_llm", ReviewerModel)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw:
        {"ran": 0, "blocked": False, "feedback": ""})


def initial_state():
    return AgentState(question=QUESTION, phase="finalize", answer=BAD,
        messages=[{"role": "user", "content": QUESTION, "_task_root": True}],
        runtime_config={"mode": "api", "model_id": "synthetic"}, plan_source="requirements")


def test_completed_review_restart_and_budget_change_do_not_repeat_dispatch(db, monkeypatch):
    saved, calls = [], []
    def review(*a, **kw):
        calls.append(1)
        kw["on_usage"]({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return verdict()
    def save(snapshot):
        saved.append(deepcopy(snapshot))
        if any(row.get("quality_status") == "passed" for row in snapshot["source_attribution_reviews"].values()):
            raise PowerLoss()
    monkeypatch.setattr(source_attribution, "review_source_attribution", review)
    with pytest.raises(PowerLoss):
        list(loop.run_agent(db, QUESTION, run_id="review-cached-recovery",
            checkpoint=initial_state().snapshot(), save_checkpoint=save))
    restored = deepcopy(saved[-1])
    restored["runtime_config"].update(attempt_id="new-resource-attempt", approval_mode="ask",
        resource_policy={"policy": {"currency": "USD", "amount_limit": "100", "request_estimate": "0.01"}})
    events = list(loop.run_agent(db, QUESTION, run_id="review-cached-recovery", checkpoint=restored))
    result = next(value for kind, value in events if kind == "answer")
    assert calls == [1] and result["completed"]
    assert result["usage"]["total_tokens"] == 2


def test_pending_review_restart_keeps_known_quality_failure_and_never_rebuys(db, monkeypatch):
    saved, calls = [], []
    state = initial_state()
    quality_key = source_attribution.quality_repair_context_key(QUESTION, state.messages)
    state.answer_quality_repairs[quality_key] = {"repair_claimed": True, "unresolved": [deepcopy(ISSUE)]}
    def dispatched(*a, **kw):
        calls.append(1)
        raise PowerLoss()
    monkeypatch.setattr(source_attribution, "review_source_attribution", dispatched)
    with pytest.raises(PowerLoss):
        list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(),
            save_checkpoint=lambda snapshot: saved.append(deepcopy(snapshot))))
    assert any(row.get("review_dispatch") == "pending" for row in saved[-1]["source_attribution_reviews"].values())
    events = list(loop.run_agent(db, QUESTION, checkpoint=deepcopy(saved[-1])))
    result = next(value for kind, value in events if kind == "answer")
    assert calls == [1] and result["completed"] is False and result["outcome"] == "partial"
    assert result["source_attribution"]["error_code"] == "dispatch_outcome_unknown"
    assert result["answer_quality"]["known_failed_count"] == 1
    assert result["stop_reason"] == "answer_quality_unresolved"


def test_corrected_answer_with_new_fixed_evidence_can_retire_known_quality_issue(db, monkeypatch):
    """A successful repair may read a source without making quality permanent."""
    import json
    from app import source_store as sources
    from app.llm import LLMResponse
    from app.models import Conversation
    from app.memory_governance import fingerprint
    from .test_answer_quality_execution import GOOD
    cid = "quality-corrected-cited"
    db.add(Conversation(id=cid, title="Quality repair")); db.flush()
    scope = sources.SourceScope("conversation", cid)
    fixed = sources.capture_source(db, scope=scope,
        authorization=sources.SourceAuthorization(scope, "quality-source-fixed", "upload"),
        kind="upload", title="schedule.md", raw_bytes=GOOD.encode(), text=GOOD, media_type="text/markdown")
    locator = sources.add_text_locator(db, fixed["revision_id"], scope=scope, line_start=1, line_end=1)
    db.commit()
    locator = sources.read_locator(db, locator["locator_id"], scope=scope)
    evidence = {**locator, "id": "source-local-1", "title": "schedule.md", "provenance_status": "fixed_source",
                "semantic_verification": "not_assessed", "verified": False}
    state = initial_state()
    state.conversation_id = cid
    state.memory_fingerprint = fingerprint(db, cid)
    state.messages[0]["_memory_fingerprint"] = state.memory_fingerprint
    reviews, generations = [], []
    def review(*a, **kw):
        reviews.append(1)
        return verdict("failed" if len(reviews) == 1 else "passed")
    class Model:
        mode = "api"
        def stream_chat(self, messages, tools=None):
            generations.append(1)
            if len(generations) == 1:
                yield "final", LLMResponse("", tool_calls=[{"id": "new-evidence", "name": "retrieve_evidence",
                    "arguments": {"query": "schedule"}}])
            else:
                yield "final", LLMResponse(GOOD + "[^e1]")
    monkeypatch.setattr(loop, "get_llm", Model)
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **kw: json.dumps({"evidence": [evidence]}, ensure_ascii=False))
    monkeypatch.setattr(source_attribution, "review_source_attribution", review)
    monkeypatch.setattr(loop, "audit_answer", lambda *a, **kw: {"status": "passed", "passed": True,
        "verified": True, "mode": "synthetic", "claims": [], "unsupported": []})
    events = list(loop.run_agent(db, QUESTION, conversation_id=cid, checkpoint=state.snapshot()))
    result = next(value for kind, value in events if kind == "answer")
    assert generations == [1, 1] and result["answer"] == GOOD + "[^e1]"
    assert result["evidence"][0]["locator_id"] == locator["locator_id"]
    assert result["completed"] is True
    assert len(reviews) == 2 and result["answer_quality"]["status"] == "passed"


def test_cancel_after_plan_finalization_reuses_unchanged_answer_review(db, monkeypatch):
    from app import cancel_store
    run_id = "review-plan-status-cancel"
    state = initial_state()
    state.plan_source = "model"
    state.todo = [{"id": phase, "title": phase, "phase": phase, "status": "pending", "evidence": []}
                  for phase in ("analyze", "write", "final")]
    saved, calls = [], []
    def review(*a, **kw):
        calls.append(1)
        return verdict()
    original = loop.plan_finalize
    def cancel_after_finalize(*a, **kw):
        result = original(*a, **kw)
        cancel_store.request_cancel(run_id)
        return result
    monkeypatch.setattr(source_attribution, "review_source_attribution", review)
    monkeypatch.setattr(loop, "plan_finalize", cancel_after_finalize)
    events = list(loop.run_agent(db, QUESTION, run_id=run_id, checkpoint=state.snapshot(),
        save_checkpoint=lambda s: saved.append(deepcopy(s))))
    assert any(kind == "cancelled" for kind, _ in events)
    assert len(calls) == 1 and saved[-1]["phase"] == "finalize"
    assert all(step["status"] == "done" for step in saved[-1]["todo"])
    assert saved[-1]["answer"] == state.answer and saved[-1]["messages"] == state.messages
    cancel_store.clear(run_id)
    monkeypatch.setattr(loop, "plan_finalize", original)
    result = next(value for kind, value in loop.run_agent(db, QUESTION, run_id=run_id,
        checkpoint=deepcopy(saved[-1])) if kind == "answer")
    assert result["completed"] is True
    assert len(calls) == 1
