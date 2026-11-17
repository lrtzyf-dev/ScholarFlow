"""Bounded writing excerpts from the same immutable sources used by the reader.

Locating an excerpt proves its provenance, not that it supports a generated claim.
"""
from __future__ import annotations

from .. import source_store as store
from ..source_documents import prepare_extraction, save_extraction, read_document, add_document_locator
from ..source_library import open_library_source
from ..library_sources import ensure_owned
from ..source_runtime import conversation_scope
from ..rag.retriever import _strong_tokens

MAX_EXCERPT = 1600


def _window(blocks, anchor):
    """Keep nearby lines/paragraphs together; a heading alone is not evidence."""
    start = next(i for i, block in enumerate(blocks) if block is anchor)
    result, size = [], 0
    for block in blocks[start:start + 8]:
        if block["kind"] != anchor["kind"] or block.get("sheet") != anchor.get("sheet"):
            break
        if result and size + len(block["text"]) > MAX_EXCERPT:
            break
        result.append(block); size += len(block["text"]) + 1
    return result


def collect_evidence(db, refs, idea, conversation_id, *, fixed_revisions=None):
    if not conversation_id:
        return ["未关联来源会话，仅提供书目信息；无法核验论点或定位原文。"]
    scope = conversation_scope(db, conversation_id)
    db.commit()  # No embedding/provider request may inherit a source write lock.
    tokens = _strong_tokens(idea)
    warnings = []
    for ref in refs:
        try:
            with db.begin_nested():
                if fixed_revisions is None:
                    fixed, _ = ensure_owned(db, ref.paper_id)
                else:
                    fixed = store.read_revision(db, fixed_revisions[ref.paper_id],
                        scope=store.SourceScope("library", ref.paper_id), include_raw=True)
            db.commit()
            # Local parsing occurs before opening the extraction writer.
            owned_scope = store.SourceScope("library", ref.paper_id)
            extraction = prepare_extraction(db, fixed["revision_id"], scope=owned_scope)
            db.rollback()
            with db.begin_nested():
                conversation_scope(db, conversation_id)
                if extraction:
                    save_extraction(db, fixed["revision_id"], scope=owned_scope, extraction=extraction)
            db.commit()
            with db.begin_nested():
                record, _ = open_library_source(db, ref.paper_id, conversation_id,
                    library_revision_id=fixed["revision_id"] if fixed_revisions is not None else None)
                doc = read_document(db, record["revision_id"], scope=scope)
                blocks = [b for b in doc["blocks"] if b.get("text", "").strip()]
                ranked = sorted(enumerate(blocks), key=lambda pair: (
                    -len(tokens & _strong_tokens(pair[1]["text"])), pair[0]))[:2]
                for index, (_, block) in enumerate(ranked, 1):
                    rid = record["revision_id"]
                    window = _window(doc["blocks"], block)
                    if doc["format"] == "pdf":
                        # Geometry is derived by the fixed extraction, never by the model.
                        location = {k: v for k, v in block.items() if k not in ("kind", "text")}
                        location.update(raw_sha256=doc["raw_sha256"], text_sha256=doc["text_sha256"],
                                        extraction_fingerprint=doc["fingerprint"])
                        loc = store._save_locator(db, rid, "pdf_page", location, block["text"])
                    elif doc["format"] == "word":
                        if block.get("block_id"):
                            loc = add_document_locator(db, rid, scope=scope, kind="word_block", block_id=block["block_id"])
                        else:
                            p = block["paragraph_index"]
                            loc = add_document_locator(db, rid, scope=scope, kind="word_paragraphs",
                                                       paragraph_start=p, paragraph_end=window[-1]["paragraph_index"])
                    elif doc["format"] in ("excel", "csv"):
                        from openpyxl.utils.cell import range_boundaries, get_column_letter
                        right = max(range_boundaries(item["cell_range"])[2] for item in window)
                        bottom = range_boundaries(window[-1]["cell_range"])[3]
                        loc = add_document_locator(db, rid, scope=scope, kind="sheet_cells",
                            sheet=block["sheet"], cell_range=block["cell_range"].split(":")[0] + ":" + f"{get_column_letter(right)}{bottom}")
                    else:
                        loc = store.add_text_locator(db, rid, scope=scope,
                            line_start=block["line_start"], line_end=window[-1]["line_end"])
                    quote = loc["quote"][:MAX_EXCERPT]
                    if not quote.strip():
                        continue
                    ref.evidence.append({"id": f"F{ref.ref_index}E{index}",
                        "paper_id": ref.paper_id, "source_id": record["source_id"], "revision_id": rid,
                        "locator_id": loc["locator_id"], "source_title": record["title"],
                        "source_fingerprint": record["snapshot_fingerprint"],
                        "quote": quote, "location": loc["location"], "excerpt_truncated": len(loc["quote"]) > MAX_EXCERPT,
                        "verification": "source_frozen_semantics_unverified"})
            db.commit()
            if not ref.evidence:
                warnings.append(f"[{ref.ref_index}] 未取得可定位文本，不能作为论点依据。")
        except store.SourceError as exc:
            db.rollback()
            if fixed_revisions is not None or isinstance(exc,store.SourceCorruption):
                raise
            ref.evidence.clear()
            warnings.append(f"[{ref.ref_index}] 原文证据未就绪：{exc}")
    return warnings
