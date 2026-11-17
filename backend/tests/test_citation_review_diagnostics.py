"""Public F draft structures and real APIClient paths; every reply is synthetic."""
import json
from pathlib import Path

import httpx
import pytest

from app import call_ledger, config, llm
from app.agent.audit import audit_answer, review_citation_scope
from app.agent.citation_scope import process_receipts, review_error_code
from app.rag.citation import citation_gate, parse_claims
from scripts.verify_real_acceptance import public_review
from tests.test_citation_evidence_contract import audit_result
from tests.test_citation_scope_receipts import FACT, EVIDENCE, messages


PRIVATE = "SYNTHETIC-PRIVATE-REASONING-AND-PROVIDER-ERROR-BODY"
USAGE = {"prompt_tokens": 17, "completion_tokens": 19, "total_tokens": 36}


def f_draft(case_id):
    fixture = json.loads((Path(__file__).parent / "fixtures/citation-f-public-drafts.json").read_text(encoding="utf-8"))
    return next(case["drafts"][1] for case in fixture["cases"] if case["id"] == case_id)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_base_url", "https://diagnostic.example.test/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", "synthetic-diagnostic-credential")
    monkeypatch.setattr(config.settings, "llm_model", "Exact-Diagnostic-Model")
    original = httpx.Client
    responses, requests = [], []

    def dispatch(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: original(
        *args, transport=httpx.MockTransport(dispatch), **kwargs))
    return responses, requests


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("case,expected", [
    ("length", "output_incomplete"), ("empty", "output_incomplete"),
    ("invalid_finish", "model_response_invalid"), ("service_error", "model_response_invalid"),
    ("http_error", "http_error"),
])
def test_reported_usage_survives_failed_review_without_retry_or_private_response(db, provider, stage, case, expected):
    responses, requests = provider
    body = {"usage": USAGE, "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": '{"results":[]}', "reasoning_content": PRIVATE}}]}
    if case == "length":
        body["choices"][0]["finish_reason"] = "length"
    elif case == "empty":
        body["choices"][0]["message"]["content"] = ""
    elif case == "invalid_finish":
        body["choices"][0]["finish_reason"] = "unknown-provider-state"
    elif case == "service_error":
        body["error"] = {"message": PRIVATE}
    responses.append(httpx.Response(400, json={"error": PRIVATE}) if case == "http_error"
                     else httpx.Response(200, json=body))
    model = llm.APIClient(synthetic=True)
    if stage == "audit":
        value = audit_answer(db, FACT, EVIDENCE, llm=model, messages=[])
        assert value["verified"] is False and value["unknown_count"] == 1
        assert value["known_failed_count"] == 0 and not value["unsupported"]
    else:
        answer = FACT + "\n\n本次已完整读取当前材料并核对原件。"
        gate = citation_gate(answer, ["e1"])
        value = review_citation_scope("核对材料", answer, EVIDENCE, gate, model, messages=[])
        assert value["unsupported"] == gate["unsupported"] and not value["passed"]
    assert value["error_code"] == expected
    assert PRIVATE not in json.dumps(value, ensure_ascii=False)
    row, = call_ledger.list_calls(include_synthetic=True)["items"]
    assert row["state"] == "failed" and row["synthetic"] is True
    if case != "http_error":
        assert row["usage_source"] == "estimated" and row["estimated_usage"] == USAGE
    assert PRIVATE not in json.dumps(row)
    assert len(requests) == 1 and not responses, "diagnosis never silently buys a second review"


@pytest.mark.parametrize("reason,content", [("length", '{"results":['), ("stop", "")])
def test_nonsynthetic_meter_path_keeps_actual_usage_before_incomplete_error(db, provider, reason, content):
    # HTTP is still MockTransport. Only the meter flag differs so this checks
    # the production actual-usage branch without any real provider request.
    responses, requests = provider
    responses.append(httpx.Response(200, json={"usage": USAGE, "choices": [{
        "finish_reason": reason, "message": {"content": content, "reasoning_content": PRIVATE}}]}))
    value = audit_answer(db, FACT, EVIDENCE, llm=llm.APIClient(), messages=[])
    row, = call_ledger.list_calls()["items"]
    assert value["error_code"] == "output_incomplete" and value["unknown_count"] == 1
    assert row["state"] == "failed" and row["usage_source"] == "actual" and row["actual_usage"] == USAGE
    assert PRIVATE not in json.dumps(value) and PRIVATE not in json.dumps(row)
    assert len(requests) == 1


@pytest.mark.parametrize("exception,expected", [
    (llm.IncompleteModelResponse(PRIVATE), "output_incomplete"),
    (llm.ModelResponseError(PRIVATE), "model_response_invalid"),
    (httpx.ReadTimeout(PRIVATE), "http_error"), (ValueError(PRIVATE), "json"),
    (RuntimeError(PRIVATE), "provider_unavailable"),
])
def test_safe_error_enum_never_uses_exception_text(exception, expected):
    assert review_error_code(exception) == expected


def test_f_doc05_keeps_nonexempt_decisions_in_public_diagnostics():
    draft = f_draft("DOC05")
    gate = citation_gate(draft, ["e1", "e2"])

    class Reviewer:
        mode = "api"
        def chat(self, prompt, **kwargs):
            data = json.loads(prompt[1]["content"])
            assert data["full_answer"] == draft
            return llm.LLMResponse(json.dumps({"results": [{
                "index": item["index"], "requires_citation": True,
                "reason": "Synthetic mixed calculation/source claim requires its inputs",
                "kind": "mixed", "process_scope": "none", "supports": []
            } for item in data["uncited_candidates"]]}))

    value = review_citation_scope("读取预算并复算", draft, EVIDENCE, gate, Reviewer(), messages=[])
    decisions = value["scoped_review"]["decisions"]
    assert len(decisions) == len(gate["unsupported"])
    assert any("1550 是本次复算结果" in item["text"] for item in decisions)
    assert all(item["requires_citation"] and item["kind"] == "mixed" and item["reason"] for item in decisions)
    assert value["unsupported"] == gate["unsupported"] and not value["scoped_review"]["exemptions"]
    assert public_review(value)["scoped_review"]["decisions"] == decisions


def test_f_doc12_scientific_claims_remain_reviewable_and_unknown_does_not_erase_rejection(db):
    draft = f_draft("DOC12")
    evidence = [{"id": "e1", "quote": "An observational cohort of 40 participants reported improved alpha score."},
                {"id": "e2", "quote": "A randomized pilot with 18 participants did not find improvement in alpha score."}]
    factual = [claim for claim in parse_claims(draft, ["e1", "e2"])
               if claim["evidence_ids"]]
    power_index = next(index for index, claim in enumerate(factual) if "检验效能有限" in claim["claim"])

    class Reviewer:
        def chat(self, prompt, **kwargs):
            assert json.dumps(draft, ensure_ascii=False) in prompt[1]["content"]
            assert "检验效能有限" in prompt[1]["content"]
            result = audit_result(power_index, False, "derived")
            result["reason"] = "Synthetic rejection: small n alone does not establish actual power"
            return llm.LLMResponse(json.dumps({"results": [result]}))

    value = audit_answer(db, draft, evidence, llm=Reviewer(), messages=[])
    assert value["status"] == "failed" and value["verified"] is False
    assert value["known_failed_count"] == 1 and value["unknown_count"] == len(factual) - 1
    assert "检验效能有限" in value["unsupported"][0]["text"]


@pytest.mark.parametrize("mutation", ["whole_receipt", "too_many", "long_reason"])
def test_oversized_semantic_proof_is_unknown_not_support_or_rejection(db, mutation):
    history = messages()
    receipt, = process_receipts(history)[0]
    support = {"receipt_id": receipt["id"], "quote": '"text_observed": true'}
    result = audit_result(0, kind="process", supports=[support])
    if mutation == "whole_receipt":
        assert len(receipt["content"]) > 240
        result["supports"][0]["quote"] = receipt["content"]
    elif mutation == "too_many":
        result["supports"] = [support] * 9
    else:
        result["reason"] = "x" * 161

    class Reviewer:
        def chat(self, prompt, **kwargs):
            assert "8至240字符" in prompt[1]["content"]
            return llm.LLMResponse(json.dumps({"results": [result]}))

    value = audit_answer(db, "本次已读取当前附件的文本内容。[^e1]", EVIDENCE, llm=Reviewer(), messages=history)
    assert value["status"] == "unverified" and value["known_failed_count"] == 0 and value["unknown_count"] == 1


def test_short_exact_receipt_fragments_still_support_actual_process(db):
    history = messages()
    receipt, = process_receipts(history)[0]
    result = audit_result(0, kind="process", supports=[
        {"receipt_id": receipt["id"], "quote": '"text_observed": true'},
        {"receipt_id": receipt["id"], "quote": '"line_start": 1'},
        {"receipt_id": receipt["id"], "quote": '"line_end": 3'},
    ])

    class Reviewer:
        def chat(self, *args, **kwargs):
            return llm.LLMResponse(json.dumps({"results": [result]}))

    value = audit_answer(db, "本次实际读取返回了第一至第三行提取文本。[^e1]", EVIDENCE, llm=Reviewer(), messages=history)
    assert value["passed"] and value["known_failed_count"] == value["unknown_count"] == 0


@pytest.mark.parametrize("raw", [
    '{"results":[],"results":[{"index":0,"supported":false}]}',
    '{"results":[{"index":0,"supported":false,"supported":true}]}',
    '{"results":[],"private_reasoning":"' + PRIVATE + '"}',
    '```json\n{"results":[]}\n```', '{"results":NaN}',
])
def test_new_review_protocol_rejects_ambiguous_or_extra_json_without_storing_raw(db, raw):
    class Reviewer:
        def chat(self, *args, **kwargs):
            return llm.LLMResponse(raw)

    value = audit_answer(db, FACT, EVIDENCE, llm=Reviewer(), messages=[])
    assert value["known_failed_count"] == 0 and value["unknown_count"] == 1 and not value["verified"]
    assert value["error_code"] in ("json", "schema_results")
    assert PRIVATE not in json.dumps(value)


def test_public_error_projection_keeps_only_fixed_diagnosis():
    assert public_review({"error_code": "output_incomplete", "error": PRIVATE,
                          "reasoning_content": PRIVATE, "provider_payload": PRIVATE}) == {"error_code": "output_incomplete"}
