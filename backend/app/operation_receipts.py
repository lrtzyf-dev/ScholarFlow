"""Exact user reconciliation over existing checkpoints, never provider proof."""
from copy import deepcopy
import hashlib
import json


def signature(name, arguments):
    return hashlib.sha256(json.dumps([name, arguments], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def effect_record(state, sig):
    return next((failure for failure in reversed(state.tool_failures)
        if failure.get("signature") == sig and failure.get("execution_unknown") and failure.get("status") == "open"), None)


def retry_permission(state, sig):
    failure = effect_record(state, sig)
    if not failure:
        return None
    receipts = failure.get("reconciliations") or []
    if not receipts or receipts[-1].get("action") != "not_executed":
        return None
    receipt = receipts[-1]
    if receipt.get("attempt_id") != state.attempt_id:
        # Safe recovery admits a new attempt. Its checkpoint explicitly names
        # the old attempt; a takeover without that recovery cannot borrow it.
        if receipt.get("attempt_id") != state.source_attempt_id:
            return None
    consumed = failure.get("retry_consumptions") or []
    if any(item.get("reconciliation_id") == receipt.get("id") for item in consumed):
        return None
    return failure, receipt


def consume_retry(state, sig, operation_key, persist):
    pending = state.pending_operation or {}
    if (pending.get("kind") != "tool" or pending.get("key") != operation_key
            or not isinstance(pending.get("name"), str) or not isinstance(pending.get("arguments"), dict)
            or signature(pending["name"], pending["arguments"]) != sig):
        return False
    permission = retry_permission(state, sig)
    if permission is None:
        return False
    failure, receipt = permission
    from .timeutil import utcnow
    failure.setdefault("retry_consumptions", []).append({"reconciliation_id": receipt["id"],
        "operation_key": operation_key, "signature": sig, "attempt_id": state.attempt_id,
        "created_at": utcnow().isoformat(), "provider_verified": False})
    # The pending operation has already been committed. The single-use permit
    # must be committed too before entering an adapter that may dispatch.
    persist()
    return True


def public_receipt(receipt):
    return {key: deepcopy(receipt.get(key)) for key in ("id", "request_id", "attempt_id", "operation_key",
        "signature", "action", "reference", "created_at", "provider_verified")}
