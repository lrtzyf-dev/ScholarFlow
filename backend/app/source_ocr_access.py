"""A local-only read permit for one completed OCR derivative, never a dispatch.

No service client or credential decryptor is imported. The selected immutable
original, producing invocation, call receipt and retained extraction must agree.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import source_store as store
from .source_documents import read_document


def require_ocr_derivative(db, revision_id, *, scope, binding, frozen):
    from . import cancel_store
    from .ledger_models import CallRecord
    from .model_profiles import current_policy
    from .runtime_models import RuntimeRun, RunAttempt
    from .service_tool_models import ServiceToolInvocation
    from .source_models import SourceRevision

    def denied():
        raise store.SourceAccessDenied("此OCR派生版本没有当前运行有效的本地读取授权")

    run_id = binding.get("run_id")
    if not isinstance(run_id, str) or not run_id or cancel_store.is_cancelled(run_id):
        denied()
    run = db.get(RuntimeRun, run_id, populate_existing=True)
    if (run is None or run.conversation_id != scope.scope_id or run.status != "running"
            or not run.current_attempt_id or run.current_attempt_id != binding.get("attempt_id")):
        denied()
    attempt = db.get(RunAttempt, run.current_attempt_id, populate_existing=True)
    if attempt is None or attempt.run_id != run_id or attempt.status != "running":
        denied()
    from .goal_policy import tool_denial
    if tool_denial("read_attached_source", {"source_revision_id": revision_id}, binding=binding):
        denied()

    record = store.read_revision(db, revision_id, scope=scope, include_raw=True)
    authorization_id = record.get("authorization_id")
    if not isinstance(authorization_id, str) or not re.fullmatch(r"service:[a-f0-9]{32}", authorization_id):
        denied()
    receipt_id = authorization_id.removeprefix("service:")
    receipt = db.get(ServiceToolInvocation, receipt_id, populate_existing=True)
    if (receipt is None or receipt.provider != "deepseek" or receipt.service != "ocr"
            or receipt.conversation_id != scope.scope_id or receipt.run_id != run_id
            or receipt.state != "completed" or receipt.finished_at is None
            or not isinstance(receipt.authorization, dict) or not isinstance(receipt.result, dict)):
        denied()
    producing_attempt = db.get(RunAttempt, receipt.attempt_id, populate_existing=True) if receipt.attempt_id else None
    if producing_attempt is None or producing_attempt.run_id != run_id:
        denied()
    authorization, result = receipt.authorization, receipt.result
    original_id = authorization.get("revision_id")
    original = next((item for item in frozen if item["revision_id"] == original_id), None)
    saved = authorization.get("source_binding")
    if (original is None or not isinstance(saved, dict) or saved.get("conversation_id") != scope.scope_id
            or not isinstance(saved.get("source_revision_ids"), list) or original_id not in saved["source_revision_ids"]
            or not isinstance(saved.get("source_bindings"), list)):
        denied()
    saved_original = next((item for item in saved["source_bindings"]
                           if isinstance(item, dict) and item.get("revision_id") == original_id), None)
    identity = ("source_id", "revision_id", "revision_fingerprint", "raw_sha256", "extraction_fingerprint")
    if saved_original is None or any(key not in saved_original or saved_original[key] != original[key] for key in identity):
        denied()
    policy = current_policy(db, scope.scope_id, binding)
    if (authorization.get("policy") != policy
            or any(binding.get(key) != policy[key] for key in ("approval_mode", "policy_revision"))):
        denied()

    # The producing receipt records its historical authorization. Rotating or
    # deleting an external credential must not revoke retained local data; no
    # credential/configuration row or decryptor is consulted for this read.
    if (type(receipt.configuration_version) is not int or receipt.configuration_version < 1
            or not isinstance(receipt.credential_ref, str) or not re.fullmatch(r"[a-f0-9]{32}", receipt.credential_ref)):
        denied()

    purpose = result.get("purpose")
    arguments_hash = hashlib.sha256(json.dumps({"source_revision_id": original_id, "purpose": purpose},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if (purpose not in {"text_recognition", "rich_layout_recognition"} or receipt.arguments_sha256 != arguments_hash
            or result.get("error") or result.get("original_revision_id") != original_id
            or result.get("original_sha256") != authorization.get("raw_sha256")
            or authorization.get("raw_sha256") != original["raw_sha256"]
            or record["raw_sha256"] != original["raw_sha256"]
            or record.get("origin_ref") != "ocr-of:" + original_id or record.get("text_origin") != "ocr_text"
            or not isinstance(record.get("text"), str) or not record["text"].strip()
            or not isinstance(record.get("parser"), str) or not record["parser"].startswith("deepseek-ocr/")):
        denied()
    produced = result.get("sources")
    fields = ("source_id", "revision_id", "snapshot_id", "version", "media_type", "raw_sha256", "text_sha256",
              "snapshot_fingerprint", "authorization_id", "origin_ref", "text_origin", "parser")
    if (not isinstance(produced, list) or len(produced) != 1 or not isinstance(produced[0], dict)
            or any(key not in produced[0] or produced[0][key] != record[key] for key in fields)):
        denied()
    call = db.get(CallRecord, receipt.call_id, populate_existing=True) if receipt.call_id else None
    if (call is None or result.get("call_id") != receipt.call_id or call.state != "completed"
            or call.provider != "deepseek" or call.service != "independent_ocr"
            or call.request_id != receipt_id or call.run_id != run_id or call.attempt_id != receipt.attempt_id
            or call.endpoint != "https://api.deepseek.com/v1/tools/ocr"):
        denied()
    doc = read_document(db, revision_id, scope=scope)
    coordinates = doc.get("coordinates")
    if (doc.get("provider") != "deepseek" or doc.get("provider_format") != "ocr" or doc.get("format") != "text"
            or doc.get("original_revision_id") != original_id or doc.get("text") != record["text"]
            or doc.get("text_sha256") != record["text_sha256"] or not isinstance(doc.get("ocr_pages"), list)
            or not isinstance(coordinates, dict) or coordinates.get("mapped_to_source") is not False
            or result.get("coordinates") != coordinates):
        denied()
    revision = db.get(SourceRevision, revision_id, populate_existing=True)
    record["local_read_authorization"] = {"version": "completed_ocr_local_read_v1", "scope": "retained_ocr_text_only",
        "provider": "deepseek", "service": "ocr", "service_receipt_id": receipt_id, "call_id": receipt.call_id,
        "run_id": run_id, "producing_attempt_id": receipt.attempt_id, "reading_attempt_id": binding.get("attempt_id"),
        "original_source_id": original["source_id"], "original_revision_id": original_id,
        "original_raw_sha256": original["raw_sha256"], "original_revision_fingerprint": original["revision_fingerprint"],
        "derived_revision_id": revision_id, "derived_revision_fingerprint": revision.fingerprint,
        "derived_raw_sha256": record["raw_sha256"], "derived_text_sha256": record["text_sha256"],
        "derived_extraction_fingerprint": doc["fingerprint"], "text_read": False,
        "original_coordinates_mapped": False, "authorizes_external_transfer": False,
        "authorizes_paid_calls": False, "frozen_source_selection_changed": False}
    return record
