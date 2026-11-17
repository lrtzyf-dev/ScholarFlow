"""Bounded semantic reviews with checkpoint-owned, charge-safe batch receipts.

Only public verdicts/usage/fingerprints enter the journal. A pending dispatch is
never silently replayed, even when a process dies after the provider charged it.
"""
from copy import deepcopy
import hashlib
import json

from .model_review import model_review_identity

MAX_CLAIMS = 12
# v1 checkpoints used four claims per paid call. Exact old keys remain
# resumable with their original grouping; an unknown/pending old dispatch is
# still seen by paid_review_family.batch_barrier before any newer key runs.
LEGACY_BATCH_SIZES = (4,)
PROTOCOL = "citation_semantic_batches_v1"
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def usage_counts(value):
    return {key: value[key] for key in TOKEN_FIELDS
            if isinstance(value, dict) and type(value.get(key)) is int and value[key] >= 0}


def _add(total, value):
    for key, count in usage_counts(value).items():
        total[key] = total.get(key, 0) + count


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _model_identity(llm):
    return model_review_identity(llm)


def _failed_call_id(llm):
    previous = getattr(getattr(llm, "_call_context", None), "failed_call", None)
    return previous[1] if isinstance(previous, tuple) and len(previous) == 2 else None


def _failure_usage(llm, prior):
    identity = _failed_call_id(llm)
    if not identity or identity == prior:
        return {}, "unknown"
    from ..call_ledger import get_call
    row = get_call(identity)
    if row.get("usage_source") == "actual":
        return usage_counts(row.get("actual_usage")), "actual"
    if row.get("usage_source") == "estimated":
        return usage_counts(row.get("estimated_usage")), "estimated"
    return {}, "unknown"


def _unknown(group, code, *, usage=None, usage_source="unknown"):
    return {"passed": False, "status": "unverified", "unsupported": [],
        "unverified": [{"text": claim["text"], "reason": "该批审计未取得完整可核验结果，需人工核对"} for claim in group],
        "mode": "unavailable", "verified": False, "claims": deepcopy(group), "factual_claims": len(group),
        "unsupported_ratio": 0.0, "unverified_count": len(group), "known_failed_count": 0,
        "unknown_count": len(group), "error_code": code, "usage": usage_counts(usage), "usage_source": usage_source}


def _store_result(entry, result):
    entry.update(state="complete", result=deepcopy(result), result_sha256=_digest(result))


def _without_audit_context(evidence_map):
    """Reconstruct the pre-context key without trusting or mutating input."""
    return {key: {field: deepcopy(value) for field, value in item.items()
                  if field != "audit_quote"}
            for key, item in evidence_map.items() if isinstance(item, dict)}


def run_batched_audit(*, answer, claims, factual_cited, evidence_map, metadata, receipts,
                      llm, journal, persist, before_batch, on_usage, audit_one,
                      question=None, input_ids=(), messages=None, excluded_indices=(), on_progress=None):
    # Keep complete-answer meaning and each claim's own eN. All source and
    # receipt text remains untrusted prompt data, never cache permissions.
    from .audit import _build_audit_text
    prompt_contract = _build_audit_text([], {}, metadata=metadata, receipts=[], answer="")
    def audit_key(batch_size, source_evidence=None):
        source_evidence = evidence_map if source_evidence is None else source_evidence
        return _digest({"protocol": PROTOCOL, "batch_size": batch_size, "answer": answer,
            "claims": factual_cited, "evidence": source_evidence, "metadata": metadata, "receipts": receipts,
            "model": _model_identity(llm), "prompt_contract": prompt_contract})

    batch_size = MAX_CLAIMS
    key = audit_key(batch_size)
    owned = journal if journal is not None else {}
    if not isinstance(owned, dict):
        raise ValueError("审计检查点格式无效")
    # A matching v1/four-claim journal belongs to the same exact audit input.
    # Continue that record in its original shape; do not silently rebatch its
    # paid or pending operations under a new key.
    legacy_context_changed = False
    for legacy_size in LEGACY_BATCH_SIZES:
        legacy_key = audit_key(legacy_size)
        old_context_key = audit_key(legacy_size, _without_audit_context(evidence_map))
        if legacy_key in owned:
            key, batch_size = legacy_key, legacy_size
            break
        if old_context_key in owned:
            # A pre-context checkpoint may safely reuse complete verdicts, but
            # a missing batch must not be dispatched against a changed prompt
            # after a crash.  Mark those batches unknown and require a fresh
            # review with the current exact input instead.
            key, batch_size, legacy_context_changed = old_context_key, legacy_size, True
            break

    groups = [factual_cited[start:start + batch_size] for start in range(0, len(factual_cited), batch_size)]
    def progress(*, status="running", completed=0, batch=None):
        if on_progress is not None:
            on_progress({"phase": "semantic_audit", "status": status,
                "completed": max(0, min(int(completed), len(factual_cited))),
                "total": len(factual_cited), "batch": batch, "total_batches": len(groups)})
    from .paid_review_family import FIELD, family_identity, batch_barrier, durable_save
    family = family_identity("citation_semantic", question=question, messages=messages,
        input_ids=input_ids, excluded_indices=excluded_indices)
    if before_batch is not None:
        before_batch()
    barrier_code, blocked, prior = batch_barrier(owned, family, protocol=PROTOCOL, digest=_digest)
    def stamp_family():
        if not barrier_code and key in owned:
            owned[key][FIELD] = deepcopy(family)
    # Capture the last durable journal before allocating a new exact record.
    save = durable_save(owned, persist, stamp=stamp_family)
    record = owned.get(key)
    if record is None:
        record = {"protocol": PROTOCOL, "batch_size": batch_size,
            "claim_count": len(factual_cited), "batches": {}}
        if not barrier_code:
            owned[key] = record
    if (not isinstance(record, dict) or record.get("protocol") != PROTOCOL
            or record.get("batch_size") != batch_size or record.get("claim_count") != len(factual_cited)
            or not isinstance(record.get("batches"), dict)):
        raise ValueError("审计检查点与当前语义输入不一致")
    records = record["batches"]
    results, summaries, new_usage, cumulative_usage = [], [], {}, {}

    progress(status="running", completed=0)

    for number, group in enumerate(groups):
        if before_batch is not None:
            before_batch()
        # A just-completed batch can itself report an uncertain dispatch.
        # Observe that new fence before buying a later batch in this run.
        barrier_code, blocked, prior = batch_barrier(owned, family, protocol=PROTOCOL, digest=_digest)
        label, start = str(number), number * batch_size
        progress(status="running", completed=start, batch=number + 1)
        entry = records.get(label)
        cached = entry is not None
        if cached:
            if (not isinstance(entry, dict) or entry.get("start") != start
                    or entry.get("count") != len(group) or entry.get("state") not in ("pending", "complete")):
                result = _unknown(group, "journal_invalid")
            elif entry["state"] == "pending":
                # No reliable result was committed. It might have been charged;
                # do not turn an uncertain dispatch into an automatic retry.
                result = _unknown(group, "dispatch_outcome_unknown")
                _store_result(entry, result)
                save()
            elif entry.get("result_sha256") != _digest(entry.get("result")):
                result = _unknown(group, "journal_invalid")
            else:
                result = deepcopy(entry["result"])
                if result.get("claims") != group or result.get("factual_claims") != len(group):
                    result = _unknown(group, "journal_invalid")
            if isinstance(entry, dict) and on_usage is not None and entry.get("usage_accounted") is False:
                # At most once within this interpreter even if a callback
                # mutates its counters before raising. The durable call ledger
                # remains authoritative when the checkpoint callback fails.
                entry["usage_accounted"] = True
                on_usage(usage_counts(result.get("usage")))
                save()
        elif barrier_code:
            # An unresolved earlier dispatch fences every missing batch and
            # changed exact key, while current known verdicts remain visible.
            result = _unknown(group, barrier_code)
        elif legacy_context_changed:
            result = _unknown(group, "audit_input_changed")
        else:
            entry = {"state": "pending", "start": start, "count": len(group), "usage_accounted": False}
            records[label] = entry
            save()  # This must succeed before a potentially charged request.
            prior_failed_call = _failed_call_id(llm)
            from .answer_review_journal import _MISSING, _unsubmitted_boundary
            call_context = getattr(llm, "_call_context", None)
            previous_failure = getattr(call_context, "failed_call", _MISSING)
            previous_started = getattr(call_context, "last_started_call_id", _MISSING)
            try:
                result = audit_one(group, start)
            except BaseException as exc:
                # Resource/cancel failures propagate without being demoted to
                # "provider unavailable". A started request stays pending.
                # If admission rejected before any new call existed, it is
                # safe to leave this batch missing for an authorized resume.
                if _unsubmitted_boundary(exc, llm, previous_failure, previous_started):
                    pending = records.pop(label, None)
                    try:
                        save()
                    except BaseException:
                        records[label] = pending  # Match the durable checkpoint.
                        raise
                raise
            result = deepcopy(result)
            reported = usage_counts(result.get("usage"))
            from ..llm import APIClient
            source = (("estimated" if llm._synthetic else "actual") if isinstance(llm, APIClient)
                      else "reported") if reported else "unknown"
            if not reported:
                reported, source = _failure_usage(llm, prior_failed_call)
            result["usage"], result["usage_source"] = reported, source
            _add(new_usage, reported)
            _store_result(entry, result)
            if on_usage is not None:
                entry["usage_accounted"] = True
                on_usage(reported)
            save()  # Public verdict + usage update are one checkpoint write.
        _add(cumulative_usage, result.get("usage"))
        results.append(result)
        progress(status="running", completed=min(start + len(group), len(factual_cited)), batch=number + 1)
        summaries.append({"batch": number, "claim_start": start, "claim_count": len(group),
            "cached": cached, "status": result.get("status"), "error_code": result.get("error_code"),
            "known_failed_count": result.get("known_failed_count", 0), "unknown_count": result.get("unknown_count", len(group)),
            "usage": usage_counts(result.get("usage")), "usage_source": result.get("usage_source", "unknown")})
        if before_batch is not None:
            before_batch()

    # The final batch may have introduced the family fence too.
    barrier_code, blocked, prior = batch_barrier(owned, family, protocol=PROTOCOL, digest=_digest)

    unsupported = [item for result in results for item in result.get("unsupported", [])]
    unverified = [item for result in results for item in result.get("unverified", [])]
    known_failed = sum(result.get("known_failed_count", 0) for result in results)
    unknown = sum(result.get("unknown_count", 0) for result in results)
    status = "failed" if known_failed else "unverified" if unverified or unknown else "passed"
    modes = {result.get("mode") for result in results}
    value = {"passed": status == "passed", "status": status, "unsupported": unsupported, "unverified": unverified,
        "mode": next(iter(modes)) if len(modes) == 1 else "llm", "verified": not unknown and not unverified,
        "claims": claims, "factual_claims": len(factual_cited), "unsupported_ratio": known_failed / len(factual_cited),
        "unverified_count": len(unverified), "known_failed_count": known_failed, "unknown_count": unknown,
        "usage": new_usage, "new_usage": new_usage, "cumulative_usage": cumulative_usage,
        "batch_count": len(groups), "batch_protocol": PROTOCOL, "batches": summaries,
        "unknown_usage_batches": sum(not summary["usage"] for summary in summaries)}
    codes = [result["error_code"] for result in results if result.get("error_code")]
    if codes:
        value["error_code"] = codes[0] if len(set(codes)) == 1 else "multiple_batch_failures"
    if any(result.get("error") for result in results):
        value["error"] = "语义审计未完整返回可核验结果，需人工核对"
    if barrier_code:
        # A cached current success must not hide a different unknown operation
        # from this user revision. Old false claims are retained as prior-scope
        # evidence, never falsely presented as a verdict on a changed answer.
        value.update(passed=False, status="failed" if known_failed else "unverified",
            verified=False, error_code=barrier_code,
            family_unknown_count=len(blocked),
            prior_known_failures=[item for item in prior if item["key"] != key])
    # The caller may still need to bind this result to the exact answer and
    # evidence identity before it is a complete audit outcome.
    progress(status=value["status"], completed=len(factual_cited), batch=len(groups))
    return value
