"""Independent D05 audit: production OCR adapter, fabricated bytes, mock HTTP.

No author test helpers, live provider, user configuration or credentials are used.
Scope reviewers below are explicit synthetic oracles; they test protocol wiring,
not a real model's scientific judgement or an OCR algorithm's accuracy.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from app import call_ledger, service_tool_config, service_tools, source_store
from app.agent.audit import review_citation_scope
from app.agent.citation_scope import process_receipts
from app.db import SessionLocal
from app.llm import LLMResponse
from app.models import Conversation, ConversationOptions
from app.providers.deepseek_tools import DeepSeekToolsClient
from app.rag.citation import citation_gate
from app.runtime_context import bind_runtime_config
from app.service_tool_models import ServiceToolInvocation
from app.source_runtime import freeze_sources


MISSING = object()
CID = "d05-independent-purpose-conversation"
SYNTHETIC_KEY = "D05-Independent-Only-Fabricated-Service-Key"
BODY = "OCR_REPORT_42; 7.5 mg; UNTRUSTED-OCR-BODY-D05"
PURPOSES = ("text_recognition", "rich_layout_recognition")


@pytest.fixture(autouse=True)
def forbid_external_sockets(monkeypatch):
    """Permit Windows' local asyncio plumbing, never external socket traffic."""
    for name in ("connect", "connect_ex"):
        original = getattr(socket.socket, name)

        def guarded(sock, address, _original=original):
            if isinstance(address, tuple) and address:
                try:
                    if ipaddress.ip_address(address[0]).is_loopback:
                        return _original(sock, address)
                except ValueError:
                    pass
            raise AssertionError("External socket forbidden in independent D05 audit")

        monkeypatch.setattr(socket.socket, name, guarded)


def _reply(purpose):
    return {"id": "d05-independent-mock-ocr", "object": "ocr.result", "created": 1762790100,
        "purpose": purpose, "page_count": 1,
        "pages": [{"index": 0, "text": "<｜ref｜>" + BODY + "<｜/ref｜><｜box｜>[[11,23,207,109]]<｜/box｜>",
            "finish_reason": "stop"}],
        "usage": {"input_tokens": 20, "output_tokens": 9, "total_tokens": 29}}


@pytest.fixture
def adapter(db, sample_pdf, monkeypatch):
    db.add(Conversation(id=CID))
    db.add(ConversationOptions(conversation_id=CID, approval_mode="always", revision=1))
    service_tool_config.save_configuration(db, {"expected_version": 0,
        "enabled_services": ["ocr"], "api_key": SYNTHETIC_KEY})
    scope = source_store.SourceScope("conversation", CID)
    original_bytes = sample_pdf.read_bytes()
    original = source_store.capture_source(db, scope=scope,
        authorization=source_store.SourceAuthorization(scope, "d05-fabricated-upload", "upload"),
        kind="upload", title="D05 fabricated original.pdf", raw_bytes=original_bytes,
        media_type="application/pdf")
    db.commit()
    binding = freeze_sources(db, CID, [original["revision_id"]],
        {"conversation_id": CID, "approval_mode": "always", "policy_revision": 1})
    requests, dispatch = [], [None]

    def transport(request):
        with SessionLocal() as owned:
            assert owned.scalar(select(ServiceToolInvocation).where(
                ServiceToolInvocation.state == "started")) is not None
        assert any(row["state"] == "started" for row in
            call_ledger.list_calls(include_synthetic=True)["items"])
        assert request.method == "POST" and request.url.host == "api.deepseek.com"
        assert request.url.path == "/v1/tools/ocr"
        assert request.headers["authorization"] == "Bearer " + SYNTHETIC_KEY
        assert original_bytes in request.content
        requests.append(request)
        return dispatch[0](request)

    mock = httpx.MockTransport(transport)
    monkeypatch.setattr(service_tools, "DeepSeekToolsClient", lambda key, **kwargs:
        DeepSeekToolsClient(key, transport=mock, synthetic=True, **kwargs))

    def invoke(purpose=MISSING, *, alter=None, outcome="ok", prepared=None):
        arguments = {"source_revision_id": original["revision_id"]}
        if purpose is not MISSING:
            arguments["purpose"] = purpose
        expected = arguments.get("purpose", "text_recognition")
        raw = _reply(expected)
        if alter:
            alter(raw)

        def respond(request):
            if outcome == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if outcome == "503":
                return httpx.Response(503, json={"synthetic": True})
            return httpx.Response(200, json=raw)

        dispatch[0] = respond
        prepared = prepared or service_tools.prepare_deepseek_tool(db, "deepseek_ocr", arguments,
            conversation_id=CID, operation_key=f"d05-independent-purpose:{len(requests)}", binding=binding)
        with bind_runtime_config(binding):
            result = json.loads(service_tools.execute_deepseek_tool(db, "deepseek_ocr", arguments,
                conversation_id=CID, prepared=prepared))
        return result, arguments, prepared

    return SimpleNamespace(invoke=invoke, original=original, original_bytes=original_bytes,
        scope=scope, requests=requests, db=db, binding=binding)


def _history(payload, arguments):
    return [{"role": "user", "content": "Use the selected fabricated attachment."},
        {"role": "assistant", "content": "MODEL-SELF-REPORT-IS-NOT-PROOF", "tool_calls": [{
            "id": "d05-independent-tool-call", "type": "function", "function": {
                "name": "deepseek_ocr", "arguments": json.dumps(arguments)}}]},
        {"role": "tool", "tool_call_id": "d05-independent-tool-call",
            "content": json.dumps(payload, ensure_ascii=False)}]


def _project(payload, arguments):
    return process_receipts(_history(payload, arguments))[0]


@pytest.mark.parametrize("purpose", [*PURPOSES, MISSING], ids=["text", "layout", "default"])
def test_real_adapter_to_unique_tool_result_keeps_exact_purpose(adapter, purpose):
    result, arguments, _ = adapter.invoke(purpose)
    expected = "text_recognition" if purpose is MISSING else purpose
    assert result["status"] == "ok" and result["purpose"] == expected
    assert len(adapter.requests) == 1
    multipart = adapter.requests[0].content
    assert b'name="purpose"' in multipart and expected.encode() in multipart
    receipts = _project(result, arguments)
    assert len(receipts) == 1
    receipt, contract = receipts[0], json.loads(receipts[0]["content"])
    assert receipt["id"] == "context:2" and receipt["scope"] == "process_metadata_only"
    assert contract["purpose"] == expected
    assert contract["original_revision_id"] == adapter.original["revision_id"]
    assert contract["original_sha256"] == hashlib.sha256(adapter.original_bytes).hexdigest()
    assert contract["returned_sources"][0]["revision_id"] != adapter.original["revision_id"]
    assert receipt["sha256"] == hashlib.sha256(receipt["content"].encode()).hexdigest()


@pytest.mark.parametrize("purpose", ["", None, "visual_understanding", [], True],
    ids=["empty", "null", "unknown", "list", "boolean"])
def test_invalid_request_purpose_does_not_dispatch(adapter, purpose):
    with pytest.raises(service_tool_config.ServiceConfigurationError):
        adapter.invoke(purpose)
    assert adapter.requests == []


@pytest.mark.parametrize("key", [MISSING, "", "   "], ids=["revoked", "empty", "whitespace"])
def test_absent_or_blank_dedicated_service_key_never_borrows_chat_key(adapter, monkeypatch, key):
    from app.config import settings
    monkeypatch.setattr(settings, "llm_api_key", "D05-Fabricated-Chat-Key-Must-Not-Be-Reused")
    service_tool_config.revoke_credential(adapter.db, 1)
    adapter.db.commit()
    if key is not MISSING:
        with pytest.raises(service_tool_config.ServiceConfigurationError):
            service_tool_config.save_configuration(adapter.db, {"expected_version": 2,
                "enabled_services": ["ocr"], "api_key": key})
    with pytest.raises(service_tool_config.ServiceConfigurationError):
        adapter.invoke("rich_layout_recognition")
    assert adapter.requests == []
    assert service_tools.available_deepseek_tool_specs() == []


@pytest.mark.parametrize("mode", ["provider-mismatch", "provider-missing", "provider-empty",
    "partial", "duplicate-page", "no-text", "503", "timeout"])
def test_actual_adapter_non_success_or_unprovable_outcome_has_no_success_purpose(adapter, mode):
    def alter(raw):
        if mode == "provider-mismatch": raw["purpose"] = "text_recognition"
        elif mode == "provider-missing": raw.pop("purpose")
        elif mode == "provider-empty": raw["purpose"] = ""
        elif mode == "partial": raw["pages"][0]["finish_reason"] = "length"
        elif mode == "duplicate-page":
            raw["page_count"] = 2
            raw["pages"] *= 2
        elif mode == "no-text": raw["pages"][0]["text"] = ""

    result, arguments, _ = adapter.invoke("rich_layout_recognition", alter=alter, outcome=mode)
    assert len(adapter.requests) == 1  # No hidden retry, including unknown outcomes.
    if mode not in {"partial", "no-text"}:
        assert result.get("status") != "ok"
    if mode == "partial":
        assert result["status"] == "partial" and result["truncated"] is True
    if mode == "no-text":
        assert result["status"] == "ok" and result["sources"] == []
    assert _project(result, arguments) == []


@pytest.mark.parametrize("defect", ["legacy-no-purpose", "empty-purpose", "list-purpose",
    "legacy-no-complete", "legacy-no-page-statuses", "empty-call-id", "wrong-purpose-arguments",
    "empty-argument-purpose", "incomplete", "truncated", "error", "unknown", "original-id",
    "empty-original-hash", "different-source-hash", "missing-source-hash", "missing-source",
    "boolean-page-index", "duplicate-page-index", "boolean-page-count"])
def test_corrupted_or_legacy_result_cannot_gain_purpose_by_current_arguments(adapter, defect):
    result, arguments, _ = adapter.invoke("rich_layout_recognition")
    result, arguments = deepcopy(result), deepcopy(arguments)
    if defect == "legacy-no-purpose": result.pop("purpose")
    elif defect == "empty-purpose": result["purpose"] = ""
    elif defect == "list-purpose": result["purpose"] = ["rich_layout_recognition"]
    elif defect == "legacy-no-complete": result.pop("response_complete")
    elif defect == "legacy-no-page-statuses": result.pop("page_statuses")
    elif defect == "empty-call-id": result["call_id"] = ""
    elif defect == "wrong-purpose-arguments": arguments["purpose"] = "text_recognition"
    elif defect == "empty-argument-purpose": arguments["purpose"] = ""
    elif defect == "incomplete": result["response_complete"] = False
    elif defect == "truncated": result["truncated"] = True
    elif defect == "error": result["error"] = "synthetic reported failure"
    elif defect == "unknown": result["execution_unknown"] = True
    elif defect == "original-id": result["original_revision_id"] = "f" * 32
    elif defect == "empty-original-hash": result["original_sha256"] = ""
    elif defect == "different-source-hash": result["sources"][0]["raw_sha256"] = "f" * 64
    elif defect == "missing-source-hash": result["sources"][0].pop("raw_sha256")
    elif defect == "missing-source": result["sources"] = []
    elif defect == "boolean-page-index": result["page_statuses"][0]["index"] = False
    elif defect == "duplicate-page-index":
        result["page_count"] = 2
        result["page_statuses"] *= 2
    elif defect == "boolean-page-count": result["page_count"] = True
    else: raise AssertionError(defect)
    assert _project(result, arguments) == []


@pytest.mark.parametrize("defect", ["wrong-key", "empty-declaration-key", "empty-result-key", "both-empty-keys",
    "missing-declaration-key", "missing-result-key", "result-before-call", "duplicate-call-message",
    "duplicate-call-in-message", "duplicate-result", "duplicate-failed-result", "missing-result", "excluded-result"])
def test_only_unique_ordered_nonempty_transport_pair_can_prove_purpose(adapter, defect):
    result, arguments, _ = adapter.invoke()
    history = _history(result, arguments)
    excluded = ()
    call = history[1]["tool_calls"][0]
    if defect == "wrong-key": history[2]["tool_call_id"] = "different-key"
    elif defect == "empty-declaration-key": call["id"] = ""
    elif defect == "empty-result-key": history[2]["tool_call_id"] = ""
    elif defect == "both-empty-keys": call["id"] = history[2]["tool_call_id"] = ""
    elif defect == "missing-declaration-key": call.pop("id")
    elif defect == "missing-result-key": history[2].pop("tool_call_id")
    elif defect == "result-before-call": history[1], history[2] = history[2], history[1]
    elif defect == "duplicate-call-message": history.insert(2, deepcopy(history[1]))
    elif defect == "duplicate-call-in-message": history[1]["tool_calls"].append(deepcopy(call))
    elif defect == "duplicate-result": history.append(deepcopy(history[2]))
    elif defect == "duplicate-failed-result":
        history.append({**deepcopy(history[2]), "content": json.dumps({"error": "failed duplicate"})})
    elif defect == "missing-result": history.pop()
    elif defect == "excluded-result": excluded = (2,)
    else: raise AssertionError(defect)
    assert process_receipts(history, excluded)[0] == []


def test_saved_success_replay_retains_bound_purpose_without_second_send(adapter):
    first, arguments, prepared = adapter.invoke("rich_layout_recognition")
    replay, replay_arguments, _ = adapter.invoke("rich_layout_recognition", prepared=prepared)
    assert len(adapter.requests) == 1 and replay["replayed_receipt"] is True
    assert replay["call_id"] == first["call_id"]
    assert _project(first, arguments) == _project(replay, replay_arguments)


def test_original_bytes_and_revision_survive_as_original_not_ocr_text(adapter):
    result, arguments, _ = adapter.invoke("rich_layout_recognition")
    original = source_store.read_revision(adapter.db, adapter.original["revision_id"],
        scope=adapter.scope, include_raw=True)
    derived = source_store.read_revision(adapter.db, result["sources"][0]["revision_id"],
        scope=adapter.scope, include_raw=True)
    assert original["raw_bytes"] == derived["raw_bytes"] == adapter.original_bytes
    assert original["current_revision_id"] == original["revision_id"] == arguments["source_revision_id"]
    assert original["text"] is None
    assert derived["revision_id"] != original["revision_id"]
    assert derived["text_origin"] == "ocr_text" and BODY in derived["text"]
    assert derived["origin_ref"] == "ocr-of:" + original["revision_id"]
    assert result["evidence"][0]["bbox"] is None


def test_fixed_original_tamper_is_blocked_before_mock_transport(adapter):
    arguments = {"source_revision_id": adapter.original["revision_id"]}
    prepared = service_tools.prepare_deepseek_tool(adapter.db, "deepseek_ocr", arguments,
        conversation_id=CID, operation_key="d05-independent-original-tamper", binding=adapter.binding)
    # Deliberate isolated storage fault injection bypasses ORM immutability.
    adapter.db.execute(text("UPDATE source_snapshots SET raw_bytes=:raw"), {"raw": b"tampered-fabricated-original"})
    adapter.db.commit()
    result, _, _ = adapter.invoke(prepared=prepared)
    assert result["code"] == "service_authorization_invalid" and adapter.requests == []
    assert _project(result, arguments) == []


def test_projection_excludes_source_prose_and_preserves_all_false_limits(adapter):
    result, arguments, _ = adapter.invoke("rich_layout_recognition")
    result["provider_claim"] = "FULL-ACCURATE-UNDERSTOOD-ORIGINAL-PIXELS"
    result["arbitrary_body"] = BODY * 5000
    result.update(original_page_coverage_verified=True, model_understanding_verified=True,
        ocr_text_accuracy_verified=True, coordinates_mapped_to_original=True)
    receipts = _project(result, arguments)
    assert len(receipts) == 1  # Long untrusted prose does not erase a small valid contract.
    encoded = receipts[0]["content"]
    contract = json.loads(encoded)
    assert BODY not in encoded and "FULL-ACCURATE" not in encoded
    assert "pages" not in contract and "evidence" not in contract and "provider_claim" not in contract
    assert contract["coverage_scope"] == "returned_ocr_text_only"
    for field in ("original_page_coverage_verified", "source_accuracy_verified", "model_understanding_verified",
            "whole_extracted_text_returned", "coordinates_mapped_to_original", "ocr_text_accuracy_verified"):
        assert contract[field] is False


class ScopeOracle:
    mode = "api"

    def __init__(self, *, kind="process", requires=False, quote='"purpose": "rich_layout_recognition"', scope="bounded"):
        self.kind, self.requires, self.quote, self.scope = kind, requires, quote, scope
        self.prompts = []

    def chat(self, messages, **kwargs):
        prompt = json.loads(messages[1]["content"])
        self.prompts.append(prompt)
        receipts = prompt["process_receipts"]
        supports = ([{"receipt_id": receipts[0]["id"], "quote": self.quote}]
            if self.kind == "process" and receipts else [])
        return LLMResponse(json.dumps({"results": [{"index": candidate["index"],
            "requires_citation": self.requires, "reason": "Synthetic explicit scope oracle for independent protocol audit",
            "kind": self.kind, "process_scope": self.scope if self.kind == "process" else "none",
            "supports": supports} for candidate in prompt["uncited_candidates"]]}))


def _scope(adapter, sentence, oracle):
    result, arguments, _ = adapter.invoke("rich_layout_recognition")
    evidence = [{**entry, "id": f"e{index}"} for index, entry in enumerate(result["evidence"], 1)]
    answer = "以下识别文本仍需核对。[^e1]\n\n" + sentence
    gate = citation_gate(answer, [entry["id"] for entry in evidence])
    assert gate["unsupported"] and not gate["passed"]
    reviewed = review_citation_scope("依据附件报告识别内容并如实说明执行范围", answer, evidence,
        gate, oracle, messages=_history(result, arguments))
    return gate, reviewed


def test_scope_receives_real_purpose_for_a_limited_invocation_parameter(adapter):
    oracle = ScopeOracle()
    gate, reviewed = _scope(adapter, "本次 OCR 成功调用的用途参数为 rich_layout_recognition。", oracle)
    assert not gate["passed"] and reviewed["passed"]
    assert len(reviewed["scoped_review"]["exemptions"]) == 1
    projected = oracle.prompts[0]["process_receipts"]
    assert len(projected) == 1
    assert json.loads(projected[0]["content"])["purpose"] == "rich_layout_recognition"
    assert BODY not in projected[0]["content"]


@pytest.mark.parametrize("kind,sentence", [
    ("source", "原件标识为 OCR_REPORT_42。"),
    ("source", "原件剂量为 7.5 mg。"),
    ("source", "该数值来自原件第一页。"),
    ("derived", "两次 7.5 mg 的累计剂量为 15 mg。"),
    ("mixed", "本次调用用途为 rich_layout_recognition，原件剂量为 7.5 mg。"),
])
def test_purpose_does_not_waive_source_numbers_identifier_page_or_mixed_assertions(adapter, kind, sentence):
    gate, reviewed = _scope(adapter, sentence, ScopeOracle(kind=kind, requires=True))
    assert not reviewed["passed"] and reviewed["unsupported"] == gate["unsupported"]
    assert reviewed["scoped_review"]["exemptions"] == []


@pytest.mark.parametrize("kind", ["source", "derived", "mixed", "uncertain"])
def test_invalid_source_kind_exemption_still_fails_despite_purpose(adapter, kind):
    gate, reviewed = _scope(adapter, "本次识别证明原件剂量为 7.5 mg。", ScopeOracle(kind=kind, requires=False))
    assert not reviewed["passed"] and reviewed["unsupported"] == gate["unsupported"]
    assert reviewed["scope_review_failure"] == "source_requirement"


@pytest.mark.parametrize("quote", ["OCR_REPORT_42", "7.5 mg in the original document",
    '"ocr_text_accuracy_verified": true', '"coordinates_mapped_to_original": true',
    '"original_page_coverage_verified": true', '"model_understanding_verified": true',
    '"page_index": 0'], ids=["identifier", "dose", "accuracy", "geometry", "original-coverage", "understanding", "original-page"])
def test_purpose_receipt_cannot_supply_missing_content_or_visual_quotes(adapter, quote):
    gate, reviewed = _scope(adapter, "本次识别已确认原件全文内容和准确版式。", ScopeOracle(quote=quote))
    assert not reviewed["passed"] and reviewed["unsupported"] == gate["unsupported"]
    assert reviewed["scope_review_failure"] == "quote"


def test_valid_purpose_quote_cannot_be_upgraded_to_whole_text_scope(adapter):
    gate, reviewed = _scope(adapter, "本次已经完整读取并理解原件全文。", ScopeOracle(scope="whole_extracted_text"))
    assert not reviewed["passed"] and reviewed["unsupported"] == gate["unsupported"]
    assert reviewed["scope_review_failure"] == "whole_scope"
