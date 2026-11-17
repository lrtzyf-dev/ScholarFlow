"""Independent B cleanup review; fixed real draft and synthetic executor replies."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import pytest

from app.agent import audit, citation_local_cleanup as cleanup
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from app.rag.citation import citation_gate, parse_claims

OUT = Path(__file__).resolve().parents[2] / "verification/d04-cleanup-independent-current/receipts"
REAL_REPORT = Path(__file__).resolve().parents[2] / "verification/real-model-20261116/current-marker-feedback-control10/acceptance-report.json"
REAL_SHA = "5181485866a21b50b55e185df6dc03e8381700eb937566b90dd14b2e2440c4df"
EVIDENCE = [{"id": "e1", "paper_title": "Independent synthetic source", "quote": "20 queries, 12 hits."}]
GOOD = "固定样本共有20条合成查询[^e1]。"
BAD = "独立反例错误结果为999项[^e1]。"
TAIL = "已报告其中12条检索命中[^e1]。"
KEY = "9" * 64


def save(kind, value):
    from uuid import uuid4
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / (kind + "-" + uuid4().hex + ".json")).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def claims(text, evidence=EVIDENCE):
    return [(item["claim"], tuple(item["evidence_ids"])) for item in parse_claims(text, [e["id"] for e in evidence])]


class CurrentAuditor:
    mode = "api"
    def chat(self, request, **options):
        pairs = re.findall(r"\[论断 (\d+)\] ([^\r\n]+)", request[-1]["content"])
        return LLMResponse(json.dumps({"results": [{"index": int(index), "supported": "999项" not in text,
            "reason": "Synthetic current complete source verdict", "support_kind": "source",
            "process_scope": "none", "supports": []} for index, text in pairs]}))


def current_audit(text):
    return audit.audit_answer(None, text, deepcopy(EVIDENCE), llm=CurrentAuditor(), messages=[],
        question="仅用于独立机械反例核对")


def test_real_control10_current_false_sentence_is_exactly_reversible_and_all_retained_claims_are_identical():
    raw = REAL_REPORT.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == REAL_SHA
    turn = json.loads(raw)["cases"][0]["result"]["turns"][0]
    original = turn["public_citation_drafts"][-1]["answer"]
    verdict = [event["audit"] for event in turn["diagnostics"]["events"] if "audit" in event][-1]
    evidence = turn["evidence"]
    assert verdict["known_failed_count"] == 1 and verdict["unknown_count"] == 0
    assert cleanup.rejected_line_candidate(original, evidence, verdict) is None
    state = AgentState(question="保留完整原任务", answer=original, evidence=deepcopy(evidence),
        provider_context={"synthetic_opaque": "RETAIN_UNTIL_ADOPTION"})
    durable = []
    result = cleanup.apply_rejected_sentence_cleanup(state, verdict, input_key=KEY,
        persist=lambda: durable.append(deepcopy(state.snapshot())), before_review=lambda: None)
    assert result["changed"] and result["removed_sentence_count"] == 1
    receipt = state.source_attribution_reviews["citation_local_cleanup/" + KEY]
    deletion = receipt["removals"][0]
    assert original[deletion["start"]:deletion["end"]] == deletion["text"]
    assert state.answer == original[:deletion["start"]] + original[deletion["end"]:]
    assert state.answer[:deletion["start"]] + deletion["text"] + state.answer[deletion["start"]:] == original
    rejected = {item["text"] for item in verdict["unsupported"]}
    assert claims(state.answer, evidence) == [item for item in claims(original, evidence) if item[0] not in rejected]
    assert citation_gate(state.answer, [e["id"] for e in evidence])["unsupported"] == citation_gate(original, [e["id"] for e in evidence])["unsupported"]
    for value in ("20 条", "12/20", "60%", "5 个百分点", "报告样本量与不确定性", "[^e1]:"):
        assert value in state.answer
    assert len(durable) == 1 and state.provider_context == {} and receipt["semantic_verification"] == "not_performed"
    save("real-control10", {"frozen_report_sha256": REAL_SHA, "original_answer_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "adopted_answer_sha256": hashlib.sha256(state.answer.encode()).hexdigest(),
        "removed_span": {key: deletion[key] for key in ("start", "end")},
        "retained_claims_and_evidence_order_exact": True, "removed_sentence_count": 1,
        "restoring_recorded_span_reconstructs_entire_original_answer": True, "semantic_verification": "not_performed"})


@pytest.mark.parametrize("bad", ["“" + BAD + "”", "「" + BAD + "」", "（" + BAD + "）",
    "**" + BAD + TAIL + "**", "__" + BAD + TAIL + "__", "**" + BAD + "\n" + TAIL + "**"])
def test_false_sentence_inside_quotes_or_cross_sentence_emphasis_cannot_be_cut(bad):
    text = GOOD + "\n\n" + bad
    verdict = current_audit(text)
    assert verdict["known_failed_count"] >= 1
    assert cleanup.rejected_sentence_candidate(text, EVIDENCE, verdict) is None


def test_no_cleanup_can_remove_an_error_if_it_would_change_uncited_neighbor_inheritance():
    text = GOOD + "\n\n" + BAD + "相邻未独立引注的具体结果仍需要核查其来源范围。"
    before = citation_gate(text, ["e1"])
    candidate = cleanup.rejected_sentence_candidate(text, EVIDENCE, current_audit(text))
    if candidate is not None:
        after = citation_gate(candidate["answer"], ["e1"])
        rejected = claims(BAD)
        assert claims(candidate["answer"]) == [item for item in claims(text) if item not in rejected]
        assert after["unsupported"] == before["unsupported"] and after["invalid_markers"] == before["invalid_markers"]


@pytest.mark.parametrize("first", ["line", "sentence"])
def test_real_old_and_new_cleanup_receipts_share_one_slot_after_json_restart(first):
    original = GOOD + "\n\n" + BAD if first == "line" else GOOD + BAD
    state = AgentState(question="同一用户修订", answer=original, evidence=deepcopy(EVIDENCE))
    adopt = cleanup.apply_rejected_line_cleanup if first == "line" else cleanup.apply_rejected_sentence_cleanup
    durable = []
    assert adopt(state, current_audit(original), input_key=KEY,
        persist=lambda: durable.append(deepcopy(state.snapshot())), before_review=lambda: None)["changed"]
    restored = AgentState.model_validate(json.loads(json.dumps(durable[-1])))
    restored.answer = GOOD + "\n\n" + BAD
    unchanged = deepcopy(restored.snapshot())
    for attempt in (cleanup.apply_rejected_line_cleanup, cleanup.apply_rejected_sentence_cleanup):
        result = attempt(restored, current_audit(restored.answer), input_key=KEY,
            persist=lambda: pytest.fail("A recovered occupied cleanup slot saved a second deletion"), before_review=lambda: None)
        assert result == {"changed": False, "reason": "cleanup_already_claimed"}
    assert restored.snapshot() == unchanged
    assert len(durable) == 1


def test_sentence_adoption_save_failure_restores_original_answer_provider_state_and_entire_journal():
    state = AgentState(question="独立回滚", answer=GOOD + BAD, evidence=deepcopy(EVIDENCE),
        provider_context={"private_fixture": "restore-me"}, source_attribution_reviews={"prior-safe": {"kept": True}})
    before = deepcopy(state.snapshot())
    def fail():
        raise OSError("synthetic uncommitted checkpoint failure")
    with pytest.raises(OSError):
        cleanup.apply_rejected_sentence_cleanup(state, current_audit(state.answer), input_key=KEY,
            persist=fail, before_review=lambda: None)
    assert state.snapshot() == before


def test_after_commit_cancel_restores_saved_sentence_without_a_second_adoption():
    state = AgentState(question="独立提交后中断", answer=GOOD + BAD, evidence=deepcopy(EVIDENCE))
    durable, checks = [], []
    def cancel_after_saved():
        checks.append(True)
        if durable:
            raise KeyboardInterrupt("synthetic cancellation after commit")
    with pytest.raises(KeyboardInterrupt):
        cleanup.apply_rejected_sentence_cleanup(state, current_audit(state.answer), input_key=KEY,
            persist=lambda: durable.append(deepcopy(state.snapshot())), before_review=cancel_after_saved)
    restored = AgentState.model_validate(json.loads(json.dumps(durable[-1])))
    assert restored.answer == GOOD and restored.snapshot() == state.snapshot()
    assert len(checks) == 2
    result = cleanup.apply_rejected_sentence_cleanup(restored, current_audit(restored.answer), input_key=KEY,
        persist=lambda: pytest.fail("Cancelled committed cleanup was adopted twice"), before_review=lambda: None)
    assert not result["changed"] and result["reason"] == "cleanup_already_claimed"


@pytest.mark.parametrize("change", ["uncounted_answer_heading", "evidence_quote_same_ids", "audit_failure_reason"])
def test_a_new_bound_current_audit_cannot_authorize_cleanup_after_its_inputs_or_verdict_change(change):
    from app.agent.citation_context_preservation import AUDIT_BINDING_FIELD
    original = GOOD + BAD
    evidence = deepcopy(EVIDENCE)
    verdict = current_audit(original)
    assert AUDIT_BINDING_FIELD in verdict, "The real auditor must create the current-input binding"
    text = original
    if change == "uncounted_answer_heading":
        text = "# 独立新增未被本次审计读取的要求\n\n" + original
    elif change == "evidence_quote_same_ids":
        evidence[0]["quote"] = "The corrected source explicitly reports 999 outcomes."
    else:
        verdict["unsupported"][0]["reason"] = "Substituted conclusion not returned by the actual current audit"
    assert claims(text, evidence) == claims(original)
    result = cleanup.rejected_sentence_candidate(text, evidence, verdict)
    save("bound-audit-" + change, {"mutation": change, "parser_claims_unchanged": True,
        "actual_auditor_binding_present": True, "candidate_generated_after_binding_mismatch": result is not None,
        "semantic_verification": result.get("semantic_verification") if result else None})
    assert result is None, "A current-audit binding mismatch still authorized deleting source claims"


def test_deleted_required_method_detail_still_fails_real_executor_quality_with_one_cleanup_receipt(db, monkeypatch):
    from app import run_lifecycle
    from app.agent import source_attribution
    from tests.test_citation_answer_rewrite_execution_review import Model, setup, run, saved, cleanup as finish
    from tests.test_rewritten_quality_execution import preserve_known_review
    kept = "该固定来源提供了编码结构信息。[^e1]"
    wrong_method = "模型采用99个注意力头完成编码处理。[^e1]"

    class RequiredMethodModel(Model):
        def __init__(self):
            super().__init__()
            self.corrections = []
        def chat(self, request, **options):
            system = request[0]["content"]
            if "语义修订后最终答案的独立质量核验员" in system:
                self.quality_reviews.append(deepcopy(request))
                actual = json.loads(request[-1]["content"])["answer"]
                assert "99个" not in actual
                return LLMResponse(json.dumps({"quality_issues": [{"kind": "unresolved_request", "text": actual,
                    "related_text": None, "user_constraint": {"receipt_id": "question", "quote": "解释方法"},
                    "reason": "仅保留来源说明，用户要求的方法尚未给出", "confirmed": True}]}))
            if system == "你是严谨的引用审计员，只输出 JSON。":
                self.audits.append(deepcopy(request))
                pairs = re.findall(r"\[论断 (\d+)\] ([^\r\n]+)", request[-1]["content"])
                return LLMResponse(json.dumps({"results": [{"index": int(index), "supported": "99个" not in text,
                    "reason": "Synthetic exact current source verdict", "support_kind": "source", "process_scope": "none",
                    "supports": []} for index, text in pairs]}))
            self.rewrites.append(deepcopy(request))
            assert len(self.rewrites) == 1
            return LLMResponse(kept + wrong_method)
        def stream_chat(self, request, tools=None):
            self.corrections.append(deepcopy(request))
            yield "final", LLMResponse(kept)

    model = RequiredMethodModel()
    monkeypatch.setattr(source_attribution, "review_source_attribution", preserve_known_review)
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db, binding["run_id"], value)
            db.commit()
        result = next(value for kind, value in run(db, cid, binding, checkpoint, persist) if kind == "answer")
        assert result["answer"].strip() == kept and result["audit"]["status"] == "passed"
        assert result["completed"] is False and result["outcome"] == "partial"
        assert result["answer_quality"]["known_failed_count"] > 0
        snapshot = saved(db, binding)
        receipts = [row for key, row in snapshot["source_attribution_reviews"].items() if key.startswith("citation_local_cleanup/")]
        assert len(receipts) == 1 and receipts[0]["protocol"] == cleanup.SENTENCE_PROTOCOL
        assert len(model.rewrites) == 1 and len(model.quality_reviews) >= 1
        save("required-method-quality", {"run_id": binding["run_id"], "cleanup_protocol": receipts[0]["protocol"],
            "semantic_status_after_deletion": result["audit"]["status"], "completed": result["completed"],
            "outcome": result["outcome"], "quality_known_failures": result["answer_quality"]["known_failed_count"],
            "rewrite_calls": len(model.rewrites), "quality_calls": len(model.quality_reviews), "cleanup_receipts": len(receipts)})
    finally:
        finish(binding)
