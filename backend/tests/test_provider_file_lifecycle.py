"""Cloud file lifecycle with a synthetic server; no real files leave the process."""
from datetime import timedelta
import json
import re
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import provider_files as files, service_tool_config as config, source_store, service_tools
from app.db import SessionLocal
from app.models import Conversation, ConversationOptions
from app.provider_models import ProviderFileBinding, ProviderOperationContext
from app.providers.deepseek_files import FilesClient
from app.runtime_context import bind_runtime_config
from app.source_runtime import freeze_sources
from app.timeutil import utcnow

CID = "cloud-file-lifecycle"
KEY = "synthetic-cloud-files-key"
RAW = b"# Synthetic markdown\nMeasured value: 17.\n"


@pytest.fixture
def setup(db, monkeypatch):
    db.add(Conversation(id=CID))
    db.add(ConversationOptions(conversation_id=CID, approval_mode="always", revision=1))
    config.save_configuration(db, {"expected_version": 0, "enabled_services": ["files"], "api_key": KEY})
    scope = source_store.SourceScope("conversation", CID)
    record = source_store.capture_source(db, scope=scope,
        authorization=source_store.SourceAuthorization(scope, "test:cloud-file", "upload"), kind="upload",
        title="sample.md", media_type="text/markdown", raw_bytes=RAW, text=RAW.decode(), text_origin="provided_text")
    db.commit()
    requests, remote = [], {}
    behavior = {"fail_upload": False, "fail_delete": False, "no_expiry": False, "processed_size": False, "bad_identity": False, "excess_expiry": False}
    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + KEY
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content)
            assert payload["model"] == "deepseek-flash"
            block = payload["messages"][-1]["content"][1]
            assert block == {"type": "file", "file_id": "file-api-synthetic-lifecycle"}
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "The value is 17.",
                "reasoning_content": "private file reasoning"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 3}})
        if request.method == "POST":
            body = request.content.decode()
            assert body.index('name="purpose"') < body.index('name="file"')
            assert 'name="expires_after[anchor]"\r\n\r\ncreated_at' in body
            assert RAW.decode() in body
            filename = re.search(r'filename="([^"]+)"', body).group(1)
            now = int(time.time())
            remote.update(id="file-api-synthetic-lifecycle", object="file", filename=filename,
                bytes=len(RAW), purpose="user_data", created_at=now, expires_at=now + 86400)
            if behavior["processed_size"]:
                remote["bytes"] = len(RAW) - 1
            if behavior["bad_identity"]:
                remote["filename"] = "unrelated.md"
            if behavior["excess_expiry"]:
                remote["expires_at"] += 86400
            if behavior["no_expiry"]:
                remote.pop("expires_at")
            if behavior["fail_upload"]:
                raise httpx.ReadError("synthetic response lost")
            return httpx.Response(200, json=remote)
        if request.method == "DELETE":
            if behavior["fail_delete"]:
                return httpx.Response(503, json={"error": {"message": "synthetic private payload"}})
            return httpx.Response(200, json={"id": remote["id"], "deleted": True, "object": "file"})
        if request.url.path == "/files":
            return httpx.Response(200, json={"object": "list", "data": [remote] if remote else [], "has_more": False})
        return httpx.Response(200, json=remote)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(files, "FilesClient", lambda key, **kw: FilesClient(key, transport=transport, synthetic=True, **kw))
    return record, requests, behavior, transport


def prepare(record, request="request-synthetic-cloud"):
    return files.prepare(CID, record["revision_id"], "user_data", 86400, request)


def test_file_prepare_local_then_upload_is_ordered_fixed_and_idempotent(db, setup):
    record, requests, _, _ = setup
    row = prepare(record)
    assert not requests and row["raw_sha256"] == record["raw_sha256"]
    assert prepare(record)["id"] == row["id"]
    uploaded = files.upload(CID, row["id"], row["version"])
    assert uploaded["state"] == "available" and uploaded["identity_verified"]
    assert uploaded["expires_at"] and len(requests) == 1
    assert files.upload(CID, row["id"], row["version"])["id"] == row["id"]
    assert len(requests) == 1
    assert KEY not in json.dumps(files.list_bindings(CID))
    assert source_store.read_revision(db, record["revision_id"], scope=source_store.SourceScope("conversation", CID), include_raw=True)["raw_bytes"] == RAW


def test_file_read_uses_bound_id_and_requires_selected_fixed_original(db, setup, monkeypatch):
    record, requests, _, transport = setup
    row = prepare(record)
    uploaded = files.upload(CID, row["id"], row["version"])
    binding = freeze_sources(db, CID, [record["revision_id"]], {"conversation_id": CID, "approval_mode": "always", "policy_revision": 1})
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, **kw, transport=transport))
    args = {"source_revision_id": record["revision_id"], "query": "Read synthetic file"}
    operation = service_tools.prepare_deepseek_tool(db, "deepseek_file_read", args, operation_key="file:read", binding=binding)
    with bind_runtime_config(binding):
        result = json.loads(service_tools.execute_deepseek_tool(db, "deepseek_file_read", args, prepared=operation))
    assert result.get("answer") == "The value is 17.", result
    assert result["evidence"] == [] and result["original_sha256"] == record["raw_sha256"]
    assert "private file reasoning" not in json.dumps(result)
    with SessionLocal() as check:
        assert check.get(ProviderOperationContext, operation.receipt_id) is not None
    with pytest.raises(source_store.SourceAccessDenied):
        files.require_for_read(db, row["id"], CID, {"conversation_id": CID})
    assert len(requests) == 2


def test_unknown_upload_cannot_be_replayed_and_reconciliation_is_cleanup_only(db, setup):
    record, requests, behavior, _ = setup
    row = prepare(record)
    behavior["fail_upload"] = True
    with pytest.raises(files.FileBindingError):
        files.upload(CID, row["id"], row["version"])
    current = files.list_bindings(CID)["items"][0]
    assert current["state"] == "unknown"
    assert prepare(record, "request-new-but-same-file")["id"] == row["id"]
    with pytest.raises(files.FileBindingError):
        files.upload(CID, row["id"], current["version"])
    assert len(requests) == 1
    reconciled = files.operate(CID, row["id"], current["version"], "reconcile")
    assert reconciled["state"] == "unknown" and len(reconciled["candidates"]) == 1
    linked = files.operate(CID, row["id"], reconciled["version"], "adopt_for_cleanup", candidate_id=reconciled["candidates"][0]["id"])
    assert linked["state"] == "remote_found_unknown" and not linked["identity_verified"]
    with pytest.raises(files.FileBindingError):
        files.require_for_read(db, row["id"], CID, {})
    deleted = files.operate(CID, row["id"], linked["version"], "delete")
    assert deleted["state"] == "deleted" and len(requests) == 3


def test_failed_delete_remains_pending_then_explicit_retry_deletes(db, setup):
    record, requests, behavior, _ = setup
    row = prepare(record)
    current = files.upload(CID, row["id"], row["version"])
    behavior["fail_delete"] = True
    with pytest.raises(files.FileBindingError):
        files.operate(CID, row["id"], current["version"], "delete")
    pending = files.list_bindings(CID)["items"][0]
    assert pending["state"] == "delete_pending" and "synthetic private payload" not in json.dumps(pending)
    behavior["fail_delete"] = False
    assert files.operate(CID, row["id"], pending["version"], "delete")["state"] == "deleted"


def test_expiration_and_revocation_prevent_read_and_upload_without_network(db, setup):
    record, requests, _, _ = setup
    row = prepare(record)
    current = files.upload(CID, row["id"], row["version"])
    with SessionLocal.begin() as check:
        check.get(ProviderFileBinding, row["id"]).expires_at = utcnow() - timedelta(seconds=1)
    assert files.list_bindings(CID)["items"][0]["state"] == "expired"
    with pytest.raises(files.FileBindingError):
        files.require_for_read(db, row["id"], CID, {})
    with SessionLocal.begin() as check:
        config.revoke_credential(check, 1)
    with pytest.raises(files.FileBindingError, match="撤销"):
        files.operate(CID, row["id"], current["version"], "delete")
    assert len(requests) == 1


def test_missing_remote_expiry_keeps_handle_but_not_available(db, setup):
    record, _, behavior, _ = setup
    behavior["no_expiry"] = True
    row = prepare(record)
    result = files.upload(CID, row["id"], row["version"])
    assert result["state"] == "unknown" and result["remote_file_id"] and not result["identity_verified"]


def test_direct_upload_binds_source_receipt_without_claiming_remote_byte_equality(db, setup):
    record, requests, behavior, _ = setup
    behavior["processed_size"] = True
    row = prepare(record)
    result = files.upload(CID, row["id"], row["version"])
    assert result["state"] == "available" and result["identity_verified"]
    assert result["identity_basis"] == "direct_upload_receipt"
    assert result["remote_size_bytes"] == len(RAW) - 1
    assert result["remote_size_matches_original"] is False
    assert result["remote_original_bytes_verified"] is False
    assert result["raw_sha256"] == record["raw_sha256"]
    refreshed = files.operate(CID, row["id"], result["version"], "refresh")
    assert refreshed["state"] == "available" and refreshed["remote_size_bytes"] == len(RAW) - 1
    assert len(requests) == 2


def test_processed_size_lost_response_stays_unknown_and_cleanup_only(db, setup):
    record, _, behavior, _ = setup
    behavior.update(processed_size=True, fail_upload=True)
    row = prepare(record)
    with pytest.raises(files.FileBindingError):
        files.upload(CID, row["id"], row["version"])
    result = files.list_bindings(CID)["items"][0]
    result = files.operate(CID, row["id"], result["version"], "reconcile")
    assert len(result["candidates"]) == 1 and not result["identity_verified"]
    result = files.operate(CID, row["id"], result["version"], "adopt_for_cleanup", candidate_id=result["candidates"][0]["id"])
    assert result["state"] == "remote_found_unknown" and not result["identity_verified"]
    with pytest.raises(files.FileBindingError):
        files.require_for_read(db, row["id"], CID, {})


@pytest.mark.parametrize("failure", ["bad_identity", "excess_expiry"])
def test_direct_receipt_does_not_relax_filename_or_retention_policy(db, setup, failure):
    record, _, behavior, _ = setup
    behavior[failure] = True
    row = prepare(record)
    result = files.upload(CID, row["id"], row["version"])
    assert result["state"] == "unknown" and not result["identity_verified"]


def test_changed_remote_metadata_on_refresh_revokes_read_binding(db, setup, monkeypatch):
    record, _, _, _ = setup
    row = prepare(record)
    result = files.upload(CID, row["id"], row["version"])
    with SessionLocal() as check:
        old = dict(check.get(ProviderFileBinding, row["id"]).remote_metadata)
    monkeypatch.setattr(FilesClient, "retrieve", lambda self, file_id: {**old, "bytes": len(RAW) + 100})
    with pytest.raises(files.FileBindingError):
        files.operate(CID, row["id"], result["version"], "refresh")
    current = files.list_bindings(CID)["items"][0]
    assert current["state"] == "unknown" and not current["identity_verified"]


def test_file_api_scope_and_local_preview(db, setup):
    record, requests, _, _ = setup
    client = TestClient(app)
    result = client.post("/api/provider-files/prepare", json={"conversation_id": CID, "source_revision_id": record["revision_id"],
        "purpose": "user_data", "expires_seconds": 86400, "request_id": "api-file-preview"})
    assert result.status_code == 200 and not requests and KEY not in result.text
    assert client.get("/api/provider-files", params={"conversation_id": "other"}).status_code == 404
    with pytest.raises(files.FileBindingError):
        files.prepare(CID, record["revision_id"], "text_recognition", 86400, "bad-purpose-request")
    assert not requests


def test_revoked_credential_manual_cleanup_requires_explicit_unverified_user_statement(db, setup):
    record, requests, _, _ = setup
    row = prepare(record)
    current = files.upload(CID, row["id"], row["version"])
    with SessionLocal.begin() as check:
        config.revoke_credential(check, 1)
    with pytest.raises(files.FileBindingError, match="明确声明"):
        files.operate(CID, row["id"], current["version"], "acknowledge_manual_cleanup")
    result = files.operate(CID, row["id"], current["version"], "acknowledge_manual_cleanup", confirm_manual_cleanup=True)
    assert result["state"] == "manual_cleanup_acknowledged"
    assert result["cleanup_verification"] == "user_reported_unverified" and len(requests) == 1
    with SessionLocal() as check:
        assert check.get(ProviderFileBinding, row["id"]).remote_metadata["manual_cleanup_confirmation"]["provider_verified"] is False
