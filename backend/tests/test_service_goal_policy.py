"""Actual DeepSeek service exits; credentials, cloud binding and HTTP are synthetic."""
from contextlib import contextmanager
from datetime import timedelta
import json

import httpx
import pytest

from app import service_tools, service_tool_config, source_store, request_resources, resource_budget
from app.db import SessionLocal
from app.models import ConversationOptions
from app.provider_models import ProviderFileBinding
from app.source_runtime import freeze_sources
from app.runtime_context import bind_runtime_config
from app.service_tool_models import ServiceToolInvocation
from app.ledger_models import CallRecord
from app.timeutil import utcnow
from tests.test_task_requirements import base, change


@pytest.fixture
def service_case(db, monkeypatch):
    goal = base(db)
    db.add(ConversationOptions(conversation_id="criteria-a", approval_mode="always", revision=1))
    service_tool_config.save_configuration(db, {"expected_version": 0,
        "enabled_services": ["search", "fetch", "ocr", "research", "files"], "api_key": "synthetic-policy-service-only"})
    scope = source_store.SourceScope("conversation", "criteria-a")
    record = source_store.capture_source(db, scope=scope,
        authorization=source_store.SourceAuthorization(scope, "synthetic-policy-source", "upload"), kind="upload",
        title="synthetic.png", media_type="image/png", raw_bytes=b"synthetic image data", text="", text_origin="provided_text")
    db.commit()
    binding = freeze_sources(db, "criteria-a", [record["revision_id"]], {
        "conversation_id": "criteria-a", "run_id": "criteria-run", "goal_id": goal["id"],
        "approval_mode": "always", "policy_revision": 1})
    selected = service_tool_config.require_configuration(db, "files")
    from app.model_profiles import current_policy
    db.add(ProviderFileBinding(conversation_id="criteria-a", revision_id=record["revision_id"],
        raw_sha256=record["raw_sha256"], title="synthetic.png", media_type="image/png", size_bytes=20,
        purpose="user_data", expires_seconds=86400, request_id="synthetic-cloud-binding",
        configuration_version=selected["version"], credential_ref=selected["credential_ref"],
        policy=current_policy(db, "criteria-a", binding), state="available", identity_verified=1,
        remote_file_id="file-synthetic-policy", expires_at=utcnow() + timedelta(days=1),
        remote_metadata={"bytes": 20, "filename": record["revision_id"] + ".png"}))
    db.commit()
    budget = resource_budget.create_budget(owner_run_id="criteria-run", amount_limit="1", request_estimate="0.4", price_version="synthetic-service-estimate")
    binding["resource_budget_id"] = budget
    calls, on_send = [], [lambda: None]
    def transport(request):
        calls.append(request)
        on_send[0]()
        if request.url.path.endswith("chat/completions"):
            if json.loads(request.content).get("stream"):
                from tests.test_deepseek_native_research import events
                return httpx.Response(200, text=events(), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Synthetic file understanding."},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
        if request.url.path.endswith("search"): return httpx.Response(200, json={"results": []})
        if request.url.path.endswith("fetch"): return httpx.Response(200, json={"title": "Synthetic page", "content": "A retained line.\n"})
        return httpx.Response(200, json={"id": "synthetic-policy-ocr", "object": "ocr.result", "created": 1762790000,
            "purpose": "text_recognition", "page_count": 1,
            "pages": [{"index": 0, "text": "Retained OCR observation.\n", "finish_reason": "stop"}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: original(*a, transport=httpx.MockTransport(transport), **{key:value for key,value in k.items() if key != "transport"}))
    return goal, record, binding, budget, calls, on_send


def args_for(name, record):
    if name == "deepseek_fetch": return {"url": "https://example.test/synthetic"}
    if name in ("deepseek_ocr", "deepseek_file_read"):
        return {"source_revision_id": record["revision_id"], **({"query": "Read synthetic file"} if name.endswith("file_read") else {})}
    return {"query": "Synthetic research"}


def execute_case(db, record, binding, name, operation):
    arguments = args_for(name, record)
    prepared = service_tools.prepare_deepseek_tool(db, name, arguments, binding=binding, operation_key=operation)
    with bind_runtime_config(binding):
        result = json.loads(service_tools.execute_deepseek_tool(db, name, arguments, prepared=prepared))
    return result, prepared


@pytest.mark.parametrize("name,rule", [(name, "no_external_tools") for name in service_tools.DEEPSEEK_TOOL_NAMES] +
    [("deepseek_ocr", "no_file_read"), ("deepseek_file_read", "no_file_read")])
def test_service_queue_goal_change_blocks_actual_http_and_releases_budget(db, monkeypatch, service_case, name, rule):
    goal, record, binding, budget, calls, _ = service_case
    admitted = request_resources.request_admission
    @contextmanager
    def policy_while_waiting(**kwargs):
        with admitted(**kwargs) as receipt:
            with SessionLocal() as other:
                from app.goal_ledger import snapshot_goal
                change(other, snapshot_goal(other, goal["id"]), {"kind": "policy", "rule": rule}); other.commit()
            yield receipt
    monkeypatch.setattr(request_resources, "request_admission", policy_while_waiting)
    result, prepared = execute_case(db, record, binding, name, "queued:" + name + ":" + rule)
    assert calls == [] and result["may_be_billed"] is False, result
    assert result["code"] in ("goal_constraint", "authorization_revoked")
    db.expire_all()
    assert db.get(ServiceToolInvocation, prepared.receipt_id).state == "failed"
    assert [row.state for row in db.query(CallRecord)] == ["failed"]
    assert resource_budget.budget_snapshot(budget)["reserved_amount"] == "0"


@pytest.mark.parametrize("name", ["deepseek_search", "deepseek_fetch", "deepseek_ocr", "deepseek_research", "deepseek_file_read"])
def test_service_goal_change_after_http_keeps_known_result_and_receipt(db, service_case, name):
    goal, record, binding, budget, calls, on_send = service_case
    def restrict_after_dispatch():
        with SessionLocal() as other:
            from app.goal_ledger import snapshot_goal
            change(other, snapshot_goal(other, goal["id"]), {"kind": "policy", "rule": "no_external_tools"}); other.commit()
    on_send[0] = restrict_after_dispatch
    result, prepared = execute_case(db, record, binding, name, "after-send:" + name)
    assert len(calls) == 1 and "error" not in result, result
    db.expire_all()
    assert db.get(ServiceToolInvocation, prepared.receipt_id).state == "completed"
    assert [row.state for row in db.query(CallRecord)] == ["completed"]
    assert resource_budget.budget_snapshot(budget)["reserved_amount"] == "0.4"


def test_service_current_goal_denial_prevents_even_cached_receipt_replay(db, service_case):
    goal, record, binding, budget, calls, _ = service_case
    result, prepared = execute_case(db, record, binding, "deepseek_search", "before-cached-read")
    assert "error" not in result and len(calls) == 1
    from app.goal_ledger import snapshot_goal
    goal = change(db, snapshot_goal(db, goal["id"]), {"kind": "policy", "rule": "no_external_tools"}); db.commit()
    with bind_runtime_config(binding):
        blocked = json.loads(service_tools.execute_deepseek_tool(db, "deepseek_search", args_for("deepseek_search", record), prepared=prepared))
    assert blocked["blocked_by_goal_constraint"] and blocked["not_executed"] and len(calls) == 1
