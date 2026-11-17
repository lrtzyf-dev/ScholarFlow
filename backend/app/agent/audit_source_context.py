"""Bounded, identity-checked source context for semantic citation review.

Retriever evidence intentionally keeps a short display quote.  That is useful
for the UI, but a sentence can fall across the quote boundary and become
``uncertain`` even though the same indexed chunk contains the supporting text.
This module gives the reviewer a little more context without changing the
evidence delivered to the user or widening the source scope.

Only a database row whose ``chunk_id`` and ``paper_id`` both match the evidence
is eligible.  A forged/mismatched quote, missing chunk, or cross-paper entry is
left untouched.  New fixed-source evidence already carries a complete locator
quote and is also left untouched here; its scope is checked by
``verified_source_metadata`` at the audit boundary.
"""
from __future__ import annotations

from copy import deepcopy

from ..models import Chunk


MAX_AUDIT_CONTEXT_CHARS = 8_000


def _quote_core(value: object) -> str:
    if not isinstance(value, str):
        return ""
    # Retriever snippets use a single Unicode ellipsis to show a cut boundary.
    # Keep the actual source text strict: only remove those display markers and
    # surrounding whitespace; do not normalize arbitrary user-controlled data.
    return value.strip().strip("…").strip()


def _context(content: str, needle: str, limit: int = MAX_AUDIT_CONTEXT_CHARS) -> str | None:
    if (not isinstance(content, str) or not isinstance(needle, str) or not needle
            or len(needle) > limit):
        return None
    start = content.find(needle)
    if start < 0:
        return None
    if len(content) <= limit:
        return content
    half = max(1, (limit - len(needle)) // 2)
    left = max(0, start - half)
    right = min(len(content), start + len(needle) + half)
    # Rebalance at document edges while retaining the exact supporting span.
    if right - left < limit:
        if left == 0:
            right = min(len(content), limit)
        elif right == len(content):
            left = max(0, len(content) - limit)
    value = content[left:right]
    return ("…" if left else "") + value + ("…" if right < len(content) else "")


def _context_from_display_quote(content: str, quote: str, limit: int) -> str | None:
    """Recover a bounded chunk when the UI snippet contains omitted spans.

    Retriever snippets may contain an ellipsis in the middle as well as at the
    edges.  We require every non-trivial displayed segment to occur in order in
    the same authenticated chunk before using that chunk as audit context.
    """
    direct = _context(content, quote, limit=limit)
    if direct is not None:
        return direct
    core = _quote_core(quote)
    parts = [part.strip() for part in core.split("…") if len(part.strip()) >= 8]
    if len(parts) < 2:
        return None
    cursor, first, last = 0, None, None
    for part in parts:
        found = content.find(part, cursor)
        if found < 0:
            return None
        first = found if first is None else first
        last = found + len(part)
        cursor = last
    span = content[first:last]
    if len(span) <= limit:
        left = max(0, first - (limit - len(span)) // 2)
        right = min(len(content), left + limit)
        return ("…" if left else "") + content[left:right] + ("…" if right < len(content) else "")
    return _context(content, max(parts, key=len), limit=limit)


def enrich_evidence_for_audit(db, evidence: list[dict] | tuple[dict, ...], *, max_chars=MAX_AUDIT_CONTEXT_CHARS):
    """Return an audit-only evidence copy with safe legacy chunk context.

    ``evidence`` remains unchanged.  The returned dictionaries carry
    ``audit_quote`` only when the quote is proven to be a substring of the
    matching indexed chunk.  Source-runtime evidence (locator/revision based)
    is deliberately not expanded through the legacy ``chunks`` table.
    """
    if not isinstance(evidence, (list, tuple)):
        return {}
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_AUDIT_CONTEXT_CHARS:
        max_chars = MAX_AUDIT_CONTEXT_CHARS
    result = {}
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        copied = deepcopy(item)
        # Never trust a similarly named field supplied by a tool/checkpoint.
        # Only the identity checks below may construct reviewer context.
        copied.pop("audit_quote", None)
        # Fixed source locators are authenticated elsewhere and must not be
        # confused with a mutable library chunk.
        if db is None or item.get("locator_id") or item.get("revision_id") or item.get("source_id"):
            result[item["id"]] = copied
            continue
        chunk_id, paper_id = item.get("chunk_id"), item.get("paper_id")
        if not isinstance(chunk_id, str) or not isinstance(paper_id, str) or not paper_id:
            result[item["id"]] = copied
            continue
        try:
            chunk = db.get(Chunk, chunk_id, populate_existing=True)
        except Exception:
            chunk = None
        if chunk is None or chunk.paper_id != paper_id or not isinstance(chunk.content, str):
            result[item["id"]] = copied
            continue
        needle = _quote_core(item.get("quote"))
        context = _context_from_display_quote(chunk.content, needle, limit=max_chars)
        if context is not None and context != item.get("quote"):
            copied["audit_quote"] = context
        result[item["id"]] = copied
    return result

