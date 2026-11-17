"""Stable user-revision fences alongside unchanged exact legacy result keys."""
from copy import deepcopy
import re


FIELD = "_paid_review_family"
PROTOCOL = "paid_review_user_revision_v1"


def family_identity(stage, *, question=None, messages=None, input_ids=(), excluded_indices=()):
    """Missing canonical input means conservative stage scope, never a guess."""
    from .source_attribution import quality_repair_context_key
    revision = (quality_repair_context_key(question, messages or [], input_ids=input_ids,
        excluded_indices=excluded_indices) if isinstance(question, str) and question.strip() else None)
    return {"protocol": PROTOCOL, "stage": stage, "user_revision": revision}


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def family_matches(record, current):
    """None means invalid; legacy/unscoped unknowns conservatively match all."""
    if FIELD not in record:
        return True
    value = record[FIELD]
    if (not isinstance(value, dict) or set(value) != {"protocol", "stage", "user_revision"}
            or value["protocol"] != PROTOCOL or value["stage"] != current["stage"]
            or (value["user_revision"] is not None and not _hex(value["user_revision"]))):
        return None
    return (value["user_revision"] is None or current["user_revision"] is None
        or value["user_revision"] == current["user_revision"])


def scope_barrier(journal, family):
    blocked = []
    for key, value in journal.items():
        if not isinstance(key, str) or not key.startswith("citation_scope/"):
            continue
        if not _hex(key[len("citation_scope/"):]) or not isinstance(value, dict):
            return "journal_invalid", []
        matches = family_matches(value, family)
        if matches is None:
            return "journal_invalid", []
        if matches and (value.get("review_dispatch") == "pending"
                or value.get("error_code") == "dispatch_outcome_unknown"):
            blocked.append(key)
    return ("dispatch_outcome_unknown", blocked) if blocked else (None, [])


def batch_barrier(journal, family, *, protocol, digest):
    blocked, prior = [], []
    for key, record in journal.items():
        # Scope reviews have a separate stage. A combined checkpoint may carry
        # their legacy keys without granting them semantic-review authority.
        if isinstance(key, str) and key.startswith("citation_scope/"):
            if not _hex(key[len("citation_scope/"):]) or not isinstance(record, dict):
                return "journal_invalid", [], []
            continue
        if not _hex(key) or not isinstance(record, dict) or record.get("protocol") != protocol:
            return "journal_invalid", [], []
        matches = family_matches(record, family)
        if matches is None or not isinstance(record.get("batches"), dict):
            return "journal_invalid", [], []
        if not matches:
            continue
        for label, entry in record["batches"].items():
            if not isinstance(entry, dict) or entry.get("state") not in {"pending", "complete"}:
                return "journal_invalid", [], []
            if entry["state"] == "pending":
                blocked.append({"key": key, "batch": label})
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or entry.get("result_sha256") != digest(result):
                return "journal_invalid", [], []
            if result.get("error_code") == "dispatch_outcome_unknown":
                blocked.append({"key": key, "batch": label})
            if (type(result.get("known_failed_count")) is int and result["known_failed_count"] > 0
                    and isinstance(result.get("unsupported"), list)):
                prior.append({"key": key, "batch": label, "claims": deepcopy(result.get("claims", [])),
                    "known_failed_count": result["known_failed_count"],
                    "unsupported": deepcopy(result["unsupported"])})
    return ("dispatch_outcome_unknown" if blocked else None), blocked, prior


def durable_save(journal, persist, *, stamp=None):
    """Own this journal only; failures never masquerade as provider failures."""
    durable = deepcopy(journal)

    def save():
        nonlocal durable
        try:
            if stamp is not None:
                stamp()
            if persist is not None:
                persist()
        except BaseException:
            journal.clear()
            journal.update(deepcopy(durable))
            raise
        durable = deepcopy(journal)
    return save


def public_result(value):
    result = deepcopy(value)
    if isinstance(result, dict):
        result.pop(FIELD, None)
    return result
