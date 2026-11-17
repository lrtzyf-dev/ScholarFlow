"""Reproduce V DOC06 annotation mechanics from retained public inputs only.

No original provider annotation response was retained in that public report.
Synthetic proposals here establish local acceptance boundaries, never what the
real annotation model proposed or whether a scientific claim is supported.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.agent import citation_marker_repair as repair
from app.llm import LLMResponse
from app.rag.citation import citation_gate, LIST_ITEM_RE, SENT_RE


REPORT = Path(__file__).resolve().parents[2] / "verification/real-model-20261116/fixed-sample-v-word-csv/acceptance-report.json"
REPORT_SHA256 = "21a372548e7241e77b9ec8aaca76be7e30f3a7cccb31d4158af5c8db5b561503"


def retained_case():
    assert hashlib.sha256(REPORT.read_bytes()).hexdigest() == REPORT_SHA256
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    return next(case for case in data["cases"] if case["id"] == "DOC06")["result"]["turns"][0]


def inputs(draft_index=0):
    case = retained_case()
    draft = case["public_citation_drafts"][draft_index]
    gate = (next(event["gate"] for event in case["diagnostics"]["events"] if event["kind"] == "critic")
        if draft_index == 0 else case["diagnostics"]["final_gate"])
    evidence = case["evidence"]
    baseline = citation_gate(draft["answer"], [item["id"] for item in evidence])
    assert baseline == draft["gate"]
    return draft["answer"], evidence, gate, baseline


def complete_matches(answer, prefix):
    candidates = []
    for line in answer.splitlines():
        for body in (line, LIST_ITEM_RE.sub("", line, count=1)):
            candidates.extend(match.group().strip() for match in SENT_RE.finditer(body))
    result = []
    for text in dict.fromkeys(candidates):
        span = repair._complete_span(answer, text)
        if span and span[2][:80] == prefix:
            result.append({"text": text, "start": span[0], "end": span[1]})
    return result


class SyntheticAnnotationModel:
    mode = "api"

    def __init__(self, proposals):
        self.proposals, self.calls = proposals, []

    def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        return LLMResponse(json.dumps({"edits": self.proposals}, ensure_ascii=False),
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


@pytest.fixture(autouse=True)
def forbid_external_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("V mapping uses retained public inputs and synthetic local proposals only")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def proposal(answer, gate, index):
    return {"candidate_index": index, "text": complete_matches(answer, gate["unsupported"][index])[0]["text"],
        "evidence_ids": ["e1"]}


def test_retained_first_draft_scope_and_current_parser_agree_exactly():
    answer, evidence, gate, baseline = inputs()
    assert len(baseline["unsupported"]) == 11 and baseline["marked_claims"] == 4
    assert len(gate["unsupported"]) == 8
    assert set(repair._eligible(gate, baseline)) == set(range(7))
    mixed = gate["scoped_review"]["decisions"][-1]
    assert mixed["kind"] == "mixed" and mixed["requires_citation"] is True
    assert mixed["text"] == gate["unsupported"][7]
    assert len(gate["unsupported"][7]) == 80
    assert repair._complete_span(answer, gate["unsupported"][7]) is None


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_real_table_rows_and_introduction_cannot_launder_unedited_claims_by_inheritance(index):
    answer, evidence, gate, baseline = inputs()
    model = SyntheticAnnotationModel([proposal(answer, gate, index)])
    result = repair.repair_citation_markers(answer, evidence, gate, model)
    assert len(model.calls) == 1
    assert not result["changed"] and result["answer"] == answer
    assert result["reason"] == "no_exact_single_reduction"
    assert result["semantic_verification"] == "not_performed"


@pytest.mark.parametrize("index", [4, 5, 6])
def test_real_derived_complete_bullets_have_an_exact_single_annotation_path(index):
    answer, evidence, gate, baseline = inputs()
    model = SyntheticAnnotationModel([proposal(answer, gate, index)])
    result = repair.repair_citation_markers(answer, evidence, gate, model)
    assert result["changed"] and len(result["edits"]) == 1
    assert result["missing_before"] == 11 and result["missing_after"] == 10
    assert result["annotation_only"] and result["semantic_verification"] == "not_performed"
    assert "passed" not in result and "verified" not in result
    assert result["answer"].count("[^e1]") == answer.count("[^e1]") + 1


def test_real_mixed_long_sentence_is_excluded_even_with_an_exact_full_span():
    answer, evidence, gate, baseline = inputs()
    edit = proposal(answer, gate, 7)
    assert len(edit["text"]) > 80 and repair._complete_span(answer, edit["text"])
    model = SyntheticAnnotationModel([edit])
    result = repair.repair_citation_markers(answer, evidence, gate, model)
    assert result["answer"] == answer and not result["changed"]
    assert result["reason"] == "edit_identity"
    prompt = json.loads(model.calls[0][0][1]["content"])
    assert [row["candidate_index"] for row in prompt["candidates"]] == list(range(7))


def test_three_safe_real_candidates_can_be_inserted_together_but_do_not_complete_the_gate():
    answer, evidence, gate, baseline = inputs()
    result = repair.repair_citation_markers(answer, evidence, gate,
        SyntheticAnnotationModel([proposal(answer, gate, i) for i in (4, 5, 6)]))
    assert result["changed"] and result["missing_after"] == 8
    after = citation_gate(result["answer"], [item["id"] for item in evidence])
    assert after["total_claims"] == baseline["total_claims"] and not after["passed"]
    assert len(result["edits"]) == 3 and result["semantic_verification"] == "not_performed"


def test_rewrite_introduction_can_be_annotated_without_erasing_the_two_retained_semantic_failures():
    answer, evidence, gate, baseline = inputs(1)
    assert list(repair._eligible(gate, baseline)) == [0]
    assert len(gate["unsupported"]) == 1 and len(baseline["unsupported"]) == 4
    result = repair.repair_citation_markers(answer, evidence, gate,
        SyntheticAnnotationModel([proposal(answer, gate, 0)]))
    assert result["changed"] and result["missing_after"] == 3
    assert not citation_gate(result["answer"], [item["id"] for item in evidence])["passed"]
    retained = retained_case()["diagnostics"]["final_audit"]
    assert retained["passed"] is False and retained["known_failed_count"] == 2
    assert result["semantic_verification"] == "not_performed" and "passed" not in result
