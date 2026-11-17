"""Exact sentence cleanup mechanics using a preserved real failure and fake verdicts.

No model, credentials or scientific acceptance is exercised by these tests.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from app.agent.checkpoint import AgentState
from app.agent import citation_local_cleanup as cleanup
from app.rag.citation import parse_claims


CONTROL10_REPORT_SHA256 = "5181485866a21b50b55e185df6dc03e8381700eb937566b90dd14b2e2440c4df"
CONTROL10_BAD = "这正是该记录没有提供、因而无法据其推断普遍效果的内容[^e1]。"


def control10():
    report = (Path(__file__).resolve().parents[2] / "verification" / "real-model-20261116"
              / "current-marker-feedback-control10" / "acceptance-report.json")
    raw = report.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CONTROL10_REPORT_SHA256
    turn = json.loads(raw)["cases"][0]["result"]["turns"][0]
    audit = [event["audit"] for event in turn["diagnostics"]["events"] if "audit" in event][-1]
    return turn["public_citation_drafts"][-1]["answer"], turn["evidence"], audit


def test_real_control10_removes_only_exact_bad_sentence_from_mixed_paragraph():
    answer, evidence, audit = control10()
    # Preserve the v1 conservative behavior and the original false verdict.
    assert cleanup.rejected_line_candidate(answer, evidence, audit) is None
    before = deepcopy((answer, evidence, audit))
    candidate = getattr(cleanup, "rejected_sentence_candidate", cleanup.rejected_line_candidate)
    result = candidate(answer, evidence, audit)
    assert result is not None, "The known false whole sentence is separable from the correct recommendation"
    assert result["answer"] == answer.replace(CONTROL10_BAD, "", 1)
    assert [removal["text"] for removal in result["removals"]] == [CONTROL10_BAD]
    assert result["semantic_verification"] == "not_performed"
    for retained in ("20 条", "12/20", "60%", "5 个百分点", "报告样本量与不确定性", "[^e1]:"):
        assert retained in result["answer"]
    ids = [entry["id"] for entry in evidence]
    retained_claims = [{"text": c["claim"], "evidence_ids": c["evidence_ids"]}
                       for c in parse_claims(result["answer"], ids)]
    assert retained_claims == audit["claims"][:-1]
    assert (answer, evidence, audit) == before


EVIDENCE = [{"id": "e1", "quote": "20 synthetic queries"},
            {"id": "e2", "quote": "12 of 20 retrieved a relevant item"}]
GOOD = "样本为20条合成查询[^e1]。"
BAD = "记录完全没有报告样本量[^e1]。"
TAIL = "12条检索命中[^e2]。"
KEY = "d" * 64


def claims_for(answer, evidence=EVIDENCE):
    return [{"text": c["claim"], "evidence_ids": c["evidence_ids"]}
            for c in parse_claims(answer, [e["id"] for e in evidence])]


def audit_for(answer, rejected=None, evidence=EVIDENCE):
    claims = claims_for(answer, evidence)
    rejected = rejected or [BAD.replace("[^e1]", "")]
    return {"mode": "llm", "status": "failed", "verified": True, "passed": False,
            "claims": claims, "factual_claims": len(claims), "known_failed_count": len(rejected),
            "unsupported": [{"text": text, "reason": "Complete synthetic false verdict for this exact sentence"}
                            for text in rejected],
            "unknown_count": 0, "unverified_count": 0, "unverified": []}


def mixed_answer(bad=BAD):
    return GOOD + "\n\n**一般性方法建议**：应报告不确定性。" + bad + TAIL + "\n\n保留用户建议。"


@pytest.mark.parametrize("bad", [BAD, "**记录完全没有报告样本量**[^e1]。",
    "__记录完全没有报告样本量__[^e1]。", "**记录完全没有报告样本量[^e1]。**"])
def test_exact_sentence_span_keeps_neighboring_prose_and_balanced_emphasis(bad):
    # The terminal-then-strong-close form is accepted only at the end of its
    # paragraph, avoiding the parser assigning that close to a surviving claim.
    answer = mixed_answer(bad) if not bad.endswith("**") else GOOD + "\n\n一般建议。" + bad
    rejected = [claims_for(bad)[0]["text"]]
    result = cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit_for(answer, rejected))
    assert result is not None
    assert result["answer"] == answer.replace(bad, "", 1)
    assert result["removals"][0]["text"] == bad
    assert result["cleanup_kind"] == "sentence"
    assert result["semantic_verification"] == "not_performed"
    assert claims_for(result["answer"]) == [c for c in claims_for(answer) if c["text"] not in rejected]


def test_crlf_unicode_whitespace_and_all_retained_bytes_remain_exact():
    answer = GOOD + "\r\n\r\n🙂 一般建议。  " + BAD + "  " + TAIL + "\r\n\r\n尾部建议。  \r\n"
    result = cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit_for(answer))
    assert result is not None and result["answer"] == answer.replace(BAD, "", 1)
    edit = result["removals"][0]
    assert answer[edit["start"]:edit["end"]] == BAD
    assert result["answer"].endswith("尾部建议。  \r\n")


@pytest.mark.parametrize("field,value", [("mode", "lexical"), ("status", "unverified"),
    ("verified", False), ("passed", 0), ("unknown_count", 1), ("unknown_count", False),
    ("unverified_count", False), ("unverified_count", 1), ("unverified", None),
    ("factual_claims", True), ("known_failed_count", True), ("claims", []),
    ("error_code", "dispatch_outcome_unknown"), ("unsupported", [])])
def test_partial_unknown_or_schema_invalid_verdict_does_not_delete(field, value):
    answer = mixed_answer()
    audit = audit_for(answer)
    audit[field] = value
    before = deepcopy(audit)
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit) is None
    assert audit == before


def test_stale_or_historical_verdict_is_not_a_current_authorization():
    answer = mixed_answer()
    audit = audit_for(answer)
    assert cleanup.rejected_sentence_candidate(answer.replace("20", "21"), EVIDENCE, audit) is None
    prior = {**audit, "status": "unverified", "verified": False,
             "prior_known_failures": [{"unsupported": audit["unsupported"]}]}
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, prior) is None
    assert cleanup.rejected_sentence_candidate(answer, [{"id": "e2", "quote": "different"}], audit) is None


@pytest.mark.parametrize("paragraph", [
    "**一般建议。" + BAD + "**",  # Strong emphasis crosses sentence boundary.
    "一般建议（背景。" + BAD + "）",  # Rejected sentence is inside parentheses.
    "一般建议“背景。" + BAD + "”",  # Rejected sentence is inside a quotation.
    "一般建议。`示例` " + BAD,
    "一般建议。[外链](https://example.test) " + BAD,
    "一般建议。*" + BAD + "*",
    "一般建议。\\" + BAD,
    "一般建议。" + BAD + "\n跨行延续。",
    "- 一般建议。" + BAD,
    "> 一般建议。" + BAD,
    "| 一般建议。" + BAD + " |",
    "# 一般建议。" + BAD,
    "一般建议。" + BAD + " **未闭合",
])
def test_ambiguous_markup_or_nonordinary_paragraph_is_noop(paragraph):
    answer = GOOD + "\n\n" + paragraph + "\n\n" + TAIL
    audit = audit_for(answer)
    # Use the actual parsed claim where markup changes its literal spelling;
    # the refusal must stem from the source-span boundary, not a stale verdict.
    matches = [c["text"] for c in audit["claims"] if "完全没有报告样本量" in c["text"]]
    audit["unsupported"] = [{"text": text, "reason": "Synthetic false verdict"} for text in matches]
    audit["known_failed_count"] = len(matches)
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit) is None


def test_duplicate_claim_partial_clause_and_incomplete_failed_set_are_noop():
    answer = mixed_answer(BAD + BAD)
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit_for(answer)) is None
    answer = mixed_answer()
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit_for(answer, ["没有报告样本量"])) is None
    unsafe = "| 另一个错误[^e1]。 |"
    answer += "\n\n" + unsafe
    audit = audit_for(answer, [BAD.replace("[^e1]", ""), claims_for(unsafe)[0]["text"]])
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit) is None


def test_deletion_cannot_change_list_inherited_citations_or_leave_no_cited_claims():
    answer = "一般建议。" + BAD + "\n\n- 保留但继承e1的事实。\n\n" + TAIL
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit_for(answer)) is None
    answer = "一般建议。" + BAD
    assert cleanup.rejected_sentence_candidate(answer, EVIDENCE, audit_for(answer)) is None


def test_answer_bound_is_checked_before_parsing(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Do not parse an over-limit answer")
    monkeypatch.setattr(cleanup, "parse_claims", forbidden)
    assert cleanup.rejected_sentence_candidate("x" * 200_001, EVIDENCE, {}) is None


@pytest.mark.parametrize("invalid_key", [None, "", "a", True, "D" * 64])
def test_invalid_input_identity_does_not_adopt_or_save(invalid_key):
    state = state_for_cleanup()
    before = deepcopy(state.snapshot())
    result = cleanup.apply_rejected_sentence_cleanup(state, audit_for(state.answer), input_key=invalid_key,
        persist=lambda: pytest.fail("Invalid identity cannot save"), before_review=lambda: None)
    assert result == {"changed": False, "reason": "invalid_input_identity"}
    assert state.snapshot() == before


def state_for_cleanup():
    return AgentState(question="解释样本与限制", answer=mixed_answer(), evidence=deepcopy(EVIDENCE),
                      provider_context={"private": "old replay"})


def test_sentence_adoption_uses_shared_once_slot_and_has_no_model_interface():
    state = state_for_cleanup()
    original = state.answer
    checkpoints, checks = [], []
    result = cleanup.apply_rejected_sentence_cleanup(state, audit_for(original), input_key=KEY,
        persist=lambda: checkpoints.append(deepcopy(state.snapshot())), before_review=lambda: checks.append(True))
    assert result == {"changed": True, "removed_sentence_count": 1, "cleanup_kind": "sentence",
                      "semantic_verification": "not_performed"}
    assert state.answer == original.replace(BAD, "", 1) and state.provider_context == {}
    assert len(checkpoints) == 1 and len(checks) == 2
    receipt = state.source_attribution_reviews["citation_local_cleanup/" + KEY]
    assert receipt["protocol"] == cleanup.SENTENCE_PROTOCOL
    assert receipt["semantic_verification"] == "not_performed" and receipt["removals"][0]["text"] == BAD
    for apply in (cleanup.apply_rejected_line_cleanup, cleanup.apply_rejected_sentence_cleanup):
        assert apply(state, audit_for(original), input_key=KEY,
            persist=lambda: pytest.fail("Saved twice"), before_review=lambda: None) == {
                "changed": False, "reason": "cleanup_already_claimed"}


@pytest.mark.parametrize("occupied", [{"protocol": cleanup.PROTOCOL}, {}, None])
def test_legacy_or_malformed_occupied_slot_never_opens_a_second_cleanup(occupied):
    state = state_for_cleanup()
    state.source_attribution_reviews["citation_local_cleanup/" + KEY] = occupied
    before = deepcopy(state.snapshot())
    result = cleanup.apply_rejected_sentence_cleanup(state, audit_for(state.answer), input_key=KEY,
        persist=lambda: pytest.fail("Should not persist"), before_review=lambda: None)
    assert result == {"changed": False, "reason": "cleanup_already_claimed"}
    assert state.snapshot() == before


def test_sentence_persistence_failure_rolls_back_every_state_field():
    state = state_for_cleanup()
    before = deepcopy(state.snapshot())
    def fail():
        raise OSError("checkpoint unavailable")
    with pytest.raises(OSError, match="checkpoint unavailable"):
        cleanup.apply_rejected_sentence_cleanup(state, audit_for(state.answer), input_key=KEY,
            persist=fail, before_review=lambda: None)
    assert state.snapshot() == before


def test_sentence_cancel_before_adoption_or_after_durable_adoption_is_safe():
    state = state_for_cleanup()
    before = deepcopy(state.snapshot())
    def cancel():
        raise RuntimeError("cancel")
    with pytest.raises(RuntimeError, match="cancel"):
        cleanup.apply_rejected_sentence_cleanup(state, audit_for(state.answer), input_key=KEY,
            persist=lambda: pytest.fail("Saved after cancellation"), before_review=cancel)
    assert state.snapshot() == before
    checkpoints = []
    def cancel_after_save():
        if checkpoints:
            raise RuntimeError("cancel")
    with pytest.raises(RuntimeError, match="cancel"):
        cleanup.apply_rejected_sentence_cleanup(state, audit_for(state.answer), input_key=KEY,
            persist=lambda: checkpoints.append(deepcopy(state.snapshot())), before_review=cancel_after_save)
    assert state.snapshot() == checkpoints[-1] and state.answer == before["answer"].replace(BAD, "", 1)
    assert cleanup.apply_rejected_sentence_cleanup(state, audit_for(before["answer"]), input_key=KEY,
        persist=lambda: pytest.fail("Saved again"), before_review=lambda: None) == {
            "changed": False, "reason": "cleanup_already_claimed"}
