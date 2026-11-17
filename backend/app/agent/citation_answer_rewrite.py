"""One checkpoint-owned answer revision; no semantic approval or HTTP retry."""
from copy import deepcopy
import hashlib
import json
import re

from .answer_review_journal import cached_review
from .model_review import model_review_identity


PROTOCOL = "citation_answer_rewrite_v1"
FIELDS = ("protocol", "context_sha256", "request_sha256", "original_answer_sha256",
    "answer", "provider_context", "usage", "status", "error_code")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _public(status="unverified", *, changed=False, error_code=None):
    return {"status": status, "changed": changed, "error_code": error_code,
        "semantic_verification": "not_performed"}


def _operation_key(context, original, request):
    return "citation_rewrite/" + _digest({"context": context, "original": original, "request": request})


def _valid_receipt(value, index):
    if not isinstance(value, dict) or set(value) != {*FIELDS, "receipt_sha256", "answer_applied", "usage_applied"}:
        return False
    if (value["protocol"] != PROTOCOL or any(value[key] != index[key] for key in (
            "context_sha256", "request_sha256", "original_answer_sha256"))
            or type(value["answer_applied"]) is not bool or type(value["usage_applied"]) is not bool
            or value["status"] not in ("ready", "unverified") or not isinstance(value["answer"], str)
            or not isinstance(value["provider_context"], dict) or not isinstance(value["usage"], dict)
            or any(key not in ("prompt_tokens", "completion_tokens", "total_tokens")
                or type(count) is not int or count < 0 for key, count in value["usage"].items())):
        return False
    try:
        if value["receipt_sha256"] != _digest({key: value[key] for key in FIELDS}):
            return False
    except (ValueError, TypeError):
        return False
    if value["status"] == "ready":
        context = value["provider_context"]
        assistant = context.get("assistant")
        return (bool(value["answer"].strip()) and value["error_code"] is None
            and context.get("version") == 1 and isinstance(assistant, dict)
            and assistant.get("role") == "assistant" and not assistant.get("tool_calls")
            and assistant.get("content") == value["answer"])
    return (value["error_code"] == "rewrite_response_invalid" and not value["answer"]
        and not value["provider_context"] and value["answer_applied"] is False)


def has_applied_citation_rewrite(journal):
    """Recover a committed semantic revision without relying on a new flag."""
    if not isinstance(journal, dict):
        return False
    for key, index in journal.items():
        if not isinstance(key, str) or not key.startswith("citation_rewrite_context/") or not isinstance(index, dict):
            continue
        if (set(index) != {"protocol", "context_sha256", "request_sha256", "original_answer_sha256", "operation_key"}
                or index.get("protocol") != PROTOCOL
                or any(not isinstance(index.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", index[field])
                    for field in ("context_sha256", "request_sha256", "original_answer_sha256"))
                or key != "citation_rewrite_context/" + index["context_sha256"]
                or index["operation_key"] != _operation_key(index["context_sha256"],
                    index["original_answer_sha256"], index["request_sha256"])):
            continue
        receipt = journal.get(index["operation_key"])
        if _valid_receipt(receipt, index) and receipt["status"] == "ready" and receipt["answer_applied"]:
            return True
    return False


def apply_citation_answer_rewrite(state, *, llm, request_messages, input_identity,
                                  call, persist, before_review, on_usage=None):
    """Persist a complete private response before cancellation or adoption.

    ``request_messages`` is the transformed request actually used by ``call``;
    the caller must invoke its dispatcher with attempts=1. ``input_identity``
    binds stable user/tool input, excluding regenerated audit feedback and run
    controls. One context index prevents feedback drift or adoption itself from
    purchasing a second revision. ``on_usage`` may update checkpointed state
    only; the authoritative paid-call ledger is separate.

    The return value is safe for public progress. Answers, provider replay data
    and receipts remain in AgentState; adoption always needs full gate/audit.
    """
    if not callable(call) or not callable(persist) or not callable(before_review):
        return _public(error_code="rewrite_journal_unavailable")
    if not isinstance(request_messages, list) or not isinstance(state.source_attribution_reviews, dict):
        return _public(error_code="rewrite_input_invalid")
    before_review()
    try:
        context_sha = _digest({"protocol": PROTOCOL, "question": state.question,
            "evidence": state.evidence, "input_identity": input_identity, "model": model_review_identity(llm)})
        request_sha, original_sha = _digest(request_messages), _digest(state.answer)
    except (ValueError, TypeError):
        return _public(error_code="rewrite_identity_invalid")
    durable = deepcopy(state.snapshot())

    def restore():
        for name, value in durable.items():
            setattr(state, name, deepcopy(value))

    def save():
        nonlocal durable
        try:
            persist()
        except BaseException:
            restore()
            raise
        durable = deepcopy(state.snapshot())

    journal = state.source_attribution_reviews
    index_key = "citation_rewrite_context/" + context_sha
    index = journal.get(index_key)
    if index is not None:
        if (not isinstance(index, dict) or set(index) != {"protocol", "context_sha256", "request_sha256", "original_answer_sha256", "operation_key"}
                or index["protocol"] != PROTOCOL or index["context_sha256"] != context_sha
                or any(not isinstance(index[key], str) or not re.fullmatch(r"[a-f0-9]{64}", index[key])
                    for key in ("request_sha256", "original_answer_sha256"))
                or index["operation_key"] != _operation_key(context_sha, index["original_answer_sha256"], index["request_sha256"])):
            return _public(error_code="rewrite_context_invalid")
        operation_key = index["operation_key"]
        # An index without intent cannot have dispatched: cached_review saves
        # pending before calling the supplier. Cancellation here is retryable.
        if operation_key not in journal:
            if original_sha != index["original_answer_sha256"]:
                return _public(error_code="rewrite_context_answer_changed")
            index = None
    if index is None:
        operation_key = _operation_key(context_sha, original_sha, request_sha)
        index = {"protocol": PROTOCOL, "context_sha256": context_sha,
            "request_sha256": request_sha, "original_answer_sha256": original_sha, "operation_key": operation_key}
        journal[index_key] = index
        save()

    dispatch_error = None
    def dispatch():
        nonlocal dispatch_error
        try:
            response = call()  # The caller supplies exactly one attempt.
            usage = getattr(response, "usage", None)
            counts = {key: value for key, value in (usage or {}).items()
                if key in ("prompt_tokens", "completion_tokens", "total_tokens") and type(value) is int and value >= 0} if isinstance(usage, dict) else {}
            answer = getattr(response, "content", None)
            ready = isinstance(answer, str) and bool(answer.strip()) and not getattr(response, "tool_calls", None)
            private = response.persisted_context() if ready else {}
            value = {"protocol": PROTOCOL, "context_sha256": context_sha,
                "request_sha256": index["request_sha256"], "original_answer_sha256": index["original_answer_sha256"],
                "answer": answer if ready else "", "provider_context": private,
                "usage": counts, "status": "ready" if ready else "unverified",
                "error_code": None if ready else "rewrite_response_invalid"}
            value.update(receipt_sha256=_digest(value), answer_applied=False, usage_applied=False)
            return value
        except BaseException as exc:
            dispatch_error = exc
            raise

    try:
        result = cached_review(journal, operation_key, review=dispatch,
            persist=save, before_review=before_review, llm=llm)
    except Exception as exc:
        if dispatch_error is not exc:
            raise  # A checkpoint failure must never masquerade as HTTP failure.
        from ..request_resources import raise_if_resource_boundary
        raise_if_resource_boundary(exc)
        return _public(error_code="dispatch_outcome_unknown")
    before_review()
    if isinstance(result, dict) and result.get("error_code") == "dispatch_outcome_unknown":
        return _public(error_code="dispatch_outcome_unknown")
    if not _valid_receipt(result, index):
        return _public(error_code="rewrite_receipt_invalid")
    current_sha = _digest(state.answer)
    if current_sha not in (index["original_answer_sha256"], _digest(result["answer"])):
        return _public(error_code="rewrite_context_answer_changed")
    entry = state.source_attribution_reviews[operation_key]
    changed = result["status"] == "ready" and state.answer != result["answer"]
    needs_save = not entry["usage_applied"] or (result["status"] == "ready" and not entry["answer_applied"])
    try:
        if not entry["usage_applied"]:
            if on_usage is not None:
                on_usage(deepcopy(entry["usage"]))
            else:
                for key in state.usage:
                    state.usage[key] += entry["usage"].get(key, 0)
            entry["usage_applied"] = True
        if result["status"] == "ready":
            state.answer = result["answer"]
            state.provider_context = deepcopy(result["provider_context"])
            entry["answer_applied"] = True
        if needs_save:
            save()
    except BaseException:
        restore()
        raise
    before_review()
    return _public("applied" if changed else "already_applied" if result["status"] == "ready" else "unverified",
        changed=changed, error_code=result["error_code"])
