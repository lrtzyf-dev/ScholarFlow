"""Exact deletion mechanics, not a substitute for semantic acceptance."""
from copy import deepcopy

import pytest

from app.agent.checkpoint import AgentState
from app.agent.citation_local_cleanup import rejected_line_candidate, apply_rejected_line_cleanup
from app.rag.citation import parse_claims


EVIDENCE = [{"id": "e1", "quote": "8 heads"}]
GOOD = "模型采用8个注意力头。[^e1]"
BAD = "表格为2行乘2列。[^e1]："
ANSWER = GOOD + "\n\n" + BAD + "\n\n保留这段用户建议。"
KEY = "a" * 64


def audit_for(answer=ANSWER, rejected=(BAD.replace("[^e1]", ""),)):
    claims = [{"text": c["claim"], "evidence_ids": c["evidence_ids"]}
              for c in parse_claims(answer, ["e1"])]
    return {"status": "failed", "passed": False, "mode": "llm", "verified": True, "claims": claims,
            "factual_claims": len(claims), "known_failed_count": len(rejected),
            "unsupported": [{"text": s, "reason": "Synthetic known negative"} for s in rejected],
            "unknown_count": 0, "unverified_count": 0, "unverified": []}


def test_exact_standalone_deletion_retains_all_other_bytes_and_citations():
    # Colon after the end-of-sentence marker is a second candidate, so the
    # actual production-like table introduction has no full stop before it.
    bad = "表格为2行乘2列（表一）[^e1]："
    answer = GOOD + "\r\n\r\n" + bad + "\r\n\r\n| 数值 |\r\n|---|\r\n| 8[^e1] |\r\n"
    result = rejected_line_candidate(answer, EVIDENCE, audit_for(answer, [bad.replace("[^e1]", "")]))
    assert result["answer"] == answer.replace(bad + "\r\n", "", 1)
    assert result["semantic_verification"] == "not_performed"
    assert len(result["removals"]) == 1


@pytest.mark.parametrize("field,value", [("mode", "lexical"), ("verified", False),
    ("status", "unverified"), ("unknown_count", 1), ("unverified_count", 1),
    ("known_failed_count", True), ("error_code", "dispatch_outcome_unknown"),
    ("claims", []), ("factual_claims", 99), ("unknown_count", False),
    ("unverified_count", 0.0), ("factual_claims", True), ("passed", True)])
def test_incomplete_or_stale_audit_cannot_authorize_deletion(field, value):
    audit = audit_for()
    audit[field] = value
    assert rejected_line_candidate(ANSWER, EVIDENCE, audit) is None


@pytest.mark.parametrize("bad", [
    "# 不支持标题[^e1]", "| 不支持数值[^e1] |", "> 不支持引文[^e1]",
    "    缩进内容[^e1]", "`inline code`[^e1]", "[文字](https://example.test)[^e1]",
    "```\n代码[^e1]\n```", "不支持内容[^e1]\n跨行内容[^e1]",
])
def test_unsafe_markdown_blocks_are_not_locally_deleted(bad):
    answer = GOOD + "\n\n" + bad + "\n\n尾部独立建议。"
    rejected = [c["claim"] for c in parse_claims(bad, ["e1"])]
    assert rejected_line_candidate(answer, EVIDENCE, audit_for(answer, rejected)) is None


def test_whole_line_with_a_supported_or_uncited_sentence_is_retained():
    for bad in ["错误句。[^e1]" + GOOD, "错误句。[^e1]这是未审查事实。"]:
        answer = GOOD + "\n\n" + bad + "\n\n建议。"
        assert rejected_line_candidate(answer, EVIDENCE, audit_for(answer, ["错误句。"])) is None


def test_duplicate_claims_and_duplicate_verdicts_cannot_be_removed():
    bad = "错误句。[^e1]"
    answer = GOOD + "\n\n" + bad + "\n\n" + bad
    assert rejected_line_candidate(answer, EVIDENCE, audit_for(answer, ["错误句。"])) is None
    answer = GOOD + "\n\n" + bad
    assert rejected_line_candidate(answer, EVIDENCE, audit_for(answer, ["错误句。", "错误句。"])) is None


def test_deleting_an_intro_cannot_change_retained_list_inheritance():
    bad = "错误引言。[^e1]"
    answer = "独立建议。\n\n" + bad + "\n\n- 列表的事实\n\n" + GOOD
    assert rejected_line_candidate(answer, EVIDENCE, audit_for(answer, ["错误引言。"])) is None


def test_flat_explicitly_cited_list_item_can_be_removed_without_touching_neighbors():
    answer = "- " + GOOD + "\n- 错误句。[^e1]\n- 保留另一事实。[^e1]\n"
    result = rejected_line_candidate(answer, EVIDENCE, audit_for(answer, ["错误句。"]))
    assert result["answer"] == answer.replace("- 错误句。[^e1]\n", "")


def test_no_empty_answer_or_exclusively_uncited_remainder():
    answer = "错误句。[^e1]"
    assert rejected_line_candidate(answer, EVIDENCE, audit_for(answer, ["错误句。"])) is None
    answer += "\n\n建议而已。"
    assert rejected_line_candidate(answer, EVIDENCE, audit_for(answer, ["错误句。"])) is None


def state_for_cleanup():
    answer = GOOD + "\n\n错误句。[^e1]\n\n独立建议。"
    return AgentState(question="解释方法", answer=answer, evidence=deepcopy(EVIDENCE),
                      provider_context={"private": "old replay"})


def test_persistence_failure_restores_answer_context_and_journal():
    state = state_for_cleanup()
    before = deepcopy(state.snapshot())
    def fail():
        raise OSError("checkpoint unavailable")
    with pytest.raises(OSError):
        apply_rejected_line_cleanup(state, audit_for(state.answer, ["错误句。"]),
            input_key=KEY, persist=fail, before_review=lambda: None)
    assert state.snapshot() == before


def test_cancel_after_durable_adoption_retains_original_receipt_and_no_second_cleanup():
    state = state_for_cleanup()
    audit = audit_for(state.answer, ["错误句。"])
    checkpoints = []
    def cancelled():
        if checkpoints:
            raise RuntimeError("cancel")
    with pytest.raises(RuntimeError, match="cancel"):
        apply_rejected_line_cleanup(state, audit, input_key=KEY,
            persist=lambda: checkpoints.append(deepcopy(state.snapshot())), before_review=cancelled)
    assert state.snapshot() == checkpoints[-1] and state.provider_context == {}
    assert "错误句" not in state.answer
    result = apply_rejected_line_cleanup(state, audit, input_key=KEY,
        persist=lambda: pytest.fail("saved twice"), before_review=lambda: None)
    assert result == {"changed": False, "reason": "cleanup_already_claimed"}


def test_actual_z_draft_only_removes_the_rejected_table_dimensions():
    # Public synthetic fixture in a previous real-provider evaluation; no key
    # or user document is read. Original real verdict is preserved unchanged.
    import json
    from pathlib import Path
    fixture = Path(__file__).with_name("fixtures") / "citation-local-cleanup-z.json"
    case = json.loads(fixture.read_text(encoding="utf-8"))
    answer, audit = case["answer"], case["audit"]
    result = rejected_line_candidate(answer, case["evidence"], audit)
    assert result is not None and len(result["removals"]) == 1
    assert result["removals"][0]["text"].startswith("表格为 2 行 × 2 列")
    assert "36" in result["answer"] and "Question:" in result["answer"]
