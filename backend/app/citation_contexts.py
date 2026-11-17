"""Conservative citation passages from immutable, library-owned source versions.

Matching a marker establishes where it occurs, not that its claim is true. The
reference list is excluded. Unsupported layouts stay missing rather than using
a bibliography entry as the purported body passage.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
from pathlib import Path
import re

from sqlalchemy import select

from . import source_store as sources
from .models import Paper
from .source_models import (Source, SourceRevision, SourceSnapshot, SourceExtraction, SourceLocator,
                            SourceLibraryBinding, SourceLibraryImport)

CITATION_CONTEXT_VERSION = "citation-context-v2"
_NUMERIC = re.compile(r"\[(\d{1,5}(?:\s*[,，;；\-–—]\s*\d{1,5})*)\]")
_AUTHOR = re.compile(r"\b([A-Z][A-Za-z'’\-]{1,60})(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z'’\-]+))?\s*(?:\(\s*|,\s*)(\d{4}[a-z]?)\s*\)?")
_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\d+[.\s]+)?(?:references|bibliography|参考文献|引用文献)\s*[:：]?\s*$", re.I)


def fingerprint_inputs(db):
    """Small immutable metadata inputs; projection hits never parse originals."""
    yield ["citation_context_algorithm", CITATION_CONTEXT_VERSION]
    for row in db.execute(select(SourceLibraryBinding.paper_id, SourceLibraryBinding.revision_id)
                          .order_by(SourceLibraryBinding.paper_id)):
        yield ["binding", *row]
    for row in db.execute(select(SourceLibraryImport.paper_id, SourceLibraryImport.raw_sha256, SourceRevision.id, SourceRevision.fingerprint,
            Source.scope_type, Source.scope_id, SourceSnapshot.raw_sha256, SourceSnapshot.text_sha256,
            SourceSnapshot.media_type, SourceExtraction.fingerprint)
            .join(SourceRevision, SourceRevision.id == SourceLibraryImport.revision_id)
            .join(Source, Source.id == SourceRevision.source_id)
            .join(SourceSnapshot, SourceSnapshot.id == SourceRevision.snapshot_id)
            .outerjoin(SourceExtraction, SourceExtraction.revision_id == SourceRevision.id)
            .order_by(SourceLibraryImport.paper_id, SourceRevision.id)):
        yield ["fixed_source", *row]
    from .research_models import RelationEvidence
    ids = sorted({context.get("locator_id") for (extracted,) in db.execute(select(RelationEvidence.extracted))
                  for context in (extracted or {}).get("citation_contexts", []) if context.get("locator_id")})
    for start in range(0, len(ids), 200):
        for row in db.execute(select(SourceLocator.id, SourceLocator.revision_id, SourceLocator.kind,
                SourceLocator.location, SourceLocator.quote, SourceLocator.fingerprint)
                .where(SourceLocator.id.in_(ids[start:start + 200])).order_by(SourceLocator.id)):
            yield ["context_locator", *row]
    # Before the first fixed copy, availability is an input too. Once pinned,
    # later changes to a legacy path must not alter that immutable evidence.
    bound = set(db.scalars(select(SourceLibraryBinding.paper_id)))
    from .config import PDF_DIR
    root = PDF_DIR.resolve()
    for pid, name in db.execute(select(Paper.id, Paper.pdf_path).where(Paper.status == "parsed").order_by(Paper.id)):
        if pid in bound:
            continue
        try:
            path = Path(name or "").resolve()
            stat = path.stat() if path.is_relative_to(root) and path.is_file() else None
            value = [stat.st_size, stat.st_mtime_ns] if stat else None
        except (OSError, ValueError):
            value = None
        yield ["unfixed_original", pid, name, value]


def _numbers(value):
    result = set()
    for part in re.split(r"[,，;；]", value):
        ends = re.split(r"[\-–—]", part.strip())
        if len(ends) == 1:
            result.add(int(ends[0]))
        elif len(ends) == 2 and int(ends[0]) <= int(ends[1]) <= int(ends[0]) + 200:
            result.update(range(int(ends[0]), int(ends[1]) + 1))
    return result


def _author_key(ref):
    raw = re.sub(r"^\s*\[\d+\]\s*", "", ref.raw_text or "")
    surname = re.match(r"([A-Z][A-Za-z'’\-]{1,60})(?:\s*,|\s+[A-Z]\.?)", raw)
    year = re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", raw)
    return (surname[1].casefold(), year[1].casefold()) if surname and year else None


def _passages(document):
    """Yield source blocks in extraction order, stopping at bibliography heading."""
    for block in document.get("blocks", []):
        for line in block.get("text", "").splitlines():
            if _HEADING.fullmatch(line):
                return
            # A numbered bibliography-like line is never promoted to body text.
            if re.match(r"^\s*\[\d+\]\s+", line):
                continue
            for sentence in re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+(?=[A-Z\u4e00-\u9fff])", line):
                sentence = sentence.strip()
                if sentence and len(sentence) <= 2000:
                    yield {**block, "_citation_line": line}, sentence


def _passage_index(document):
    numeric, authors = {}, {}
    for order, (block, sentence) in enumerate(_passages(document)):
        for match in _NUMERIC.finditer(sentence):
            for number in _numbers(match[1]):
                if number not in numeric and len(numeric) >= 5000:
                    continue
                entries = numeric.setdefault(number, [])
                if len(entries) < 8 and (not entries or entries[-1][0] != order):
                    entries.append((order, block, sentence, match[0], "numeric_brackets"))
        for match in _AUTHOR.finditer(sentence):
            key = (match[1].casefold(), match[2].casefold())
            if key not in authors and len(authors) >= 5000:
                continue
            entries = authors.setdefault(key, [])
            if len(entries) < 8 and (not entries or entries[-1][0] != order):
                entries.append((order, block, sentence, match[0], "author_year"))
    return numeric, authors


def extract_contexts(document, ref, max_contexts=8, *, _index=None):
    """Pure matching; the graph shares one passage index for every paper/ref."""
    key = _author_key(ref)
    numeric, authors = _index if _index is not None else _passage_index(document)
    found = []
    candidates = sorted(numeric.get(ref.ref_index, []) + authors.get(key, []), key=lambda c: c[0])
    seen = set()
    for order, block, sentence, marker, method in candidates:
        entry = " ".join(re.sub(r"^\s*\[\d+\]\s*", "", ref.raw_text or "").split())
        original_line = " ".join(re.sub(r"^\s*\[\d+\]\s*", "", block.get("_citation_line", "")).split())
        if order in seen or sentence == (ref.raw_text or "").strip() or original_line == entry:
            continue
        seen.add(order)
        kind = block.get("kind")
        if kind == "pdf_page" and block.get("geometry_verified"):
            location = {"kind": "pdf_page", **{k: block[k] for k in ("page_index", "page_number", "bbox",
                "page_width", "page_height", "rotation", "coordinate_space") if k in block},
                "geometry_verified": True, "precision": "page"}
        elif kind == "text_line":
            location = {"kind": "text_lines", "line_start": block["line_start"], "line_end": block["line_end"]}
        elif kind in ("word_paragraph", "word_table_row"):
            location = {"kind": "word_paragraphs", "paragraph_start": block["paragraph_index"],
                        "paragraph_end": block["paragraph_index"]}
        elif kind == "word_part":
            location = {"kind": "word_block", **{k: block[k] for k in ("block_id", "part_name", "xml_path", "section_kind", "part_label")}}
        elif kind == "sheet_row":
            location = {"kind": "sheet_cells", "sheet": block["sheet"], "cell_range": block["cell_range"]}
        else:
            continue
        candidate = {"quote": sentence, "marker": marker, "match_method": method, "location": location,
            "semantic_support": "unverified"}
        if candidate not in found:
            found.append(candidate)
        if len(found) >= max_contexts:
            break
    return {"contexts": found, "status": "located" if found else "not_found",
        "extractor_version": CITATION_CONTEXT_VERSION,
        "supported_styles": ["numeric_square_brackets", "latin_surname_year"],
        "note": "最多显示8处正文引用标记，不证明引用目标或科学结论正确；未覆盖上标编号及全部参考文献样式。"}


def _pdf_regions(raw):
    """One pass per paper, shared by every reference; never one PDF parse/ref."""
    import pdfplumber
    result = {}
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for i, page in enumerate(pdf.pages):
            words = page.extract_words()
            result[i] = words
            page.close()
    return result


def _region(words, quote):
    text, spans = "", []
    for word in words:
        start = len(text)
        text += (" " if text else "") + word["text"]
        spans.append((start, len(text), word))
    expected = " ".join(quote.split())
    offset = text.find(expected)
    if offset < 0 or text.find(expected, offset + 1) >= 0:
        return None
    selected = [w for start, end, w in spans if end > offset and start < offset + len(expected)]
    return [min(w["x0"] for w in selected), min(w["top"] for w in selected),
            max(w["x1"] for w in selected), max(w["bottom"] for w in selected)] if selected else None


def _fixed_document(db, paper):
    cache = db.info.setdefault("graph_citation_documents", {})
    key = (paper.id, paper.content_hash)
    if key not in cache:
        try:
            from .library_sources import ensure_owned
            from .source_documents import read_document, prepare_extraction, save_extraction
            record, _ = ensure_owned(db, paper.id)
            scope = sources.SourceScope("library", paper.id)
            prepared = prepare_extraction(db, record["revision_id"], scope=scope)
            doc = (save_extraction(db, record["revision_id"], scope=scope, extraction=prepared)
                   if prepared else read_document(db, record["revision_id"], scope=scope))
            try:
                regions = _pdf_regions(record["raw_bytes"]) if doc["format"] == "pdf" else {}
            except Exception:
                # Saved extraction still supplies a verified page anchor. A
                # failed optional region pass must not invent precise boxes.
                regions = {}
            cache[key] = (record, doc, regions, _passage_index(doc))
        except (sources.SourceError, OSError, ValueError):
            cache[key] = None
    return cache[key]


def relation_contexts(db, paper, ref):
    fixed = _fixed_document(db, paper)
    if fixed is None:
        return {"citation_contexts": [], "citation_sentence": None, "sentence_status": "source_unavailable",
                "context_extractor_version": CITATION_CONTEXT_VERSION}
    record, doc, regions, index = fixed
    result = extract_contexts(doc, ref, _index=index)
    contexts = []
    for item in result["contexts"]:
        loc = deepcopy(item["location"])
        kind = loc.pop("kind")
        if kind == "pdf_page":
            bbox = _region(regions.get(loc["page_index"], []), item["quote"])
            if bbox:
                page_bbox = loc["bbox"]
                if page_bbox[0] <= bbox[0] < bbox[2] <= page_bbox[2] and page_bbox[1] <= bbox[1] < bbox[3] <= page_bbox[3]:
                    loc["bbox"], loc["precision"] = bbox, "text_region"
            loc.update(raw_sha256=record["raw_sha256"], text_sha256=doc["text_sha256"],
                       extraction_fingerprint=doc["fingerprint"], page_index_base=0)
        else:
            # This location came from the validated, cached extraction above,
            # never an API body. Re-reading/hashing an Office/text original for
            # each reference would turn one cold build into O(bytes * refs).
            loc.update(raw_sha256=record["raw_sha256"], text_sha256=doc["text_sha256"],
                       extraction_fingerprint=doc["fingerprint"])
            if kind == "text_lines":
                loc["line_index_base"] = 1
            elif kind == "word_paragraphs":
                loc["paragraph_index_base"] = 1
            elif kind == "word_block":
                loc["rendered_page_available"] = False
            else:
                loc.update(index_base=1, formula_values_recalculated=False)
        saved = sources._save_locator(db, record["revision_id"], kind, loc, item["quote"])
        contexts.append({**item, "location": saved["location"], "locator_id": saved["locator_id"],
            "revision_id": record["revision_id"], "raw_sha256": record["raw_sha256"],
            "extraction_fingerprint": doc["fingerprint"]})
    return {"citation_contexts": contexts, "citation_sentence": contexts[0]["quote"] if contexts else None,
        "sentence_status": result["status"], "context_extractor_version": CITATION_CONTEXT_VERSION,
        "context_note": result["note"], "context_supported_styles": result["supported_styles"]}


def locate_context(db, item, conversation_id, index):
    """Copy precisely the stored old revision/locator, never a current head."""
    contexts = item.extracted.get("citation_contexts", [])
    if type(index) is not int or not 0 <= index < len(contexts):
        raise sources.SourceNotFound("所选正文引用原句不存在，未改为参考条目或其他版本")
    context = contexts[index]
    scope = sources.SourceScope("library", item.source_paper_id)
    record = sources.read_revision(db, context["revision_id"], scope=scope, include_raw=True)
    if record["raw_sha256"] != context["raw_sha256"] or record["raw_sha256"] != item.extracted["source_sha256"]:
        raise sources.SourceCorruption("正文引用原件指纹不一致")
    locator = sources.read_locator(db, context["locator_id"], scope=scope)
    if locator["revision_id"] != record["revision_id"]:
        raise sources.SourceCorruption("正文引用位置不属于固定原件")
    from .source_documents import read_document
    if read_document(db, record["revision_id"], scope=scope)["fingerprint"] != context["extraction_fingerprint"]:
        raise sources.SourceCorruption("正文引用提取版本校验失败")
    from .source_runtime import conversation_scope
    from .library_sources import copy_record, copy_locator
    target_scope = conversation_scope(db, conversation_id)
    origin = "graph-citation:" + item.id + ":" + record["revision_id"]
    alias = db.scalar(select(SourceRevision).join(Source, Source.id == SourceRevision.source_id).where(
        Source.scope_type == "conversation", Source.scope_id == conversation_id, SourceRevision.origin_ref == origin))
    target = (sources.read_revision(db, alias.id, scope=target_scope, include_raw=True) if alias else
              copy_record(db, record, scope, target_scope, origin))
    if target["raw_sha256"] != record["raw_sha256"]:
        raise sources.SourceCorruption("会话正文引用副本校验失败")
    saved = copy_locator(db, context["locator_id"], scope, target["revision_id"], target_scope)
    return {"revision_id": target["revision_id"], "locator_id": saved["locator_id"],
        "conversation_id": conversation_id, "relation_id": item.id, "relation_version": item.version,
        "location_kind": "citation_context", "context_index": index, "quote": context["quote"],
        "citation_sentence_verified": True, "semantic_support_verified": False}
