"""Formal service wiring with synthetic credentials and zero external traffic."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import call_ledger, service_tool_config as config, service_tools as service, source_store
from app.db import SessionLocal
from app.main import app
from app.models import Conversation, ConversationOptions, ModelCredential
from app.providers.deepseek_tools import DeepSeekToolsClient
from app.runtime_context import bind_runtime_config
from app.service_tool_models import ServiceToolConfiguration, ServiceToolCredential, ServiceToolInvocation
from app.source_documents import read_document
from app.source_models import Source, SourceRevision
from app.source_runtime import freeze_sources
from app.timeutil import utcnow


client = TestClient(app)
KEY = "Synthetic-Independent-Service-Key-CaSe"
CHAT_KEY = "Synthetic-Chat-Key-Must-Not-Be-Reused"
CID = "service-conversation"


def settings(**overrides):
    return client.put("/api/service-tools/deepseek", json={"expected_version": 0,
        "enabled_services": ["search", "fetch", "ocr"], "api_key": KEY, **overrides})


@pytest.fixture
def binding(db):
    db.add(Conversation(id=CID))
    db.add(ConversationOptions(conversation_id=CID, approval_mode="always", revision=1))
    db.commit()
    assert settings().status_code == 200
    return {"conversation_id": CID, "approval_mode": "always", "policy_revision": 1}


@pytest.fixture
def network(monkeypatch):
    calls = []
    dispatch = [lambda request: httpx.Response(200, json={"results": []})]

    def handle(request):
        # Both independent ledgers are durably present before any send.
        with SessionLocal() as db:
            assert db.scalar(select(ServiceToolInvocation).where(ServiceToolInvocation.state == "started")) is not None
        assert any(r["state"] == "started" for r in call_ledger.list_calls(include_synthetic=True)["items"])
        assert request.headers["authorization"] == "Bearer " + KEY
        calls.append(request)
        return dispatch[0](request)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(service, "DeepSeekToolsClient", lambda key, **kw:
        DeepSeekToolsClient(key, transport=transport, synthetic=True, **kw))
    return calls, dispatch


def execute(db, binding, name="deepseek_search", arguments=None, *, operation="service:test", prepared=None):
    arguments = arguments or {"query": "synthetic query"}
    prepared = prepared or service.prepare_deepseek_tool(db, name, arguments,
        conversation_id=CID, operation_key=operation, binding=binding)
    with bind_runtime_config(binding):
        return json.loads(service.execute_deepseek_tool(db, name, arguments,
            conversation_id=CID, prepared=prepared)), prepared


def item(content=""):
    return {"title": "Synthetic page", "url": "https://synthetic.invalid/page", "snippet": "discovery snippet",
        "summary": "A synthetic search summary is not original source text.", "content": content,
        "rerank_score": 1.5, "authority": 0, "site_name": None, "site_icon": None, "date": None}


def test_configuration_is_disabled_by_default_fixed_endpoint_and_has_no_chat_fallback(db, monkeypatch):
    from app.config import settings as chat_settings
    monkeypatch.setattr(chat_settings, "llm_api_key", CHAT_KEY)
    db.add(ModelCredential(protected_value="synthetic-chat-record"))
    db.commit()
    initial = client.get("/api/service-tools/deepseek")
    assert initial.status_code == 200
    assert initial.json()["version"] == 0 and initial.json()["enabled_services"] == []
    assert initial.json()["api_key_set"] is False
    assert initial.json()["endpoints"]["search"] == "https://api.deepseek.com/v1/tools/search"
    missing = settings(api_key=None)
    assert missing.status_code == 422
    omitted = client.put("/api/service-tools/deepseek", json={"expected_version": 0, "enabled_services": ["fetch"]})
    assert omitted.status_code == 422
    assert CHAT_KEY not in initial.text + missing.text + omitted.text
    assert db.query(ServiceToolCredential).count() == 0


def test_configuration_cas_rotation_revokes_old_key_and_public_contract_never_leaks(db):
    first = settings()
    assert first.status_code == 200
    assert first.json()["version"] == 1 and first.json()["api_key_set"]
    assert KEY not in first.text and "credential_ref" not in first.text
    with SessionLocal() as session:
        old = session.query(ServiceToolCredential).one()
        old_id = old.id
        assert old.protected_value.startswith("dpapi:") and KEY not in old.protected_value
        assert config.require_configuration(session, "search", reveal_key=True)["api_key"] == KEY
    assert settings().status_code == 409
    same = client.put("/api/service-tools/deepseek", json={"expected_version": 1, "enabled_services": ["search"]})
    assert same.status_code == 200 and same.json()["version"] == 2
    assert same.json()["api_key_set"]
    changed = settings(expected_version=2, api_key="Synthetic-Rotated-Key", enabled_services=["ocr"])
    assert changed.status_code == 200 and changed.json()["version"] == 3
    with SessionLocal() as session:
        assert session.get(ServiceToolCredential, old_id).revoked_at is not None
    stale = client.delete("/api/service-tools/deepseek/credential?expected_version=2")
    assert stale.status_code == 409
    revoked = client.delete("/api/service-tools/deepseek/credential?expected_version=3")
    assert revoked.status_code == 200
    assert revoked.json()["version"] == 4 and not revoked.json()["api_key_set"] and not revoked.json()["enabled_services"]


@pytest.mark.parametrize("patch", [
    {"api_key": ""}, {"api_key": "   "}, {"api_key": "secret\nline"}, {"api_key": {"private": KEY}},
    {"api_key": [KEY]}, {"expected_version": True}, {"enabled_services": ["search", "search"]},
    {"base_url": "https://attacker.invalid"}, {"model": "chat-model"}, {"enabled_services": ["Search"]},
])
def test_bad_configuration_does_not_echo_credentials_or_partially_save(patch):
    response = settings(**patch)
    assert response.status_code == 422
    assert KEY not in response.text and "secret\nline" not in response.text
    assert client.get("/api/service-tools/deepseek").json()["version"] == 0


def test_tools_are_guarded_and_any_model_can_discover_them(binding):
    from app.agent.tools import get_tool_specs, tool_risk
    specs = {s["name"]: s for s in get_tool_specs()}
    assert {"deepseek_search", "deepseek_fetch", "deepseek_ocr"} <= set(specs)
    assert not {"deepseek_research", "deepseek_file_read"} & set(specs), "Native and Files require their own explicit switches"
    assert all(tool_risk(name) == "guarded" for name in service.DEEPSEEK_TOOL_NAMES)
    assert "深度搜索" in specs["deepseek_search"]["description"]
    assert "不是深度搜索" in specs["deepseek_search"]["description"]


def test_model_catalog_hides_unconfigured_disabled_and_revoked_services():
    from app.agent.tools import get_tool_specs
    assert not service.DEEPSEEK_TOOL_NAMES & {s["name"] for s in get_tool_specs()}
    assert settings(enabled_services=["fetch"]).status_code == 200
    assert service.DEEPSEEK_TOOL_NAMES & {s["name"] for s in get_tool_specs()} == {"deepseek_fetch"}
    assert client.delete("/api/service-tools/deepseek/credential?expected_version=1").status_code == 200
    assert not service.DEEPSEEK_TOOL_NAMES & {s["name"] for s in get_tool_specs()}


def test_model_catalog_hides_undecryptable_credential_without_network(db, binding, network):
    db.query(ServiceToolCredential).one().protected_value = "dpapi:invalid"
    db.commit()
    assert service.available_deepseek_tool_specs() == [] and not network[0]


def test_model_cannot_bypass_approval_with_arguments(db, binding, network):
    with bind_runtime_config(binding):
        result = json.loads(service.execute_deepseek_tool(db, "deepseek_fetch", {"url": "https://synthetic.invalid"}, conversation_id=CID))
    assert result["code"] == "approval_required" and not network[0]
    assert not call_ledger.list_calls(include_synthetic=True)["items"]


def test_search_summary_does_not_become_evidence_and_paid_result_replays(db, binding, network):
    network[1][0] = lambda request: httpx.Response(200, json={"results": [item()]})
    result, prepared = execute(db, binding)
    assert result["status"] == "ok" and result["evidence"] == [] and result["sources"] == []
    assert result["results"][0]["summary_is_source_text"] is False
    assert not result["results"][0]["content_saved"]
    replay, _ = execute(db, binding, prepared=prepared)
    assert replay["replayed_receipt"] is True and len(network[0]) == 1
    assert db.query(Source).count() == 0
    rows = call_ledger.list_calls(include_synthetic=True)["items"]
    assert len(rows) == 1 and rows[0]["estimated_usage"] == {"tool_search": 1} and rows[0]["synthetic"]
    assert rows[0]["actual_usage"] == {}
    assert KEY not in json.dumps(rows) and "synthetic query" not in json.dumps(rows)


@pytest.mark.parametrize("name", ["deepseek_search", "deepseek_fetch"])
def test_returned_body_becomes_fixed_source_and_real_locator(db, binding, network, name):
    content = "First retained line.\nSecond retained line.\n"
    network[1][0] = lambda request: httpx.Response(200,
        json={"results": [item(content)]} if name == "deepseek_search" else {"title": "Fetched page", "content": content})
    arguments = {"query": "synthetic query", "need_content": True} if name == "deepseek_search" else {"url": "https://synthetic.invalid/page"}
    result, _ = execute(db, binding, name, arguments)
    assert len(result["sources"]) == len(result["evidence"]) == 1
    evidence = result["evidence"][0]
    assert evidence["quote"] == content and evidence["bbox"] is None
    scope = source_store.SourceScope("conversation", CID)
    saved = source_store.read_revision(db, evidence["revision_id"], scope=scope, include_raw=True)
    assert saved["raw_bytes"] == content.encode() and saved["text_origin"] == "web_text"
    locator = source_store.read_locator(db, evidence["locator_id"], scope=scope)
    assert locator["quote"] == content and locator["revision_id"] == evidence["revision_id"]
    assert read_document(db, evidence["revision_id"], scope=scope)["original_page_coverage_verified"] is False
    assert "id" not in evidence  # executor assigns this run's unique eN


@pytest.mark.parametrize("change", ["revoke", "rotate", "policy", "archive", "cancel"])
def test_approval_to_send_revalidation_blocks_stale_authorization(db, binding, network, change):
    if change == "cancel":
        binding = {**binding, "run_id": "cancelled-service-run"}
    args = {"query": "synthetic query"}
    prepared = service.prepare_deepseek_tool(db, "deepseek_search", args, conversation_id=CID, operation_key="before-send", binding=binding)
    if change == "revoke":
        assert client.delete("/api/service-tools/deepseek/credential?expected_version=1").status_code == 200
    elif change == "rotate":
        assert settings(expected_version=1, api_key="Synthetic-Rotated-Key").status_code == 200
    elif change == "policy":
        row = db.get(ConversationOptions, CID)
        row.revision += 1
        db.commit()
    elif change == "archive":
        db.get(Conversation, CID).archived_at = utcnow()
        db.commit()
    else:
        from app import cancel_store
        cancel_store.request_cancel(binding["run_id"])
    result, _ = execute(db, binding, prepared=prepared)
    assert result["code"] == "service_authorization_invalid" and not network[0]
    assert not call_ledger.list_calls(include_synthetic=True)["items"]


def test_revocation_during_response_stops_transfer_and_never_retries(db, binding, network):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"results":'
            assert client.delete("/api/service-tools/deepseek/credential?expected_version=1").status_code == 200
            yield b'[]}'
    network[1][0] = lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=Chunks())
    result, prepared = execute(db, binding)
    assert result["code"] == "authorization_revoked" and result["may_be_billed"] is None
    replay, _ = execute(db, binding, prepared=prepared)
    assert replay["replayed_receipt"] and len(network[0]) == 1
    assert call_ledger.list_calls(include_synthetic=True)["items"][0]["state"] == "unknown"


def test_started_receipt_after_crash_is_not_resubmitted(db, binding, network):
    args = {"query": "synthetic query"}
    prepared = service.prepare_deepseek_tool(db, "deepseek_search", args, conversation_id=CID, operation_key="crash-window", binding=binding)
    row = db.get(ServiceToolInvocation, prepared.receipt_id)
    row.state = "started"
    db.commit()
    result, _ = execute(db, binding, prepared=prepared)
    assert result["code"] == "outcome_unknown" and result["automatic_retry"] is False and not network[0]


def test_receipt_cannot_cross_conversation_arguments_run_or_attempt(db, binding, network):
    result, prepared = execute(db, binding)
    with bind_runtime_config({**binding, "run_id": "another-run"}):
        wrong = json.loads(service.execute_deepseek_tool(db, "deepseek_search", {"query": "synthetic query"}, conversation_id=CID, prepared=prepared))
    assert wrong["code"] == "service_authorization_invalid"
    wrong, _ = execute(db, binding, arguments={"query": "different"}, prepared=prepared)
    assert wrong["code"] == "service_authorization_invalid"
    with bind_runtime_config(binding):
        wrong = json.loads(service.execute_deepseek_tool(db, "deepseek_search", {"query": "synthetic query"}, conversation_id="other", prepared=prepared))
    assert wrong["code"] == "service_authorization_invalid" and len(network[0]) == 1


def source(db, sample_pdf, binding):
    scope = source_store.SourceScope("conversation", CID)
    saved = source_store.capture_source(db, scope=scope,
        authorization=source_store.SourceAuthorization(scope, "synthetic-upload-approval", "upload"),
        kind="upload", title="Synthetic original.pdf", raw_bytes=sample_pdf.read_bytes(), media_type="application/pdf")
    db.commit()
    return saved, freeze_sources(db, CID, [saved["revision_id"]], binding)


def ocr_response():
    return {"id": "synthetic-ocr-result", "object": "ocr.result", "created": 1762790000,
        "purpose": "rich_layout_recognition", "page_count": 1,
        "pages": [{"index": 0, "text": "<｜ref｜>retained OCR text<｜/ref｜><｜box｜>[[10,20,200,100]]<｜/box｜>", "finish_reason": "stop"}],
        "usage": {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18}}


def test_ocr_approves_fixed_selected_original_and_preserves_unmapped_geometry(db, binding, network, sample_pdf):
    saved, binding = source(db, sample_pdf, binding)
    args = {"source_revision_id": saved["revision_id"], "purpose": "rich_layout_recognition"}
    prepared = service.prepare_deepseek_tool(db, "deepseek_ocr", args, conversation_id=CID, operation_key="ocr-test", binding=binding)
    assert prepared.approval_arguments["original_sha256"] == hashlib.sha256(sample_pdf.read_bytes()).hexdigest()
    assert not prepared.approval_arguments["coordinates_mapped_to_original"]
    network[1][0] = lambda request: httpx.Response(200, json=ocr_response())
    result, _ = execute(db, binding, "deepseek_ocr", args, prepared=prepared)
    assert result["page_count"] == 1 and not result["coordinates"]["mapped_to_source"]
    assert result["original_revision_id"] == saved["revision_id"]
    evidence = result["evidence"][0]
    assert evidence["revision_id"] != saved["revision_id"] and evidence["bbox"] is None and evidence["page"] == 0
    scope = source_store.SourceScope("conversation", CID)
    original = source_store.read_revision(db, saved["revision_id"], scope=scope, include_raw=True)
    derived = source_store.read_revision(db, evidence["revision_id"], scope=scope, include_raw=True)
    assert original["text"] is None and derived["text_origin"] == "ocr_text"
    assert original["raw_bytes"] == derived["raw_bytes"] == sample_pdf.read_bytes()
    assert original["current_revision_id"] == saved["revision_id"]
    doc = read_document(db, evidence["revision_id"], scope=scope)
    assert doc["ocr_pages"][0]["regions"] and not doc["coordinates"]["mapped_to_source"]
    ledger, = call_ledger.list_calls(include_synthetic=True)["items"]
    assert ledger["estimated_usage"] == {"ocr_pages": 1} and ledger["actual_usage"] == {}
    assert len(network[0]) == 1 and network[0][0].url.path == "/v1/tools/ocr"
    assert b'name="purpose"' in network[0][0].content and sample_pdf.read_bytes() in network[0][0].content


def test_ocr_rejects_unselected_original_and_tampered_fixed_version_before_charge(db, binding, network, sample_pdf):
    saved, selected = source(db, sample_pdf, binding)
    args = {"source_revision_id": saved["revision_id"]}
    with pytest.raises(source_store.SourceAccessDenied):
        service.prepare_deepseek_tool(db, "deepseek_ocr", args, conversation_id=CID, operation_key="unselected", binding=binding)
    prepared = service.prepare_deepseek_tool(db, "deepseek_ocr", args, conversation_id=CID, operation_key="tampered", binding=selected)
    db.execute(text("UPDATE source_snapshots SET raw_bytes=:raw"), {"raw": b"tampered"})
    db.commit()
    result, _ = execute(db, selected, "deepseek_ocr", args, prepared=prepared)
    assert result["code"] == "service_authorization_invalid" and not network[0]


@pytest.mark.parametrize("decision,mode,expected_calls", [
    ("approve", "always", 1), ("deny", "always", 0), ("revoke", "always", 0),
    ("approve", "full_access", 1),
])
def test_real_executor_prepares_approval_and_dispatches_only_current_authorization(
        db, binding, network, monkeypatch, decision, mode, expected_calls):
    from app import approval_store, mcp_policy
    from app.agent import loop
    from app.agent.checkpoint import AgentState
    from app.llm import LLMResponse
    monkeypatch.setattr(mcp_policy, "load_policy", lambda: {"enabled_tools": [], "disabled_tools": [], "tool_rules": {}})
    monkeypatch.setattr(approval_store, "_pending", {})
    monkeypatch.setattr(loop.settings, "memory_capture", False)
    db.get(ConversationOptions, CID).approval_mode = mode
    db.commit()
    binding = {**binding, "approval_mode": mode, "mode": "mock", "model_id": "Synthetic-Unrelated-Chat-Model"}

    class Model:
        def stream_chat(self, messages, tools=None):
            if not any(m["role"] == "tool" for m in messages):
                yield "final", LLMResponse("", [{"id": "synthetic-search-call", "name": "deepseek_search",
                    "arguments": {"query": "synthetic query"}}])
            else:
                yield "final", LLMResponse("本轮流程核验结束。")

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    from app.goal_ledger import ensure_run_goal
    from app.runtime_models import RuntimeRun
    goal = ensure_run_goal(db, "synthetic-service-executor", CID, "q")
    db.add(RuntimeRun(id="synthetic-service-executor", kind="foreground", conversation_id=CID, goal_id=goal["id"]))
    db.commit()
    snapshot = AgentState(phase="model", question="q", conversation_id=CID, runtime_config=binding).snapshot()
    approvals, tool_results = [], []
    for kind, event in loop.run_agent(db, "q", conversation_id=CID, run_id="synthetic-service-executor", checkpoint=snapshot):
        if kind == "approval_request":
            approvals.append(event)
            assert not network[0] and event["arguments"]["configuration_version"] == 1
            assert KEY not in json.dumps(event) and "credential_ref" not in json.dumps(event)
            assert event["arguments"]["protocol"] == "independent_tool_api"
            if decision == "revoke":
                assert client.delete("/api/service-tools/deepseek/credential?expected_version=1").status_code == 200
            approval_store.resolve(event["approval_id"], "deny" if decision == "deny" else "approve")
        if kind == "tool_result":
            tool_results.append(event)
    assert len(network[0]) == expected_calls and len(approvals) == (0 if mode == "full_access" else 1)
    assert len(tool_results) == 1
    if expected_calls:
        ledger, = call_ledger.list_calls(include_synthetic=True)["items"]
        assert ledger["run_id"] == "synthetic-service-executor" and ledger["state"] == "completed"
    else:
        assert not call_ledger.list_calls(include_synthetic=True)["items"]
