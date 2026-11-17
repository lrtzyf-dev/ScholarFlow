"""Independent compact-scope cache and upgrade adversaries; no real supplier.

Legacy journals are produced by the frozen pre-change implementation instead
of copying the new implementation's legacy-key reconstruction.
"""
from copy import deepcopy
import ast
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.agent import audit, citation_scope
from app.agent.paid_review_family import FIELD
from app.llm import IncompleteModelResponse, LLMResponse
from app.rag.citation import citation_gate


QUESTION = "固定材料的科学结论与实际读取范围分别是什么？"
EVIDENCE = [{"id": "e1", "paper_title": "Independent fabricated material", "quote": "40 synthetic observations."}]
ANSWER = "虚构材料报告40个合成观察值[^e1]。\n\n工具已返回该固定附件全部提取文本。"
FROZEN_AUDIT_SHA256 = "def62bff14a05e66bc0d6a7f7af56f9a62376847d4dcb6ab71efecc991ebf15c"
FROZEN_SCOPE_SHA256 = "2750e75ecd0d77fdfefcd7367d7cbc42d135b7c3a11c94ee073731391dea875d"


def history():
    payload = {"read_range": {"kind": "text_lines", "line_start": 1, "line_end": 2,
                              "total_lines": 2, "next_line": None},
               "text_observed": True, "output_is_partial": False, "coverage_scope": "extracted_text_only",
               "source": {"revision_id": "9" * 32, "title": "Independent fabricated input"}}
    return [{"role": "assistant", "content": "", "tool_calls": [{"id": "independent-read",
        "type": "function", "function": {"name": "read_attached_source",
        "arguments": json.dumps({"source_revision_id": "9" * 32})}}]},
        {"role": "tool", "tool_call_id": "independent-read", "content": json.dumps(payload)}]


def proof_chunks(content):
    chunks = [content[start:start + 240] for start in range(0, len(content), 240)]
    if len(chunks) > 1 and len(chunks[-1]) < 8:
        move = 8 - len(chunks[-1])
        chunks[-1] = chunks[-2][-move:] + chunks[-1]
        chunks[-2] = chunks[-2][:-move]
    assert "".join(chunks) == content and len(chunks) <= 8 and all(8 <= len(part) <= 240 for part in chunks)
    return chunks


class Reviewer:
    mode = "api"

    def __init__(self, *, mutate=None, raw=None, error=None):
        self.calls, self.mutate, self.raw, self.error = [], mutate, raw, error
        self.forbid_dispatch = False
        self.dispatch_attempts = 0

    def chat(self, messages, **kwargs):
        self.dispatch_attempts += 1
        assert not self.forbid_dispatch, "A cached or unknown paid operation was dispatched again"
        payload = json.loads(messages[-1]["content"])
        self.calls.append({"messages": deepcopy(messages), "payload": payload, "kwargs": deepcopy(kwargs)})
        if self.error:
            raise self.error
        rows = []
        for candidate in payload["uncited_candidates"]:
            receipt = payload["process_receipts"][0]
            compact = payload.get("response_protocol") == "citation_scope_with_process_receipts_v2"
            quotes = proof_chunks(receipt["content"]) if compact else [receipt["content"]]
            rows.append({"index": candidate["index"], "requires_citation": False,
                "reason": "Checked the complete real metadata." if compact else "L" * 201,
                "kind": "process", "process_scope": "whole_extracted_text",
                "supports": [{"receipt_id": receipt["id"], "quote": quote} for quote in quotes]})
        response = {"results": rows}
        if self.mutate:
            self.mutate(response, payload)
        return LLMResponse(self.raw if self.raw is not None else json.dumps(response),
                           usage={"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26})


def invoke(reviewer, journal, *, function=audit.review_citation_scope, text=ANSWER,
           evidence=EVIDENCE, messages=None, persist=lambda: None, before=lambda: None, usage=None):
    attempts = reviewer.dispatch_attempts
    result = function(QUESTION, text, evidence, citation_gate(text, [entry["id"] for entry in evidence]), reviewer,
        messages=history() if messages is None else messages, review_journal=journal,
        persist=persist, before_review=before, on_usage=usage)
    if reviewer.forbid_dispatch:
        # An AssertionError inside chat is caught as provider failure by the
        # production reviewer; assert the call count outside that boundary.
        assert reviewer.dispatch_attempts == attempts, "Production attempted a forbidden cached/unknown redispatch"
    return result


def frozen_function():
    base = (Path(__file__).resolve().parents[2] / "verification" / "real-model-20261116"
            / "code-current-marker-feedback" / "backend" / "app" / "agent")
    legacy_audit, legacy_scope = base / "audit.py", base / "citation_scope.py"
    assert hashlib.sha256(legacy_audit.read_bytes()).hexdigest() == FROZEN_AUDIT_SHA256
    assert hashlib.sha256(legacy_scope.read_bytes()).hexdigest() == FROZEN_SCOPE_SHA256
    tree = ast.parse(legacy_scope.read_text(encoding="utf-8"))
    instructions = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "PROCESS_INSTRUCTIONS" for target in node.targets))
    assert citation_scope.PROCESS_INSTRUCTIONS == instructions, "v1 instruction bytes changed, so old exact keys cannot be reconstructed"
    spec = importlib.util.spec_from_file_location("app.agent._independent_frozen_scope_audit", legacy_audit)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.review_citation_scope


def assert_original_gate(value, text=ANSWER):
    gate = citation_gate(text, ["e1"])
    assert all(value[name] == expected for name, expected in gate.items())
    assert value.get("scope_review_failure") and "scoped_review" not in value


def test_compact_cached_receipt_boolean_count_is_not_an_authenticated_integer():
    reviewer, journal = Reviewer(), {}
    assert invoke(reviewer, journal)["passed"]
    saved = next(iter(journal.values()))
    assert saved["scoped_review"]["process_receipt_count"] == 1
    saved["scoped_review"]["process_receipt_count"] = True
    reviewer.forbid_dispatch = True
    result = invoke(reviewer, journal)
    assert_original_gate(result)


@pytest.mark.parametrize("container,field,value,uncited_only", [
    ("scope", "process_receipt_count", 1.0, False),
    ("scope", "original_total_claims", 2.0, False),
    ("scope", "original_marked_claims", True, False),
    ("scope", "original_unsupported_count", True, False),
    ("result", "marked_claims", True, False),
    ("result", "total_claims", True, False),
    ("result", "grounded_ratio", True, False),
    ("scope", "original_total_claims", True, True),
    ("scope", "original_marked_claims", False, True),
    ("scope", "original_grounded_ratio", False, True),
    ("result", "marked_claims", False, True),
    ("result", "total_claims", False, True),
])
def test_compact_cached_numeric_identity_never_accepts_boolean_or_float_count(container, field, value, uncited_only):
    text = ANSWER.split("\n\n")[-1] if uncited_only else ANSWER
    reviewer, journal = Reviewer(), {}
    assert invoke(reviewer, journal, text=text)["passed"]
    saved = next(iter(journal.values()))
    target = saved["scoped_review"] if container == "scope" else saved
    assert target[field] == value and type(target[field]) is not type(value)
    target[field] = value
    reviewer.forbid_dispatch = True
    assert_original_gate(invoke(reviewer, journal, text=text), text)


@pytest.mark.parametrize("damage", ["nan_ratio", "infinite_ratio", "protocol", "verified", "index_boolean",
    "reason", "nine_supports", "long_quote", "wrong_receipt", "quote_not_literal", "exemption_mismatch"])
def test_compact_cached_proof_mutations_preserve_original_gate_without_redispatch(damage):
    reviewer, journal = Reviewer(), {}
    assert invoke(reviewer, journal)["passed"]
    saved = next(iter(journal.values()))
    scope = saved["scoped_review"]
    if damage == "nan_ratio":
        saved["grounded_ratio"] = float("nan")
    elif damage == "infinite_ratio":
        scope["original_grounded_ratio"] = float("inf")
    elif damage == "protocol":
        scope["protocol"] = "citation_scope_with_process_receipts_v1"
    elif damage == "verified":
        scope["verified"] = 1
    elif damage == "exemption_mismatch":
        scope["exemptions"] = []
    else:
        for decision in scope["decisions"] + scope["exemptions"]:
            if damage == "index_boolean":
                decision["index"] = False
            elif damage == "reason":
                decision["reason"] = " " * 160 + "R"
            elif damage == "nine_supports":
                decision["supports"] = [deepcopy(decision["supports"][0]) for _ in range(9)]
            elif damage == "long_quote":
                receipt = reviewer.calls[0]["payload"]["process_receipts"][0]
                decision["supports"][0]["quote"] = receipt["content"][:241]
            elif damage == "wrong_receipt":
                decision["supports"][0]["receipt_id"] = "forged"
            else:
                decision["supports"][0]["quote"] = "This literal never appeared in a tool receipt"
    reviewer.forbid_dispatch = True
    assert_original_gate(invoke(reviewer, journal))


@pytest.mark.parametrize("failed", [False, True])
def test_frozen_v1_long_complete_or_failed_receipts_survive_repeated_json_restoration(failed):
    old = frozen_function()
    reviewer = Reviewer(raw="not complete JSON" if failed else None)
    journal, usage = {}, []
    original = invoke(reviewer, journal, function=old, usage=usage.append)
    assert len(journal) == len(reviewer.calls) == len(usage) == 1
    key = next(iter(journal))
    if failed:
        assert original["scope_review_failure"] == "json"
    else:
        scope = original["scoped_review"]
        assert scope["protocol"] == "citation_scope_with_process_receipts_v1"
        assert len(scope["decisions"][0]["reason"]) > 160
        assert len(scope["decisions"][0]["supports"][0]["quote"]) > 240
    reviewer.forbid_dispatch = True
    for _ in range(12):
        journal = json.loads(json.dumps(journal, ensure_ascii=False))
        result = invoke(reviewer, journal, usage=usage.append,
                        persist=lambda: pytest.fail("An exact complete legacy result must not need saving again"))
        assert set(journal) == {key} and len(reviewer.calls) == len(usage) == 1
        if failed:
            assert_original_gate(result)
            assert result["scope_review_failure"] == "json"
        else:
            assert result["passed"] and result["scoped_review"] == original["scoped_review"]


def test_frozen_v1_complete_known_source_requirement_is_retained_without_becoming_a_pass():
    def source_required(data, _):
        for row in data["results"]:
            row.update(kind="source", requires_citation=True, process_scope="none", supports=[])
    reviewer, journal = Reviewer(mutate=source_required), {}
    original = invoke(reviewer, journal, function=frozen_function())
    assert original["passed"] is False and original["scoped_review"]["verified"] is True
    assert original["scoped_review"]["protocol"] == "citation_scope_with_process_receipts_v1"
    assert original["scoped_review"]["exemptions"] == []
    reviewer.forbid_dispatch = True
    for _ in range(12):
        journal = json.loads(json.dumps(journal))
        current = invoke(reviewer, journal)
        assert current["passed"] is False and current["unsupported"] == original["unsupported"]
        assert current["scoped_review"] == original["scoped_review"]
    assert len(reviewer.calls) == 1


def test_frozen_v1_output_incomplete_failure_stays_distinct_and_is_not_repurchased():
    reviewer, journal = Reviewer(error=IncompleteModelResponse("Independent synthetic incomplete output")), {}
    original = invoke(reviewer, journal, function=frozen_function())
    assert original["scope_review_failure"] == "output_incomplete"
    reviewer.error, reviewer.forbid_dispatch = None, True
    for _ in range(12):
        journal = json.loads(json.dumps(journal))
        current = invoke(reviewer, journal)
        assert_original_gate(current)
        assert current["scope_review_failure"] == "output_incomplete"
    assert len(reviewer.calls) == 1


class LostReply(BaseException):
    """Simulate a process loss after the mock supplier began dispatch."""


def old_pending_journal():
    reviewer, journal, checkpoints = Reviewer(error=LostReply()), {}, []
    with pytest.raises(LostReply):
        invoke(reviewer, journal, function=frozen_function(), persist=lambda: checkpoints.append(deepcopy(journal)))
    assert len(reviewer.calls) == len(checkpoints) == 1
    assert next(iter(checkpoints[-1].values()))["review_dispatch"] == "pending"
    reviewer.error, reviewer.forbid_dispatch = None, True
    return reviewer, json.loads(json.dumps(checkpoints[-1]))


@pytest.mark.parametrize("unscoped", [False, True])
@pytest.mark.parametrize("old_state", ["pending", "unknown"])
def test_frozen_old_unknown_upgrade_blocks_model_evidence_answer_and_read_drift(unscoped, old_state):
    reviewer, journal = old_pending_journal()
    key = next(iter(journal))
    if unscoped:
        journal[key].pop(FIELD)
    if old_state == "unknown":
        family = journal[key].get(FIELD)
        journal[key] = {"error_code": "dispatch_outcome_unknown", "passed": False, "verified": False}
        if family is not None:
            journal[key][FIELD] = family
    reviewer._runtime_config = {"mode": "api", "model_id": "changed-mock-model", "reasoning_effort": "high"}
    evidence = [{**EVIDENCE[0], "quote": "Changed fixed mock evidence; same user revision."}]
    changed_history = history()
    payload = json.loads(changed_history[-1]["content"])
    payload["source"]["title"] = "Different mock return title"
    changed_history[-1]["content"] = json.dumps(payload)
    text = ANSWER + "\n\n一般建议是再次核查术语。"
    checkpoints = []
    result = invoke(reviewer, journal, text=text, evidence=evidence, messages=changed_history,
                    persist=lambda: checkpoints.append(deepcopy(journal)))
    assert_original_gate(result, text)
    assert result["scope_review_failure"] == "dispatch_outcome_unknown" and set(journal) == {key}
    restored = json.loads(json.dumps(journal))
    assert invoke(reviewer, restored, text=text)["scope_review_failure"] == "dispatch_outcome_unknown"
    assert len(reviewer.calls) == 1


def test_failed_unknown_conversion_save_restores_old_pending_and_still_never_dispatches():
    reviewer, journal = old_pending_journal()
    before = deepcopy(journal)
    def failed_save():
        raise OSError("Independent pending-to-unknown checkpoint failed")
    with pytest.raises(OSError, match="pending-to-unknown"):
        invoke(reviewer, journal, persist=failed_save)
    assert journal == before and len(reviewer.calls) == 1
    assert invoke(reviewer, journal)["scope_review_failure"] == "dispatch_outcome_unknown"
    assert len(reviewer.calls) == 1


@pytest.mark.parametrize("kind", ["source", "derived", "mixed", "uncertain"])
def test_source_or_derived_mixed_kinds_cannot_become_independent_exemptions_live_or_cached(kind):
    def changed(data, _):
        for row in data["results"]:
            row.update(kind=kind, requires_citation=False, process_scope="none", supports=[])
    reviewer, journal = Reviewer(mutate=changed), {}
    result = invoke(reviewer, journal)
    assert_original_gate(result)
    assert result["scope_review_failure"] == "source_requirement"
    reviewer.forbid_dispatch = True
    assert invoke(reviewer, journal)["scope_review_failure"] == "source_requirement"
    reviewer, journal = Reviewer(), {}
    assert invoke(reviewer, journal)["passed"]
    for row in next(iter(journal.values()))["scoped_review"]["decisions"] + next(iter(journal.values()))["scoped_review"]["exemptions"]:
        row.update(kind=kind, requires_citation=False, process_scope="none", supports=[])
    reviewer.forbid_dispatch = True
    assert_original_gate(invoke(reviewer, journal))


def test_compact_process_fixture_keeps_every_literal_metadata_byte_and_all_clause_instructions():
    reviewer, journal = Reviewer(), {}
    result = invoke(reviewer, journal)
    assert result["passed"]
    call = reviewer.calls[0]
    receipt = call["payload"]["process_receipts"][0]
    support = result["scoped_review"]["decisions"][0]["supports"]
    assert len(support) > 1 and len(support) <= 8
    assert "".join(item["quote"] for item in support) == receipt["content"]
    assert all(item["receipt_id"] == receipt["id"] and 8 <= len(item["quote"]) <= 240 for item in support)
    instructions = call["messages"][0]["content"]
    assert "所有子句都须受到引用回执完整支持" in instructions
    assert "不得因压缩而省略任何过程事实的实际支持" in instructions
    assert "全文" in instructions and "whole_extracted_text_returned" in instructions


def test_source_quote_is_not_process_proof_even_when_it_is_a_real_evidence_string():
    def source_prose(data, payload):
        quote = EVIDENCE[0]["quote"]
        receipt = payload["process_receipts"][0]
        assert quote not in receipt["content"]
        data["results"][0]["supports"] = [{"receipt_id": receipt["id"], "quote": quote}]
    reviewer, journal = Reviewer(mutate=source_prose), {}
    result = invoke(reviewer, journal)
    assert_original_gate(result)
    assert result["scope_review_failure"] == "quote"


@pytest.mark.parametrize("raw", [
    '```json\n{"results":[]}\n```',
    '{"results":[],"results":[]}',
    '{"results":[{"index":0,"requires_citation":false}]',
    '{"results":[]} trailing text',
])
def test_partial_wrapped_duplicate_or_trailing_json_is_cached_failure_without_salvage(raw):
    reviewer, journal = Reviewer(raw=raw), {}
    first = invoke(reviewer, journal)
    assert_original_gate(first)
    reviewer.forbid_dispatch = True
    assert_original_gate(invoke(reviewer, journal))
    assert len(reviewer.calls) == 1
