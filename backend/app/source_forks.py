"""Copy explicitly forked fixed source references into the new conversation."""
from __future__ import annotations

from copy import deepcopy

from . import source_store as store
from .source_runtime import conversation_scope, freeze_sources, validate_source_evidence
from .source_documents import read_document, save_extraction
from .source_parsing import Extraction
from .source_models import SourceExtraction


def fork_record_sources(db, record, old_cid, new_cid, cache):
    old_scope = conversation_scope(db, old_cid, writing=False)
    new_scope = conversation_scope(db, new_cid)
    binding, evidence = deepcopy(record.runtime_config), deepcopy(record.evidence or [])
    ids = (binding or {}).get("source_revision_ids", [])
    validate_source_evidence(db, old_cid, evidence)

    def clone(rid):
        if rid in cache:
            return cache[rid]
        original = store.read_revision(db, rid, scope=old_scope, include_raw=True)
        copied = store.capture_source(db, scope=new_scope,
            authorization=store.SourceAuthorization(new_scope, "fork:" + rid, original["kind"]),
            kind=original["kind"], title=original["title"], raw_bytes=original["raw_bytes"], text=original["text"],
            media_type=original["media_type"], origin_ref="fork:" + old_cid + ":" + rid,
            text_origin=original["text_origin"], parser=original["parser"])
        if db.get(SourceExtraction, rid):
            doc = read_document(db, rid, scope=old_scope)
            payload = {k: v for k, v in doc.items() if k not in ("source_id", "revision_id", "fingerprint", "title")}
            save_extraction(db, copied["revision_id"], scope=new_scope,
                            extraction=Extraction(doc["text"], doc["parser"], payload))
        cache[rid] = copied
        return copied

    if binding is not None:
        binding = freeze_sources(db, new_cid, [clone(rid)["revision_id"] for rid in ids], binding)
    for item in evidence:
        if not item.get("revision_id"):
            continue
        copied = clone(item["revision_id"])
        locator = store.read_locator(db, item["locator_id"], scope=old_scope)
        location = deepcopy(locator["location"])
        if location.get("extraction_fingerprint"):
            location["extraction_fingerprint"] = read_document(db, copied["revision_id"], scope=new_scope)["fingerprint"]
        new_locator = store._save_locator(db, copied["revision_id"], locator["kind"], location, locator["quote"])
        item.update(source_id=copied["source_id"], revision_id=copied["revision_id"], locator_id=new_locator["locator_id"])
        if item.get("source_location") is not None:
            item["source_location"] = location
    return binding, evidence


def remap_plan_sources(value, cache):
    if isinstance(value, list):
        return [remap_plan_sources(item, cache) for item in value]
    if isinstance(value, dict):
        result = {key: remap_plan_sources(item, cache) for key, item in value.items()}
        for key in ("source_revision_id", "revision_id"):
            if isinstance(result.get(key), str) and result[key] in cache:
                result[key] = cache[result[key]]["revision_id"]
        if isinstance(result.get("source_revision_ids"), list):
            result["source_revision_ids"] = [cache[rid]["revision_id"] if rid in cache else rid
                                              for rid in result["source_revision_ids"]]
        return result
    return value
