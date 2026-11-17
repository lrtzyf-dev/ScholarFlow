"""Policy-versioned review results with a stable unknown-dispatch fence."""
from copy import deepcopy
import re

from .answer_review_journal import cached_review


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _unknown(value):
    return isinstance(value, dict) and (value.get("review_dispatch") == "pending"
        or value.get("error_code") == "dispatch_outcome_unknown")


def _invalid():
    return {"mode": "source_attribution", "status": "unverified", "passed": False,
        "verified": False, "unsupported": [], "claims": [], "unverified_count": 1,
        "error_code": "journal_invalid", "reason": "source_review_contract_invalid",
        "quality_status": "unverified", "quality_verified": False, "quality_issues": []}


def cached_source_review(journal, legacy_key, *, contract_id, review, persist,
                         before_review, llm=None, allow_dispatch=True, dispatch_family=None):
    """Recheck known old verdicts once; never rebuy an unknown family member.

    ``legacy_key`` is the exact historical protocol-6 context hash, independent
    of policy/schema upgrades. Old direct records remain readable. Each actual
    contract has its own result key; any direct or family pending/unknown intent
    fences the whole family before a new result may be purchased. The shared
    journal preserves intent before dispatch and proves resource denials.
    ``allow_dispatch=False`` still reuses current/unknown receipts but never
    buys or records a missing policy result; explicit resumption may re-admit.
    The initial executor supplies ``dispatch_family`` to fence the same user
    revision across exact answer/model keys. Standalone legacy callers retain
    their historical exact-family contract unless they supply that identity.
    """
    if (not isinstance(journal, dict) or not _hex(legacy_key) or not _hex(contract_id)
            or not callable(review) or not callable(persist) or not callable(before_review)):
        return _invalid()
    before_review()

    from .paid_review_family import FIELD, family_matches, public_result
    if dispatch_family is not None and (not isinstance(dispatch_family, dict)
            or dispatch_family.get("stage") != "source_attribution"
            or family_matches({FIELD: dispatch_family}, dispatch_family) is not True):
        return _invalid()

    def finish(value):
        before_review()
        return public_result(value)

    if type(allow_dispatch) is not bool:
        return finish(_invalid())
    durable = deepcopy(journal)
    prefix = "source_review/" + legacy_key + "/"
    current = prefix + contract_id
    stamp_current = False

    def save():
        nonlocal durable
        try:
            # cached_review replaces a pending intent with a public unknown
            # dictionary. Keep only identity already present in that durable
            # intent; a legacy unscoped record must remain unscoped.
            for key, previous in durable.items():
                if (isinstance(previous, dict) and FIELD in previous
                        and isinstance(journal.get(key), dict) and FIELD not in journal[key]):
                    journal[key][FIELD] = deepcopy(previous[FIELD])
            if stamp_current and dispatch_family is not None and isinstance(journal.get(current), dict):
                journal[current][FIELD] = deepcopy(dispatch_family)
            persist()
        except BaseException:
            # Only this journal is owned here. The caller owns other state and
            # the authoritative call ledger; no possibly paid request is reset.
            journal.clear()
            journal.update(deepcopy(durable))
            raise
        durable = deepcopy(journal)
    # Exact result identity still includes answer/model/observations. Payment
    # admission belongs to the user revision, so changing those exact fields
    # cannot conceal an earlier intent. Unscoped legacy intents match
    # conservatively; never retrofit a new identity onto an unknown old proof.
    if dispatch_family is not None:
        for key, value in journal.items():
            if not isinstance(key, str) or not (key.startswith("source_review/") or _hex(key)):
                continue
            invalid_key = key.startswith("source_review/") and (
                len(key.split("/")) != 3 or not all(_hex(part) for part in key.split("/")[1:]))
            if not isinstance(value, dict) or invalid_key:
                return finish(_invalid())
            prior_family = value.get(FIELD)
            if isinstance(prior_family, dict) and prior_family.get("stage") != dispatch_family["stage"]:
                return finish(_invalid())
            matches = family_matches(value, dispatch_family)
            if matches is None:
                return finish(_invalid())
            if matches and _unknown(value):
                return finish(cached_review(journal, key, review=review, persist=save,
                    before_review=before_review, llm=llm))
    family = ([legacy_key] if legacy_key in journal else []) + sorted(
        key for key in journal if isinstance(key, str) and key.startswith(prefix))
    # Scan every policy before looking up the current known result. An upgrade
    # cannot hide a charged intent behind a different policy digest.
    for key in family:
        value = journal[key]
        if key != legacy_key and not _hex(key[len(prefix):]):
            return finish(_invalid())
        if not isinstance(value, dict):
            return finish(_invalid())
        if _unknown(value):
            return finish(cached_review(journal, key, review=review, persist=save,
                before_review=before_review, llm=llm))
    if current not in journal and not allow_dispatch:
        result = _invalid()
        result.pop("error_code")
        result.update(reason="run_budget_exhausted", quality_reason="run_budget_exhausted")
        return finish(result)
    stamp_current = True
    result = cached_review(journal, current, review=review, persist=save,
        before_review=before_review, llm=llm)
    return finish(result if isinstance(result, dict) else _invalid())
