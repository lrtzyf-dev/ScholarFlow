"""Checkpoint the intent of a paid answer review before dispatching it."""
from copy import deepcopy


_MISSING = object()


def _unsubmitted_boundary(exc, llm, previous_failure, previous_started):
    """Only unchanged production-client attempt receipts can prove no HTTP.

    An unchanged ``failed_call`` alone is insufficient: a successful request
    clears it before a later usage callback can reject a resource boundary.
    The meter keeps its last started attempt even after a successful response.
    Both same-thread receipts must remain unchanged, including across multiple
    calls inside a review. No subclass/custom client can supply this proof.
    """
    from ..llm import APIClient
    from ..request_resources import raise_if_resource_boundary
    if type(llm) is not APIClient:
        return False
    try:
        raise_if_resource_boundary(exc)
    except Exception as boundary:
        if boundary is not exc:
            return False
    else:
        return False
    context = getattr(llm, "_call_context", None)
    return (context is not None
            and getattr(context, "failed_call", _MISSING) is previous_failure
            and getattr(context, "last_started_call_id", _MISSING) is previous_started)


def cached_review(journal, key, *, review, persist, before_review, llm=None):
    """An uncertain charged dispatch is never silently purchased again.

    The model-call ledger remains authoritative for paid usage when saving a
    result fails. Results are committed before observing post-call cancellation.
    """
    if key in journal:
        result = journal[key]
        if isinstance(result, dict) and result.get("review_dispatch") == "pending":
            result = {"mode": "source_attribution", "status": "unverified", "passed": False,
                "verified": False, "unsupported": [], "claims": [], "unverified_count": 1,
                "reason": "review_dispatch_outcome_unknown", "error_code": "dispatch_outcome_unknown",
                "quality_status": "unverified", "quality_issues": []}
            journal[key] = result
            persist()
        return deepcopy(result)
    before_review()
    journal[key] = {"review_dispatch": "pending"}
    persist()  # No request can start if this checkpoint is not durable.
    previous_failure = getattr(getattr(llm, "_call_context", None), "failed_call", _MISSING)
    previous_started = getattr(getattr(llm, "_call_context", None), "last_started_call_id", _MISSING)
    try:
        result = review()
    except BaseException as exc:
        if _unsubmitted_boundary(exc, llm, previous_failure, previous_started):
            # No provider dispatch occurred. Persist the absence so an
            # explicitly resumed task can retry admission after its boundary
            # is resolved. Every possibly charged outcome stays pending.
            pending = journal.pop(key, None)
            try:
                persist()
            except BaseException:
                journal[key] = pending  # Match the last durable checkpoint.
                raise
        raise
    journal[key] = deepcopy(result)
    persist()
    before_review()  # The successful receipt survives cancellation here.
    return result
