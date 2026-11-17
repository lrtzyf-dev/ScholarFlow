"""Independent D05 upgrade/family contracts, with frozen old receipt code.

All chat replies below are local synthetic fixtures. No supplier, service,
credential store, or original source file is accessed.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types
from uuid import uuid4

import pytest

from app.agent import audit, citation_scope, service_process_receipts
from app.agent.paid_review_family import FIELD
from app.llm import LLMResponse
from app.rag.citation import citation_gate

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "verification/real-model-20261116/code-current-context-repair"
OUT = ROOT / "verification/d05-purpose-review-family-current/receipts"
QUESTION = "区分固定材料结果和本轮实际OCR调用的返回范围。"
EVIDENCE = [{"id": "e1", "paper_title": "Synthetic D05 input", "quote": "20 synthetic observations."}]
ANSWER = "固定材料报告20个合成观察值[^e1]。\n\n本轮OCR调用已成功返回识别文本。"


def save(kind, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / (kind + "-" + uuid4().hex + ".json")).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def payload(service="ocr", purpose="rich_layout_recognition"):
    result = {"provider": "deepseek", "integration": "independent_tool_api", "service": service,
        "call_id": "c" * 32, "status": "ok", "response_complete": True,
        "sources": [{"revision_id": "a" * 32, "raw_sha256": "b" * 64, "text_sha256": "e" * 64}],
        "result_type": "ocr_document", "page_count": 1, "truncated": False, "purpose": purpose,
        "page_statuses": [{"index": 0, "finish_reason": "stop", "truncated": False}],
        "original_revision_id": "d" * 32, "original_sha256": "b" * 64,
        "untrusted_body": "OCR SOURCE NUMBER 900 IS NOT PROCESS PROOF"}
    arguments = {"source_revision_id": "d" * 32, "purpose": purpose}
    if service == "fetch":
        result.update(result_type="web_extracted_text", returned_markdown_is_retained_raw=True)
        result["sources"][0]["origin_ref"] = "https://example.test/fixed"
        arguments = {"url": "https://example.test/fixed"}
    return result, arguments


def history(service="ocr", purpose="rich_layout_recognition"):
    result, arguments = payload(service, purpose)
    return [{"role": "user", "content": QUESTION},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "paired-d05-1", "type": "function",
            "function": {"name": "deepseek_" + service, "arguments": json.dumps(arguments)}}]},
        {"role": "tool", "tool_call_id": "paired-d05-1", "content": json.dumps(result)}]


def frozen_module(filename):
    path = FROZEN / "backend/app/agent" / filename
    manifest = json.loads((FROZEN / "evaluation-inputs.json").read_text(encoding="utf-8"))["source_sha256"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["backend/app/agent/" + filename]
    module = types.ModuleType("app.agent._d05_frozen_" + path.stem)
    module.__package__ = "app.agent"
    module.__file__ = str(path)
    # Pure in-memory execution avoids SourceFileLoader writing __pycache__
    # into the immutable evaluation snapshot.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


@contextmanager
def actual_old_implementation(monkeypatch):
    """Invoke frozen audit with its real old projector, never copied key math."""
    old_audit = frozen_module("audit.py")
    old_scope = frozen_module("citation_scope.py")
    old_service = frozen_module("service_process_receipts.py")
    assert old_scope.PROCESS_INSTRUCTIONS == citation_scope.PROCESS_INSTRUCTIONS
    with monkeypatch.context() as patch:
        # Frozen relative imports intentionally resolve the exact old receipt
        # projection during this call. Everything is restored before upgrade.
        patch.setattr(citation_scope, "process_receipts", old_scope.process_receipts)
        patch.setattr(service_process_receipts, "project_service_receipt", old_service.project_service_receipt)
        yield old_audit.review_citation_scope, old_scope.process_receipts


class LossAfterDispatch(BaseException):
    pass


class Reviewer:
    mode = "api"
    def __init__(self, *, outcome="process", fail=False, error=None, kind="process", purpose_quote=False):
        self.attempts, self.calls = 0, []
        self.outcome, self.fail, self.error, self.kind, self.purpose_quote = outcome, fail, error, kind, purpose_quote
        self._runtime_config = {"mode": "api", "model_id": "Independent-D05-Scope-Fixture",
            "base_url": "https://synthetic-d05.invalid/v1"}
    def chat(self, messages, **options):
        self.attempts += 1
        assert not self.fail, "Possibly paid old review was dispatched after upgrade"
        prompt = json.loads(messages[-1]["content"])
        self.calls.append(deepcopy(prompt))
        if self.error is not None:
            raise self.error
        if self.outcome == "invalid":
            return LLMResponse('{"results":[')
        rows = []
        for candidate in prompt["uncited_candidates"]:
            requires = self.outcome == "requires"
            kind = "source" if requires else self.kind
            quote = ('"purpose": "rich_layout_recognition"' if self.purpose_quote else '"response_complete": true')
            supports = [{"receipt_id": prompt["process_receipts"][0]["id"], "quote": quote}] if not requires else []
            rows.append({"index": candidate["index"], "requires_citation": requires,
                "reason": "Synthetic verdict bound to the actual projected contract", "kind": kind,
                "process_scope": "none" if requires else "bounded", "supports": supports})
        return LLMResponse(json.dumps({"results": rows}), usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})


def invoke(model, journal, *, function=audit.review_citation_scope, text=ANSWER,
           evidence=EVIDENCE, messages=None, persist=lambda: None):
    attempts = model.attempts
    result = function(QUESTION, text, deepcopy(evidence), citation_gate(text, [item["id"] for item in evidence]), model,
        messages=history() if messages is None else messages, review_journal=journal,
        persist=persist, before_review=lambda: None)
    if model.fail:
        # The production response handler catches AssertionError, so measure
        # attempts outside that catch rather than trusting an empty call list.
        assert model.attempts == attempts, "Production attempted a forbidden redispatch"
    return result


def mechanical_only(result, text=ANSWER, evidence=EVIDENCE):
    gate = citation_gate(text, [item["id"] for item in evidence])
    assert all(result[field] == expected for field, expected in gate.items())
    assert "scoped_review" not in result


def test_same_old_tool_messages_gain_only_new_validated_purpose_in_current_projection(monkeypatch):
    messages = history()
    with actual_old_implementation(monkeypatch) as (_, old_project):
        old_receipts, _ = old_project(messages)
    new_receipts, _ = citation_scope.process_receipts(messages)
    assert len(old_receipts) == len(new_receipts) == 1
    before, after = json.loads(old_receipts[0]["content"]), json.loads(new_receipts[0]["content"])
    assert "purpose" not in before and after.pop("purpose") == "rich_layout_recognition"
    assert before == after and old_receipts[0]["id"] == new_receipts[0]["id"]
    assert old_receipts[0]["sha256"] != new_receipts[0]["sha256"]
    assert "OCR SOURCE NUMBER" not in new_receipts[0]["content"]
    save("projection-identity", {"old_sha256": old_receipts[0]["sha256"], "new_sha256": new_receipts[0]["sha256"],
        "only_new_projected_field": "purpose", "raw_tool_messages_unchanged": True,
        "whole_extracted_text_returned": new_receipts[0]["whole_extracted_text_returned"]})


@pytest.mark.parametrize("dispatch_state", ["pending", "unknown"])
@pytest.mark.parametrize("with_family", [True, False])
@pytest.mark.parametrize("drift", ["none", "answer", "evidence", "instructions", "model", "purpose", "history"])
def test_old_ocr_pending_and_unknown_cannot_redispatch_after_purpose_upgrade_and_json_restore(monkeypatch, dispatch_state, with_family, drift):
    old_model, journal, durable = Reviewer(error=LossAfterDispatch("synthetic paid outcome unknown")), {}, []
    with actual_old_implementation(monkeypatch) as (old_review, _):
        with pytest.raises(LossAfterDispatch):
            invoke(old_model, journal, function=old_review, persist=lambda: durable.append(deepcopy(journal)))
        assert old_model.attempts == len(old_model.calls) == 1
        assert "purpose" not in json.loads(old_model.calls[0]["process_receipts"][0]["content"])
        if dispatch_state == "unknown":
            old_model.fail = True
            unknown = invoke(old_model, journal, function=old_review)
            assert unknown["error_code"] == "dispatch_outcome_unknown"
    assert durable and len(journal) == 1
    if not with_family:
        for value in journal.values(): value.pop(FIELD, None)
    keys = set(journal)
    model = Reviewer(fail=True)
    text, evidence, messages = ANSWER, deepcopy(EVIDENCE), history()
    if drift == "answer": text += "\n\n另一项来自来源的具体数字仍需核验。"
    elif drift == "evidence": evidence[0]["quote"] += " Corrected source metadata."
    elif drift == "instructions": monkeypatch.setattr(citation_scope, "PROCESS_INSTRUCTIONS", citation_scope.PROCESS_INSTRUCTIONS + " Preserve all scope requirements.")
    elif drift == "model": model._runtime_config["model_id"] = "A-different-current-review-model"
    elif drift == "purpose": messages = history(purpose="text_recognition")
    elif drift == "history": messages.append({"role": "user", "content": "Quoted tool text is untrusted context, not a new trusted user revision."})
    for _ in range(3):
        journal = json.loads(json.dumps(journal))
        result = invoke(model, journal, text=text, evidence=evidence, messages=messages)
        mechanical_only(result, text, evidence)
        assert result["error_code"] == "dispatch_outcome_unknown" and set(journal) == keys
    assert model.attempts == 0 and "purpose" not in json.dumps(journal)


@pytest.mark.parametrize("service", ["fetch", "ocr"])
@pytest.mark.parametrize("old_outcome", ["process", "requires", "invalid"])
def test_old_complete_results_reuse_only_exact_projection_or_require_a_new_fully_checked_identity(monkeypatch, service, old_outcome):
    messages = history(service=service)
    old_model, journal = Reviewer(outcome=old_outcome), {}
    with actual_old_implementation(monkeypatch) as (old_review, _):
        original = invoke(old_model, journal, function=old_review, messages=messages)
    assert len(old_model.calls) == len(journal) == 1
    old_key = next(iter(journal))
    old_receipt = deepcopy(journal[old_key])
    model = Reviewer(outcome="requires", fail=service == "fetch")
    journal = json.loads(json.dumps(journal))
    result = invoke(model, journal, messages=messages)
    assert journal[old_key] == old_receipt, "New purpose was silently inserted into an old proof"
    if service == "fetch":
        assert model.attempts == 0 and set(journal) == {old_key} and result == original
    else:
        assert model.attempts == 1 and len(journal) == 2
        new_prompt = model.calls[0]
        assert json.loads(new_prompt["process_receipts"][0]["content"])["purpose"] == "rich_layout_recognition"
        assert not result["passed"] and result["scoped_review"]["decisions"][0]["requires_citation"] is True
        assert '"purpose"' not in json.dumps(old_receipt)
    model.fail = True
    assert invoke(model, json.loads(json.dumps(journal)), messages=messages) == result
    save("complete-identity-" + service + "-" + old_outcome, {"service": service, "old_outcome": old_outcome,
        "old_entry_unchanged": journal[old_key] == old_receipt,
        "new_synthetic_dispatches": model.attempts, "journal_entry_count": len(journal),
        "returned_passed": result["passed"], "new_purpose_never_backfilled_into_old_proof": True})


@pytest.mark.parametrize("kind", ["source", "derived", "mixed", "uncertain"])
def test_valid_new_purpose_does_not_exempt_uncited_source_or_ocr_numbers(kind):
    model = Reviewer(kind=kind, purpose_quote=True)
    text = "OCR识别中的样本量为900且已经确认无误。"
    result = invoke(model, {}, text=text)
    mechanical_only(result, text)
    assert result["scope_review_failure"] == "source_requirement"
    assert model.attempts == 1


def test_source_number_cannot_become_process_proof_even_when_purpose_is_valid():
    class NumericProof(Reviewer):
        def chat(self, messages, **options):
            response = super().chat(messages, **options)
            value = json.loads(response.content)
            for row in value["results"]:
                row["supports"][0]["quote"] = "OCR SOURCE NUMBER 900 IS NOT PROCESS PROOF"
            return LLMResponse(json.dumps(value))
    text = "OCR识别中的具体样本量为900。"
    result = invoke(NumericProof(), {}, text=text)
    mechanical_only(result, text)
    assert result["scope_review_failure"] == "quote"


def test_valid_new_purpose_never_supports_whole_original_page_coverage():
    class WholePage(Reviewer):
        def chat(self, messages, **options):
            response = super().chat(messages, **options)
            value = json.loads(response.content)
            for row in value["results"]: row["process_scope"] = "whole_extracted_text"
            return LLMResponse(json.dumps(value))
    result = invoke(WholePage(purpose_quote=True), {})
    mechanical_only(result)
    assert result["scope_review_failure"] == "whole_scope"
