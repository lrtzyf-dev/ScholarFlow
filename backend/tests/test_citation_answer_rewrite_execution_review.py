"""Independent production-executor review; every model reply is synthetic.

These tests exercise the real audit journals, fixed source, writer fence and
SQLite checkpoints. Synthetic semantic verdicts do not prove model accuracy.
"""
from copy import deepcopy
import json

import pytest

from app import cancel_store, run_lifecycle, run_store
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from app.runtime_models import RunAttempt
from tests.test_citation_repair_review import _fixed_evidence, _current_checkpoint
from tests.test_research_children import parent


PRIVATE = "SYNTHETIC_PRIVATE_REWRITE_CONTEXT"
USAGE = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
QUESTION = "依据所选来源解释方法"
ORIGINAL = "模型采用99个注意力头完成编码处理。[^e1]"
CORRECTED = "模型采用8个注意力头完成编码处理。[^e1]"


class PowerLoss(BaseException):
    pass


class Model:
    mode = "api"

    def __init__(self, *, lose_rewrite=False, missing_mixed=False):
        self.rewrites, self.audits, self.scopes = [], [], []
        self.quality_reviews = []
        self.lose_rewrite, self.missing_mixed = lose_rewrite, missing_mixed
        self._runtime_config = {"mode": "api", "base_url": "https://executor-review.example.test/v1",
            "model_id": "Synthetic-Executor-Review"}

    def chat(self, messages, **kwargs):
        text = messages[0]["content"]
        payload = messages[-1]["content"]
        if "语义修订后最终答案的独立质量核验员" in text:
            self.quality_reviews.append(deepcopy(messages))
            return LLMResponse('{"quality_issues":[]}')
        if text == "你是严谨的引用审计员，只输出 JSON。":
            self.audits.append(deepcopy(messages))
            passed = self.missing_mixed or "99个" not in payload
            return LLMResponse(json.dumps({"results": [{"index": 0,
                "supported": passed, "reason": "Synthetic explicit source verdict",
                "support_kind": "source", "process_scope": "none", "supports": []}]}))
        # The scope prompt contains an explicit candidate list. The test
        # supplies a mixed claim so it cannot be repaired by marker insertion.
        if "uncited_candidates" in payload:
            data = json.loads(payload)
            self.scopes.append(deepcopy(messages))
            return LLMResponse(json.dumps({"results": [{"index": item["index"],
                "requires_citation": True, "reason": "Synthetic mixed claim needs review",
                "kind": "mixed", "process_scope": "none", "supports": []}
                for item in data["uncited_candidates"]]}))
        self.rewrites.append((deepcopy(messages), kwargs))
        assert len(self.rewrites) == 1, "An uncertain/saved revision was purchased twice"
        if self.lose_rewrite:
            raise PowerLoss("Synthetic loss after durable rewrite intent")
        return LLMResponse(CORRECTED, usage=USAGE, reasoning_content=PRIVATE,
            provider_payload={"assistant": {"opaque": PRIVATE}})


def setup(db, monkeypatch, model, *, answer=ORIGINAL):
    cid, evidence = _fixed_evidence(db)
    binding = parent(db, cid=cid, question=QUESTION)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False})
    state = AgentState(question=QUESTION, conversation_id=cid, phase="finalize", answer=answer,
        evidence=[evidence], evidence_counter=1, runtime_config={**binding, "mode": "api"},
        goal_id=binding["goal_id"], attempt_id=binding["attempt_id"], writer_token=binding["writer_token"],
        messages=[{"role": "user", "content": QUESTION}])
    checkpoint = _current_checkpoint(db, state)
    run_lifecycle.save_checkpoint(db, binding["run_id"], checkpoint)
    db.commit()
    return cid, binding, checkpoint


def saved(db, binding):
    db.expire_all()
    return deepcopy(db.get(RunAttempt, binding["attempt_id"], populate_existing=True).checkpoint)


def run(db, cid, binding, checkpoint, save):
    return list(loop.run_agent(db, QUESTION, conversation_id=cid, run_id=binding["run_id"],
        checkpoint=checkpoint, save_checkpoint=save))


def cleanup(binding):
    cancel_store.clear(binding["run_id"])
    run_store.finish(binding["run_id"], "interrupted", attempt_id=binding["attempt_id"])


def test_executor_restores_durable_private_rewrite_after_cancel_without_repurchase(db, monkeypatch):
    model = Model()
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    cancellation = []

    def persist(value):
        run_lifecycle.save_checkpoint(db, binding["run_id"], value)
        db.commit()
        ready = [row for key, row in value["source_attribution_reviews"].items()
            if key.startswith("citation_rewrite/") and row.get("status") == "ready"
            and row.get("answer_applied") is False]
        if ready and not cancellation:
            cancellation.append(True)
            cancel_store.request_cancel(binding["run_id"])

    try:
        events = run(db, cid, binding, checkpoint, persist)
        assert any(kind == "cancelled" for kind, _ in events)
        checkpoint = saved(db, binding)
        assert checkpoint["phase"] == "finalize" and checkpoint["answer"] == ORIGINAL
        assert checkpoint["usage"]["total_tokens"] == 0
        assert PRIVATE in json.dumps(checkpoint) and PRIVATE not in json.dumps(events)
        cancel_store.clear(binding["run_id"])
        resumed = run(db, cid, binding, checkpoint, persist)
        answer = next(value for kind, value in resumed if kind == "answer")
        assert answer["answer"] == CORRECTED and answer["refusal"] == 0
        assert answer["audit"]["status"] == "passed"
        checkpoint = saved(db, binding)
        assert checkpoint["usage"] == USAGE and PRIVATE in json.dumps(checkpoint["provider_context"])
        assert PRIVATE not in json.dumps(resumed)
        assert len(model.rewrites) == 1 and len(model.audits) == 2
        assert model.rewrites[0][1].get("tools") is None
        row, = [row for key, row in checkpoint["source_attribution_reviews"].items()
            if key.startswith("citation_rewrite/")]
        assert row["answer_applied"] is row["usage_applied"] is True
    finally:
        cleanup(binding)


@pytest.mark.parametrize("missing_mixed", [False, True])
def test_executor_pending_rewrite_cannot_rebuy_or_escape_via_new_marker_dispatch(db, monkeypatch, missing_mixed):
    model = Model(lose_rewrite=True, missing_mixed=missing_mixed)
    original = CORRECTED + "\n\n本次模型编码结果可以概括为完整且可靠的方法比较结论。" if missing_mixed else ORIGINAL
    cid, binding, checkpoint = setup(db, monkeypatch, model, answer=original)

    def persist(value):
        run_lifecycle.save_checkpoint(db, binding["run_id"], value)
        db.commit()

    try:
        with pytest.raises(PowerLoss):
            run(db, cid, binding, checkpoint, persist)
        checkpoint = saved(db, binding)
        intents = [row for key, row in checkpoint["source_attribution_reviews"].items()
            if key.startswith("citation_rewrite/")]
        assert intents == [{"review_dispatch": "pending"}]
        before = (len(model.rewrites), len(model.audits), len(model.scopes))
        resumed = run(db, cid, binding, checkpoint, persist)
        assert (len(model.rewrites), len(model.audits), len(model.scopes)) == before
        assert any(value.get("error_code") == "dispatch_outcome_unknown" for _, value in resumed)
        answer = next(value for kind, value in resumed if kind == "answer")
        assert answer["completed"] is False and answer["outcome"] == "blocked"
        assert any(row.get("reason") == "citation_review_unresolved" for row in answer["incomplete_steps"])
        assert answer["refusal"] == 1 and len(model.rewrites) == 1
        checkpoint = saved(db, binding)
        rewrite_rows = [row for key, row in checkpoint["source_attribution_reviews"].items()
            if key.startswith("citation_rewrite/")]
        assert len(rewrite_rows) == 1 and rewrite_rows[0]["error_code"] == "dispatch_outcome_unknown"
    finally:
        cleanup(binding)
