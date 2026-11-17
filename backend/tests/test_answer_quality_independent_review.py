"""Independent adversarial checks; synthetic review and real stage rebuild only."""
from copy import deepcopy

import pytest

from app.agent import source_attribution as subject
from app.agent.checkpoint import AgentState
from app.agent.memory_stage import rebuild_stage
from .test_answer_quality_protocol import QUESTION, ANSWER, Reviewer, issue, review


@pytest.mark.parametrize("bad_row", [{"unexpected": "row"}, issue(text="not in original answer")])
def test_valid_confirmed_issue_survives_an_invalid_neighbour(bad_row):
    value = review([issue(), bad_row])
    assert value["quality_status"] == "failed"
    assert value["quality_verified"] is False
    assert sum(row.get("confirmed") is True for row in value["quality_issues"]) == 1


def test_memory_stage_reload_cannot_reset_same_task_quality_repair(db, monkeypatch):
    """Clearing old QA memory is not a new task input or rewrite allowance."""
    from app import conversation_memory
    monkeypatch.setattr(conversation_memory, "load_history", lambda *a, **kw:
        ([], {"fingerprint": "memory-cleared-without-task-revision"}))
    state = AgentState(question=QUESTION, phase="model", messages=[
        {"role": "system", "content": "Trusted policy."},
        {"role": "user", "content": "Historical unrelated QA message."},
        {"role": "assistant", "content": "Historical reply."},
        {"role": "user", "content": QUESTION, "_task_root": True}],
        runtime_config={"mode": "mock"})
    old_key = subject.quality_repair_context_key(QUESTION, state.messages)
    state.answer_quality_repairs[old_key] = {"repair_claimed": True,
        "unresolved": [{**issue(), "answer_spans": [{"start": ANSWER.index(issue()["text"]),
                                                      "end": len(ANSWER)}]}]}
    before = deepcopy(state.answer_quality_repairs[old_key])
    rebuild_stage(db, state, None, reason="memory")
    key = subject.quality_repair_context_key(QUESTION, state.messages)
    assert key == old_key
    assert state.answer_quality_repairs[key] == before


def test_known_failure_is_not_washed_out_by_invalid_neighbour_in_executor(db, monkeypatch):
    """The review validator, not a pre-cooked verdict, feeds actual finalize."""
    from app.agent import loop
    from app.llm import LLMResponse
    requests = []
    class Model(Reviewer):
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            yield "final", LLMResponse(ANSWER)
    model = Model([issue(), {"unexpected": "row"}])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw:
        {"ran": 0, "blocked": False, "feedback": ""})
    state = AgentState(question=QUESTION, phase="finalize", answer=ANSWER,
        messages=[{"role": "user", "content": QUESTION, "_task_root": True}],
        runtime_config={"mode": "api", "model_id": "synthetic"}, plan_source="requirements")
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot()))
    value = next(value for kind, value in events if kind == "answer")
    assert requests and len(requests) == 1
    assert value["completed"] is False
    assert value["answer_quality"]["known_failed_count"] == 1
    assert value["stop_reason"] == "answer_quality_unresolved"
