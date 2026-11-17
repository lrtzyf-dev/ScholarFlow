"""Independent review-policy migration counterexamples; zero provider traffic.

Legacy keys reproduce the shipped protocol-6 hash exactly. Synthetic model
verdicts test cache/dispatch mechanics, not semantic quality or human acceptance.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from app.agent import loop, source_attribution as subject
from app.agent.checkpoint import AgentState
from app.agent.model_review import model_review_identity
from app.llm import LLMResponse
from app.models import Conversation
from tests.test_citation_repair_review import _current_checkpoint


QUESTION = "用两句话解释，不要检索材料。"
ANSWER = "第一句是独立说明。第二句是独立说明。"
USAGE = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}


class Model:
    mode = "api"

    def __init__(self):
        self.calls = []
        self._runtime_config = {"mode": "api", "base_url": "https://contract-review.example.test/v1",
            "model_id": "Synthetic-Review-Policy-Model"}

    def chat(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        data = json.loads(messages[1]["content"])
        return LLMResponse(json.dumps({"source_required": False, "quality_issues": [],
            "results": [{"index": block["index"], "kind": "independent", "claims": []}
                for block in data["blocks"]]}), usage=USAGE)

    def stream_chat(self, *args, **kwargs):
        pytest.fail("Review policy migration cannot buy a generation/repair")
        yield  # pragma: no cover


def legacy_key(state, model, *, protocol=6):
    # Exact historical execution.py contract: actual instructions are absent.
    return hashlib.sha256(json.dumps({"protocol": protocol, "question": state.question,
        "answer": state.answer, "messages": state.messages,
        "excluded_indices": state.source_attribution_feedback_indices,
        "runtime_receipt": subject.execution_receipt(state),
        "runtime_configuration": subject.configuration_receipt(state),
        "review_model_identity": model_review_identity(model)},
        ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def completed_legacy_review():
    return {"mode": "source_attribution", "status": "not_applicable", "passed": False,
        "verified": True, "unsupported": [], "claims": [], "unverified_count": 0,
        "requirement_missing": False, "quality_status": "passed", "quality_verified": True,
        "quality_issues": [], "reason": "independent_content"}


def fixture(db, monkeypatch):
    cid = "source-review-contract"
    db.add(Conversation(id=cid)); db.commit()
    model = Model()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False})
    state = AgentState(question=QUESTION, conversation_id=cid, phase="finalize", answer=ANSWER,
        runtime_config={"mode": "api", "model_id": "synthetic"},
        messages=[{"role": "user", "content": QUESTION}])
    _current_checkpoint(db, state)
    return model, state


def drive(db, model, state):
    saved = []
    events = list(loop.run_agent(db, QUESTION, conversation_id=state.conversation_id,
        checkpoint=state.snapshot(), save_checkpoint=lambda value: saved.append(deepcopy(value))))
    answer = next(value for kind, value in events if kind == "answer")
    return answer, saved


def test_legacy_known_success_is_rechecked_once_under_actual_current_review_policy(db, monkeypatch):
    model, state = fixture(db, monkeypatch)
    old_key = legacy_key(state, model)
    state.source_attribution_reviews[old_key] = completed_legacy_review()
    answer, saved = drive(db, model, state)
    assert len(model.calls) == 1, "An unversioned old success silently bypassed the new review instructions"
    instructions = model.calls[0][0]["content"]
    assert "字数和句数限制覆盖完整可见答案" in instructions
    assert "似然" in instructions and "尾概率" in instructions and "后验" in instructions
    assert answer["completed"] and answer["cumulative_usage"] == USAGE
    checkpoint = next(value for value in reversed(saved) if value["phase"] == "finalize")
    restored = AgentState.model_validate(checkpoint)
    second, _ = drive(db, model, restored)
    assert len(model.calls) == 1 and second["cumulative_usage"] == USAGE


@pytest.mark.parametrize("old_outcome", ["pending", "dispatch_unknown"])
def test_protocol_upgrade_cannot_buy_a_second_unknown_legacy_dispatch(db, monkeypatch, old_outcome):
    model, state = fixture(db, monkeypatch)
    old_key = legacy_key(state, model)
    state.source_attribution_reviews[old_key] = ({"review_dispatch": "pending"} if old_outcome == "pending"
        else {"mode": "source_attribution", "status": "unverified", "verified": False,
            "passed": False, "error_code": "dispatch_outcome_unknown", "unsupported": [], "claims": [],
            "unverified_count": 1, "quality_status": "unverified", "quality_issues": []})
    monkeypatch.setattr(subject, "REVIEW_PROTOCOL", 7)
    answer, saved = drive(db, model, state)
    assert model.calls == [], "A protocol upgrade silently repurchased an unknown charged review"
    assert answer["source_attribution"]["error_code"] == "dispatch_outcome_unknown"
    assert answer["source_attribution"]["verified"] is False


def test_source_repair_allowance_ignores_review_protocol_upgrade_and_preserves_legacy_hash(monkeypatch):
    history = [{"role": "user", "content": QUESTION}]
    old = subject.repair_context_key(QUESTION, history, input_ids=["actual-user-input"], source_revision_ids=["fixed-source"])
    monkeypatch.setattr(subject, "REVIEW_PROTOCOL", 999)
    assert subject.repair_context_key(QUESTION, history, input_ids=["actual-user-input"],
        source_revision_ids=["fixed-source"]) == old, "Unrelated review policy changed the source-repair allowance"
    assert old == "b1d56317c25a2f323f04ad74c00df02bccd9cbb45256c28c85540e2b7186d9d2"


def test_quality_repair_allowance_already_ignores_review_protocol_upgrade(monkeypatch):
    history = [{"role": "user", "content": QUESTION}]
    old = subject.quality_repair_context_key(QUESTION, history, input_ids=["actual-user-input"])
    monkeypatch.setattr(subject, "REVIEW_PROTOCOL", 999)
    assert subject.quality_repair_context_key(QUESTION, history, input_ids=["actual-user-input"]) == old


def test_same_current_review_contract_reuses_known_result_without_new_call(db, monkeypatch):
    model, state = fixture(db, monkeypatch)
    first, saved = drive(db, model, state)
    assert len(model.calls) == 1 and first["completed"]
    checkpoint = next(value for value in reversed(saved) if value["phase"] == "finalize")
    restored = AgentState.model_validate(checkpoint)
    second, _ = drive(db, model, restored)
    assert len(model.calls) == 1 and second["completed"]
    assert second["cumulative_usage"] == first["cumulative_usage"] == USAGE


@pytest.mark.parametrize("changed", ["instructions", "schema", "answer_limit", "context_limit"])
def test_contract_identity_tracks_actual_policy_schema_and_limits_without_resetting_repairs(monkeypatch, changed):
    before = subject.review_contract_id()
    history = [{"role": "user", "content": QUESTION}]
    repair = subject.repair_context_key(QUESTION, history, input_ids=["user-input"], source_revision_ids=["source-version"])
    instructions = subject.review_instructions()
    if changed == "instructions":
        monkeypatch.setattr(subject, "review_instructions", lambda: instructions + " New synthetic review criterion.")
    elif changed == "schema":
        monkeypatch.setattr(subject, "REVIEW_PROTOCOL", 999)
    elif changed == "answer_limit":
        monkeypatch.setattr(subject, "MAX_ANSWER_CHARS", subject.MAX_ANSWER_CHARS + 1)
    else:
        monkeypatch.setattr(subject, "MAX_CONTEXT_CHARS", subject.MAX_CONTEXT_CHARS + 1)
    assert subject.review_contract_id() != before
    assert subject.review_contract_id() == subject.review_contract_id()
    assert subject.repair_context_key(QUESTION, history, input_ids=["user-input"], source_revision_ids=["source-version"]) == repair


def test_actual_instruction_change_rechecks_known_current_result_once(db, monkeypatch):
    model, state = fixture(db, monkeypatch)
    _, saved = drive(db, model, state)
    checkpoint = next(value for value in reversed(saved) if value["phase"] == "finalize")
    old_instructions = subject.review_instructions()
    monkeypatch.setattr(subject, "review_instructions", lambda: old_instructions + " Additional synthetic policy sentence.")
    second, saved = drive(db, model, AgentState.model_validate(checkpoint))
    assert len(model.calls) == 2 and model.calls[-1][0]["content"].endswith("Additional synthetic policy sentence.")
    assert second["cumulative_usage"] == {key: value * 2 for key, value in USAGE.items()}
    checkpoint = next(value for value in reversed(saved) if value["phase"] == "finalize")
    third, _ = drive(db, model, AgentState.model_validate(checkpoint))
    assert len(model.calls) == 2 and third["cumulative_usage"] == second["cumulative_usage"]


def invoke(journal, *, contract="b" * 64, review=None, persist=None,
           before_review=None, allow_dispatch=True):
    from app.agent.source_review_journal import cached_source_review
    return cached_source_review(journal, "a" * 64, contract_id=contract,
        review=review or completed_legacy_review, persist=persist or (lambda: None),
        before_review=before_review or (lambda: None), allow_dispatch=allow_dispatch)


def test_legacy_known_result_remains_immutable_and_each_actual_contract_dispatches_once():
    legacy = completed_legacy_review()
    journal, calls = {"a" * 64: deepcopy(legacy)}, []
    def review():
        calls.append(1)
        return completed_legacy_review()
    invoke(journal, review=review)
    invoke(journal, review=review)
    invoke(journal, contract="c" * 64, review=review)
    invoke(journal, contract="c" * 64, review=review)
    assert calls == [1, 1] and journal["a" * 64] == legacy
    assert len([key for key in journal if key.startswith("source_review/")]) == 2


@pytest.mark.parametrize("place", ["legacy", "prior_policy", "current_policy"])
@pytest.mark.parametrize("outcome", ["pending", "dispatch_unknown"])
def test_any_unknown_family_member_fences_even_an_already_known_current_contract(place, outcome):
    legacy = "a" * 64
    current = "source_review/" + legacy + "/" + "b" * 64
    old = legacy if place == "legacy" else "source_review/" + legacy + "/" + ("c" * 64 if place == "prior_policy" else "b" * 64)
    journal = {current: completed_legacy_review()}
    journal[old] = ({"review_dispatch": "pending"} if outcome == "pending"
        else {"status": "unverified", "error_code": "dispatch_outcome_unknown", "verified": False})
    saved = []
    result = invoke(journal, review=lambda: pytest.fail("Unknown family dispatch was repurchased"),
        persist=lambda: saved.append(deepcopy(journal)))
    assert result["error_code"] == "dispatch_outcome_unknown"
    assert journal[old]["error_code"] == "dispatch_outcome_unknown"
    assert len(saved) == (1 if outcome == "pending" else 0)


@pytest.mark.parametrize("entry", [None, "corrupt", {"bad-suffix": {}}])
def test_corrupted_family_cannot_authorize_dispatch(entry):
    key = "a" * 64 if entry != {"bad-suffix": {}} else "source_review/" + "a" * 64 + "/bad-suffix"
    journal = {key: entry}
    result = invoke(journal, review=lambda: pytest.fail("Corrupt family caused a new charge"))
    assert result["error_code"] == "journal_invalid"


def test_current_contract_intent_is_durable_before_dispatch_and_result_before_return():
    journal, saved = {}, []
    def review():
        key, = saved[-1]
        assert key == "source_review/" + "a" * 64 + "/" + "b" * 64
        assert saved[-1][key] == {"review_dispatch": "pending"}
        return completed_legacy_review()
    result = invoke(journal, review=review, persist=lambda: saved.append(deepcopy(journal)))
    assert len(saved) == 2 and result["quality_status"] == "passed"
    assert next(iter(saved[-1].values())) == result


@pytest.mark.parametrize("legacy", [False, True])
def test_no_dispatch_budget_does_not_save_or_accept_missing_current_policy_and_can_resume(legacy):
    journal = {"a" * 64: completed_legacy_review()} if legacy else {}
    original, calls, saved = deepcopy(journal), [], []
    def review():
        calls.append(1)
        return completed_legacy_review()
    result = invoke(journal, allow_dispatch=False, review=review,
        persist=lambda: saved.append(deepcopy(journal)))
    assert result["verified"] is False and result["quality_verified"] is False
    assert result["reason"] == "run_budget_exhausted"
    assert journal == original and not calls and not saved
    invoke(journal, review=review, persist=lambda: saved.append(deepcopy(journal)))
    assert calls == [1] and len(saved) == 2


@pytest.mark.parametrize("entry", ["known", "pending", "dispatch_unknown"])
def test_exhausted_budget_can_reuse_known_or_unknown_current_result_without_dispatch(entry):
    key = "source_review/" + "a" * 64 + "/" + "b" * 64
    value = completed_legacy_review() if entry == "known" else ({"review_dispatch": "pending"}
        if entry == "pending" else {"status": "unverified", "error_code": "dispatch_outcome_unknown", "verified": False})
    journal = {key: value}
    result = invoke(journal, allow_dispatch=False, review=lambda: pytest.fail("Budget exhausted but review dispatched"))
    assert result["quality_status"] == "passed" if entry == "known" else result["error_code"] == "dispatch_outcome_unknown"


@pytest.mark.parametrize("entry", ["known", "pending", "dispatch_unknown"])
@pytest.mark.parametrize("cancel_at", [1, 2])
def test_cached_and_unknown_paths_check_cancellation_before_and_after_reuse(entry, cancel_at):
    class Cancelled(Exception):
        pass
    key = "source_review/" + "a" * 64 + "/" + "b" * 64
    value = completed_legacy_review() if entry == "known" else ({"review_dispatch": "pending"}
        if entry == "pending" else {"status": "unverified", "error_code": "dispatch_outcome_unknown", "verified": False})
    journal, checks, saved = {key: value}, [], []
    def check():
        checks.append(1)
        if len(checks) == cancel_at:
            raise Cancelled()
    with pytest.raises(Cancelled):
        invoke(journal, review=lambda: pytest.fail("Cache reuse dispatched a new model request"),
            before_review=check, persist=lambda: saved.append(deepcopy(journal)))
    assert len(checks) == cancel_at
    if entry == "pending" and cancel_at == 2:
        assert saved[-1][key]["error_code"] == "dispatch_outcome_unknown"


def test_review_policy_upgrade_does_not_change_existing_citation_rewrite_context(monkeypatch):
    from app.agent.citation_answer_rewrite import apply_citation_answer_rewrite
    model, saved, calls = Model(), [], []
    state = AgentState(question=QUESTION, phase="finalize", answer=ANSWER,
        messages=[{"role": "user", "content": QUESTION}], evidence=[{"id": "e1", "quote": "fixed source"}])
    def identity():
        return {"observations": subject.repair_context_key(QUESTION, state.messages,
            input_ids=["actual-input"], source_revision_ids=["fixed-version"]), "memory_fingerprint": "fixed-memory"}
    def rewrite():
        calls.append(1)
        return LLMResponse("A revised synthetic answer.[^e1]", usage=USAGE)
    arguments = {"llm": model, "request_messages": [{"role": "user", "content": "Fixed rewrite request"}],
        "call": rewrite, "persist": lambda: saved.append(deepcopy(state.snapshot())), "before_review": lambda: None}
    first = apply_citation_answer_rewrite(state, input_identity=identity(), **arguments)
    assert first["changed"] and len(calls) == 1
    instructions = subject.review_instructions()
    monkeypatch.setattr(subject, "review_instructions", lambda: instructions + " New review policy.")
    monkeypatch.setattr(subject, "REVIEW_PROTOCOL", 999)
    second = apply_citation_answer_rewrite(state, input_identity=identity(), **arguments)
    assert second["status"] == "already_applied" and len(calls) == 1 and state.usage == USAGE


@pytest.mark.parametrize("failed_save", [1, 2])
def test_persist_failure_restores_journal_to_last_durable_state_and_never_rebuys_unknown(failed_save):
    journal, saved, saves, calls = {}, [], [], []
    def review():
        calls.append(1)
        return completed_legacy_review()
    def persist():
        saves.append(1)
        if len(saves) == failed_save:
            raise OSError("Synthetic source review checkpoint failure")
        saved.append(deepcopy(journal))
    with pytest.raises(OSError):
        invoke(journal, review=review, persist=persist)
    assert journal == (saved[-1] if saved else {})
    if failed_save == 1:
        assert not calls and not journal
        assert invoke(journal, review=review)["quality_status"] == "passed" and calls == [1]
    else:
        assert calls == [1] and next(iter(journal.values())) == {"review_dispatch": "pending"}
        result = invoke(journal, review=lambda: pytest.fail("Failed result save repurchased the review"))
        assert result["error_code"] == "dispatch_outcome_unknown" and calls == [1]


def test_failed_pending_to_unknown_save_retains_pending_and_next_recovery_never_dispatches():
    key = "source_review/" + "a" * 64 + "/" + "b" * 64
    journal = {key: {"review_dispatch": "pending"}}
    original = deepcopy(journal)
    def failed():
        raise OSError("Synthetic unknown-state checkpoint failure")
    with pytest.raises(OSError):
        invoke(journal, persist=failed, review=lambda: pytest.fail("Pending review was dispatched"))
    assert journal == original
    result = invoke(journal, review=lambda: pytest.fail("Pending review was repurchased on next recovery"))
    assert result["error_code"] == "dispatch_outcome_unknown"


def test_successful_result_is_saved_before_post_dispatch_cancellation_and_reused_afterward():
    class Cancelled(Exception):
        pass
    journal, saved, calls = {}, [], []
    def review():
        calls.append(1)
        return completed_legacy_review()
    def check():
        if any(value.get("quality_status") == "passed" for value in journal.values()):
            raise Cancelled()
    with pytest.raises(Cancelled):
        invoke(journal, review=review, persist=lambda: saved.append(deepcopy(journal)), before_review=check)
    assert len(saved) == 2 and saved[-1] == journal and calls == [1]
    result = invoke(journal, review=lambda: pytest.fail("Saved successful review was repurchased"))
    assert result["quality_status"] == "passed" and calls == [1]
