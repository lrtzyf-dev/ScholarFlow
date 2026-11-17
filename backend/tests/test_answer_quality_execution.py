"""Quality corrections preserve user constraints without inventing source work."""
from copy import deepcopy

import pytest

from app.agent import loop, source_attribution
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse


QUESTION = "只有两个晚上有空，请安排运动和做饭。"
BAD = "第三个晚上再运动。"
GOOD = "第一晚运动，第二晚做饭；下雨则在第一晚室内徒手运动。"
ISSUE = {"kind": "constraint_violation", "text": BAD, "related_text": None,
    "user_constraint": {"receipt_id": "question", "quote": "只有两个晚上有空"},
    "reason": "增加了第三晚", "confirmed": True, "answer_spans": [{"start": 0, "end": len(BAD)}]}


def verdict(quality="passed", *, material=False):
    return {"mode": "source_attribution", "status": "failed" if material else "not_applicable",
        "passed": False, "verified": True, "unsupported": [{"text": BAD}] if material else [],
        "claims": [], "unverified_count": 0, "requirement_missing": False,
        "quality_status": quality, "quality_verified": quality != "unverified",
        "quality_issues": [deepcopy(ISSUE)] if quality == "failed" else []}


def drive(db, monkeypatch, reviews, *, replacement=GOOD, repeat_read=False, seed=None):
    saved, calls, seen = [], [], []
    class Model:
        mode = "api"
        def stream_chat(self, messages, tools=None):
            assert any(r.get("repair_claimed") for r in saved[-1]["answer_quality_repairs"].values())
            seen.append(deepcopy(messages))
            if repeat_read and len(seen) == 1:
                yield "final", LLMResponse("", tool_calls=[{"id": "read_once", "name": "list_attached_sources", "arguments": {}}])
            else:
                yield "final", LLMResponse(replacement)
    def review(*args, **kwargs):
        calls.append(1)
        kwargs["on_usage"]({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return deepcopy(reviews[min(len(calls) - 1, len(reviews) - 1)])
    monkeypatch.setattr(loop, "get_llm", Model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **kw: '{"sources":[]}')
    monkeypatch.setattr(source_attribution, "review_source_attribution", review)
    state = seed or AgentState(question=QUESTION, phase="finalize", answer=BAD,
        messages=[{"role": "user", "content": QUESTION}],
        runtime_config={"mode": "api", "model_id": "synthetic"}, plan_source="requirements")
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(),
        save_checkpoint=lambda s: saved.append(deepcopy(s))))
    result = next(event for kind, event in events if kind == "answer")
    return result, saved, calls, seen


def test_quality_only_repairs_once_and_rechecks_without_source_instructions(db, monkeypatch):
    result, saved, calls, seen = drive(db, monkeypatch, [verdict("failed"), verdict()])
    assert result["completed"] and result["answer"] == GOOD and len(calls) == 2 and len(seen) == 1
    feedback = seen[0][-1]["content"]
    assert "增加了第三晚" in feedback and "日常建议和表达修正不要求补读材料或检索" in feedback
    assert "补齐真正缺少的材料依据" not in feedback
    assert "完整、可独立阅读的修订后答案" in feedback and "不要只列修正点" in feedback
    assert not saved[-1]["source_attribution_repair_contexts"]
    assert result["answer_quality"]["status"] == "passed" and result["usage"]["total_tokens"] == 4


@pytest.mark.parametrize("second", ["failed", "unverified"])
def test_known_quality_failure_is_not_erased_or_retried_by_invalid_review(db, monkeypatch, second):
    result, saved, calls, seen = drive(db, monkeypatch, [verdict("failed"), verdict(second)], replacement=BAD)
    assert len(calls) == 2 and len(seen) == 1
    assert result["completed"] is False and result["outcome"] == "partial"
    assert result["stop_reason"] == "answer_quality_unresolved" and result["refusal"] == 0
    assert result["answer"] == BAD and "材料结论" not in result["answer"]
    assert result["answer_quality"]["known_failed_count"] == 1
    assert result["incomplete_steps"][-1]["reason"] == "answer_quality_unresolved"
    assert sum(r.get("repair_claimed", False) for r in saved[-1]["answer_quality_repairs"].values()) == 1


def test_unknown_initial_quality_does_not_invent_failure_or_source_requirements(db, monkeypatch):
    result, _, calls, seen = drive(db, monkeypatch, [verdict("unverified")])
    assert result["completed"] is False and not seen and len(calls) == 1
    assert result["outcome"] == "partial"
    assert result["answer_quality"]["issues"] == []
    assert result["answer_quality"]["known_failed_count"] == 0
    assert result["answer_quality"]["status"] == "unverified"


def test_tool_results_cannot_reset_quality_repair_allowance(db, monkeypatch):
    result, saved, calls, seen = drive(db, monkeypatch, [verdict("failed")], repeat_read=True, replacement=BAD)
    assert len(calls) == 2 and len(seen) == 2 and result["completed"] is False
    assert len(saved[-1]["answer_quality_repairs"]) == 1


def test_material_and_quality_issues_share_the_same_correction_round(db, monkeypatch):
    result, saved, calls, seen = drive(db, monkeypatch, [verdict("failed", material=True), verdict()])
    assert result["completed"] and len(calls) == 2 and len(seen) == 1
    assert len(saved[-1]["source_attribution_repair_contexts"]) == 1
    assert "补齐真正缺少的材料依据" in seen[0][-1]["content"]


def test_cited_answer_cannot_hide_previously_confirmed_quality_failure(db, monkeypatch):
    state = AgentState(question=QUESTION, phase="finalize", answer=BAD + "[^e1]",
        messages=[{"role": "user", "content": QUESTION}], plan_source="requirements",
        evidence=[{"id": "e1", "quote": "source", "page": 1}],
        runtime_config={"mode": "mock", "model_id": "synthetic"})
    key = source_attribution.quality_repair_context_key(QUESTION, state.messages)
    state.answer_quality_repairs[key] = {"repair_claimed": True, "unresolved": [deepcopy(ISSUE)]}
    monkeypatch.setattr(loop, "citation_gate", lambda *a: {"passed": True, "total_claims": 1,
        "marked_claims": 1, "grounded_ratio": 1, "invalid_markers": [], "unsupported": []})
    monkeypatch.setattr(loop, "audit_answer", lambda *a, **kw: {"status": "passed", "passed": True,
        "verified": True, "mode": "lexical", "claims": [], "unsupported": []})
    result, _, calls, seen = drive(db, monkeypatch, [verdict("unverified")], seed=state)
    assert len(calls) == 1 and not seen and result["completed"] is False
    assert result["stop_reason"] == "answer_quality_unresolved"
