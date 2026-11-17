"""Remove exact rejected lines or complete sentences; never certify the rest.

This deterministic fallback follows the single semantic rewrite. It does not
purchase another rewrite, edit a clause, or treat an unknown review as false.
The caller must rerun the full citation and goal/context checks after adoption.
The legacy line and separate sentence APIs share one durable cleanup slot.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import re

from ..rag.citation import (MARKER_RE, LIST_ITEM_RE, SENT_RE, parse_claims,
                           prose_for_citations, split_sentences, citation_gate)


PROTOCOL = "exact_rejected_line_cleanup_v1"
SENTENCE_PROTOCOL = "exact_rejected_sentence_cleanup_v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _claims(answer, ids):
    return [{"text": c["claim"], "evidence_ids": c["evidence_ids"]}
            for c in parse_claims(answer, ids)]


def _counter(claims):
    return Counter((c["text"], tuple(c["evidence_ids"])) for c in claims)


def _cleanup_context(answer, evidence, audit):
    """Bind deletion candidates to the complete current claim/eN verdict."""
    # Legacy public line/sentence fixtures keep their original claim/eN-only
    # contract. A new audit binding must never be ignored or reinstalled after
    # changing its complete answer, evidence or verdict. The automatic executor
    # obtains the new binding from the actual auditor on every full llm review.
    from .citation_context_preservation import AUDIT_BINDING_FIELD, audit_preservation_matches
    if (isinstance(audit, dict) and AUDIT_BINDING_FIELD in audit
            and not audit_preservation_matches(answer, evidence, audit)):
        return None
    if (not isinstance(answer, str) or not isinstance(evidence, list)
            or not isinstance(audit, dict) or audit.get("mode") != "llm"
            or audit.get("status") != "failed" or audit.get("verified") is not True
            or audit.get("passed") is not False
            or type(audit.get("known_failed_count")) is not int
            or audit["known_failed_count"] <= 0 or type(audit.get("unknown_count")) is not int
            or audit["unknown_count"] != 0 or type(audit.get("unverified_count")) is not int
            or audit.get("unverified") != [] or audit["unverified_count"] != 0
            or audit.get("error_code")):
        return None
    # Do not implement a second Markdown/HTML parser here. Raw HTML blocks
    # can span blank lines, and indentation can keep children/code inside a
    # list whose parent would otherwise look standalone. This narrow fallback
    # declines the whole answer whenever either grammar is present.
    if (re.search(r"<(?:/?[A-Za-z]|!|\?)", answer)
            or any(line and line[:1].isspace() and line.strip() for line in answer.splitlines())):
        return None
    ids = [e.get("id") for e in evidence if isinstance(e, dict)]
    if (len(ids) != len(evidence) or any(not isinstance(i, str) or not re.fullmatch(r"e\d+", i) for i in ids)
            or len(set(ids)) != len(ids)):
        return None
    claims = _claims(answer, ids)
    if (not claims or audit.get("claims") != claims
            or type(audit.get("factual_claims")) is not int or audit["factual_claims"] != len(claims)
            or citation_gate(answer, ids)["invalid_markers"]):
        return None
    rejected = audit.get("unsupported")
    if (not isinstance(rejected, list) or len(rejected) != audit["known_failed_count"]
            or any(not isinstance(c, dict) or not isinstance(c.get("text"), str)
                or not isinstance(c.get("reason"), str) or not c["reason"].strip() for c in rejected)):
        return None
    failed = Counter(c["text"] for c in rejected)
    text_counts = Counter(c["text"] for c in claims)
    if any(count != 1 or text_counts[text] != 1 for text, count in failed.items()):
        return None
    return ids, claims, failed


def rejected_line_candidate(answer, evidence, audit):
    """Return a v1 deletion only for wholly rejected standalone lines.

    This legacy entry point deliberately retains its narrower behavior. Use
    the separate sentence entry point for exact complete sentences in prose.
    """
    context = _cleanup_context(answer, evidence, audit)
    if context is None:
        return None
    ids, claims, failed = context
    lines = answer.splitlines(keepends=True)
    removals, removed_claims = [], []
    offset = 0
    for i, raw in enumerate(lines):
        line = raw.strip()
        before = lines[i - 1].strip() if i else ""
        after = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # Do not infer Markdown block boundaries inside fences or quotes. Only
        # an isolated paragraph or a flat, explicitly cited list item is safe.
        flat_list = bool(re.match(r"^(?:[-*+]|\d+[.)])\s+", raw))
        isolated = not before and not after
        unsafe = (not line or raw[:1].isspace() or line.startswith(("#", "|", ">", "[", "`", "~"))
                  or "```" in answer or "~~~" in answer
                  or (flat_list and after and not re.match(r"^(?:[-*+]|\d+[.)])\s+", lines[i + 1]))
                  or (not flat_list and not isolated))
        if not unsafe:
            own = _claims(raw, ids)
            prose = prose_for_citations(LIST_ITEM_RE.sub("", line, count=1), preserve_inline=True)
            sentences = [MARKER_RE.sub("", s).strip() for s in split_sentences(prose)]
            if (own and sentences == [c["text"] for c in own]
                    and all(failed[c["text"]] == 1 for c in own)
                    and _counter(own) <= _counter(claims)):
                removals.append({"start": offset, "end": offset + len(raw), "text": raw})
                removed_claims.extend(own)
        offset += len(raw)
    if not removals or Counter(c["text"] for c in removed_claims) != failed:
        return None
    candidate = answer
    for edit in reversed(removals):
        candidate = candidate[:edit["start"]] + candidate[edit["end"]:]
    remaining = _claims(candidate, ids)
    if (not candidate.strip() or not remaining
            or _counter(remaining) != _counter(claims) - _counter(removed_claims)):
        return None
    old_gate, new_gate = citation_gate(answer, ids), citation_gate(candidate, ids)
    # No hidden loss or inheritance of previously uncited content is allowed.
    if (new_gate["invalid_markers"] or new_gate["unsupported"] != old_gate["unsupported"]
            or old_gate["total_claims"] - new_gate["total_claims"] != len(removed_claims)):
        return None
    return {"answer": candidate, "removals": removals,
            "removed_claims": removed_claims, "semantic_verification": "not_performed"}


def _plain_boundaries(raw):
    """Return top-level offsets for a tiny, deliberately bounded prose grammar.

    The citation parser remains authoritative. This scanner only refuses cuts
    inside paired punctuation or strong emphasis; it is not a Markdown parser.
    """
    without_markers = MARKER_RE.sub("", raw)
    if (len(raw) > 20_000 or any(token in without_markers for token in ("`", "~", "\\", "[", "]", "|", "<", ">"))
            or re.search(r"[*_]", re.sub(r"\*\*|__", "", without_markers))):
        return None
    strong = list(re.finditer(r"\*\*|__", raw))
    if len(strong) % 2:
        return None
    pairs = []
    for opening, closing in zip(strong[::2], strong[1::2]):
        if opening[0] != closing[0]:
            return None
        inner = MARKER_RE.sub("", raw[opening.end():closing.start()]).strip()
        # Emphasis may enclose a heading or one complete sentence, never
        # connect two sentences whose retained formatting would be changed.
        punctuation = list(re.finditer(r"[。！？!?]", inner))
        if punctuation and (len(punctuation) != 1 or punctuation[0].end() != len(inner)):
            return None
        pairs.append((opening.start(), opening.end(), closing.start(), closing.end()))
    openings = {"(": ")", "（": "）", "【": "】", "{": "}", "「": "」", "『": "』", "“": "”", "‘": "’"}
    closings = set(openings.values())
    stack, boundaries, cursor = [], {0}, 0
    while cursor < len(raw):
        marker = MARKER_RE.match(raw, cursor)
        if marker:
            cursor = marker.end()
        else:
            char = raw[cursor]
            if char == '"':
                if stack and stack[-1] == '"':
                    stack.pop()
                else:
                    stack.append('"')
            elif char in openings:
                stack.append(openings[char])
            elif char in closings:
                if not stack or stack.pop() != char:
                    return None
            cursor += 1
        if not stack:
            boundaries.add(cursor)
    return (boundaries, pairs) if not stack else None


def rejected_sentence_candidate(answer, evidence, audit):
    """Delete only unique complete false sentences from isolated plain prose.

    Unknown, historical or partial verdicts never authorize edits. The raw span
    includes only this sentence's explicit citations and balanced emphasis;
    every surviving claim, citation inheritance and uncited candidate is kept.
    The returned transformation is not a new scientific verdict.
    """
    if not isinstance(answer, str) or len(answer) > 200_000:
        return None
    context = _cleanup_context(answer, evidence, audit)
    if context is None:
        return None
    ids, claims, failed = context
    if len(failed) > 16 or "```" in answer or "~~~" in answer:
        return None
    lines = answer.splitlines(keepends=True)
    removals, removed_claims, offset = [], [], 0
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        before = lines[i - 1].strip() if i else ""
        after = lines[i + 1].strip() if i + 1 < len(lines) else ""
        eligible = (line.strip() and not before and not after and not LIST_ITEM_RE.match(line)
                    and not line.startswith(("#", "|", ">", "[", "`", "~")))
        checked = _plain_boundaries(line) if eligible else None
        if checked is not None and prose_for_citations(line, preserve_inline=True) == line:
            boundaries, pairs = checked
            consumed = 0
            for match in SENT_RE.finditer(line):
                start, end = max(consumed, match.start()), match.end()
                while start < end and line[start].isspace():
                    start += 1
                while end > start and line[end - 1].isspace():
                    end -= 1
                # A strong close immediately after terminal punctuation can
                # belong to the complete rejected sentence; keep it balanced.
                for left, _opened, close, right in pairs:
                    if start <= left < end and end == close:
                        end = right
                if start >= end or start not in boundaries or end not in boundaries:
                    continue
                if any(left < start < right or left < end < right for left, _opened, _close, right in pairs):
                    continue
                piece = line[start:end]
                terminal = re.sub(r"\*\*|__", "", MARKER_RE.sub("", piece)).rstrip()
                own = _claims(piece, ids)
                if (not terminal.endswith(("。", "！", "？", "!", "?")) or len(own) != 1
                        or not MARKER_RE.search(piece) or failed[own[0]["text"]] != 1
                        or not _counter(own) <= _counter(claims)):
                    continue
                removals.append({"start": offset + start, "end": offset + end, "text": piece})
                removed_claims.extend(own)
                consumed = end
        offset += len(raw)
    if not removals or Counter(c["text"] for c in removed_claims) != failed:
        return None
    candidate = answer
    for edit in reversed(removals):
        candidate = candidate[:edit["start"]] + candidate[edit["end"]:]
    remaining = _claims(candidate, ids)
    expected = [claim for claim in claims if claim["text"] not in failed]
    if (not candidate.strip() or not remaining or remaining != expected
            or _counter(remaining) != _counter(claims) - _counter(removed_claims)):
        return None
    old_gate, new_gate = citation_gate(answer, ids), citation_gate(candidate, ids)
    if (new_gate["invalid_markers"] or new_gate["unsupported"] != old_gate["unsupported"]
            or old_gate["total_claims"] - new_gate["total_claims"] != len(removed_claims)):
        return None
    return {"answer": candidate, "removals": removals, "removed_claims": removed_claims,
            "semantic_verification": "not_performed", "cleanup_kind": "sentence"}


def apply_rejected_line_cleanup(state, audit, *, input_key, persist, before_review):
    """One nonbillable cleanup per user revision; checkpoint before publishing.

    This is not a signed receipt or a new semantic verdict. Recovered and new
    candidates always face the caller's current full checks. Persistence failure
    restores the complete prior in-memory state, matching the last checkpoint.
    """
    before_review()
    if not isinstance(input_key, str) or not re.fullmatch(r"[a-f0-9]{64}", input_key):
        return {"changed": False, "reason": "invalid_input_identity"}
    key = "citation_local_cleanup/" + input_key
    if key in state.source_attribution_reviews:
        return {"changed": False, "reason": "cleanup_already_claimed"}
    candidate = rejected_line_candidate(state.answer, state.evidence, audit)
    if candidate is None:
        return {"changed": False, "reason": "no_safe_exact_deletion"}
    durable = deepcopy(state.snapshot())
    state.source_attribution_reviews[key] = {
        "protocol": PROTOCOL, "original_answer_sha256": _digest(state.answer),
        "answer_sha256": _digest(candidate["answer"]), "evidence_sha256": _digest(state.evidence),
        "audit_sha256": _digest(audit), "removals": candidate["removals"],
        "semantic_verification": "not_performed"}
    state.answer, state.provider_context = candidate["answer"], {}
    try:
        persist()
    except BaseException:
        for name, value in durable.items():
            setattr(state, name, deepcopy(value))
        raise
    before_review()
    return {"changed": True, "removed_line_count": len(candidate["removals"]),
            "semantic_verification": "not_performed"}


def apply_rejected_sentence_cleanup(state, audit, *, input_key, persist, before_review):
    """Adopt at most one cleanup using the same durable v1 per-revision slot.

    Old line receipts, malformed occupied slots and new sentence receipts all
    block another edit. Adoption has no model calls and certifies no conclusion;
    callers must rerun the original citation, semantic and task quality checks.
    """
    before_review()
    if not isinstance(input_key, str) or not re.fullmatch(r"[a-f0-9]{64}", input_key):
        return {"changed": False, "reason": "invalid_input_identity"}
    key = "citation_local_cleanup/" + input_key
    if key in state.source_attribution_reviews:
        return {"changed": False, "reason": "cleanup_already_claimed"}
    candidate = rejected_sentence_candidate(state.answer, state.evidence, audit)
    if candidate is None:
        return {"changed": False, "reason": "no_safe_exact_deletion"}
    durable = deepcopy(state.snapshot())
    state.source_attribution_reviews[key] = {
        "protocol": SENTENCE_PROTOCOL, "original_answer_sha256": _digest(state.answer),
        "answer_sha256": _digest(candidate["answer"]), "evidence_sha256": _digest(state.evidence),
        "audit_sha256": _digest(audit), "removals": candidate["removals"],
        "semantic_verification": "not_performed"}
    state.answer, state.provider_context = candidate["answer"], {}
    try:
        persist()
    except BaseException:
        for name, value in durable.items():
            setattr(state, name, deepcopy(value))
        raise
    before_review()
    return {"changed": True, "removed_sentence_count": len(candidate["removals"]),
            "cleanup_kind": "sentence", "semantic_verification": "not_performed"}
