"""Independent full-body cache correspondence checks; fabricated fixed sources only.

The real source producer builds every initial body and locator. A cache entry's
self-hash is deliberately recomputed after corruption, so passing these tests
requires correspondence with the immutable source, not just hash consistency.
"""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app import source_documents, source_runtime, source_store, source_tools
from app.agent import readonly_cache
from app.models import Conversation
from app.runtime_context import bind_runtime_config
from app.source_parsing import MEDIA, extract
from scripts.verify_real_acceptance import fixture_bytes


CID = "independent-full-body-cache"


@pytest.fixture(autouse=True)
def forbid_external_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("independent fixed-source cache review must not send HTTP")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def fixed_source(db, name):
    db.add(Conversation(id=CID))
    db.commit()
    scope = source_store.SourceScope("conversation", CID)
    media = MEDIA[name.rsplit(".", 1)[1]]
    raw = fixture_bytes(name)
    extraction = None if media == MEDIA["pdf"] else extract(raw, media)
    record = source_store.capture_source(db, scope=scope,
        authorization=source_store.SourceAuthorization(scope, "independent-upload", "upload"),
        kind="upload", title=name, raw_bytes=raw, media_type=media,
        **({"text": extraction.text, "text_origin": "parsed_text", "parser": extraction.parser}
           if extraction else {}))
    if extraction:
        source_documents.save_extraction(db, record["revision_id"], scope=scope, extraction=extraction)
    db.commit()
    binding = source_runtime.freeze_sources(db, CID, [record["revision_id"]],
        {"conversation_id": CID, "run_id": "independent-full-body-run"})
    db.commit()
    return record, binding


def arguments_for(record, name):
    bounds = {"pdf": {"page_index": 0}, "docx": {"paragraph_start": 1, "paragraph_end": 2},
        "xlsx": {"sheet": "Budget", "cell_range": "A1:C2"},
        "csv": {"sheet": "CSV", "cell_range": "A1:D2"}}
    return {"source_revision_id": record["revision_id"],
        **bounds.get(name.rsplit(".", 1)[1], {"line_start": 1, "line_end": 2})}


def read(db, binding, arguments, cache, *, action=None):
    with bind_runtime_config(binding):
        return readonly_cache.attached_read(db, arguments, binding=binding,
            conversation_id=CID, cache=cache, action=action or
            (lambda: source_tools.execute_source_tool(db, "read_attached_source", arguments, conversation_id=CID)))


def replace_body(entry, body):
    entry["result"] = json.dumps(body, ensure_ascii=False)
    entry["result_sha256"] = hashlib.sha256(entry["result"].encode()).hexdigest()


@pytest.mark.parametrize("name", ["pilot.txt", "pilot.md", "pilot.pdf", "protocol.docx", "budget.xlsx", "scores.csv"])
def test_six_real_fixed_formats_hit_without_reinvoking_the_original_producer(db, name):
    record, binding = fixed_source(db, name)
    arguments = arguments_for(record, name)
    cache = {}
    original = read(db, binding, arguments, cache)
    db.commit()
    assert not json.loads(original).get("error") and json.loads(original)["evidence"] and len(cache) == 1
    restored = json.loads(json.dumps(cache))
    def forbidden():
        pytest.fail("unchanged authorized body must reuse its source-verified cache")
    assert read(db, binding, arguments, restored, action=forbidden) == original


@pytest.mark.parametrize("field", ["read_range", "read_contract", "source_directory", "table_context"])
def test_self_hashed_nonquote_public_field_corruption_reexecutes_the_real_producer(db, field):
    name = "budget.xlsx" if field == "table_context" else "pilot.txt"
    record, binding = fixed_source(db, name)
    arguments = arguments_for(record, name)
    cache = {}
    first = read(db, binding, arguments, cache)
    db.commit()
    entry = next(iter(cache.values()))
    original_identity = deepcopy(entry["identity"])
    body = json.loads(entry["result"])
    if field == "read_range":
        body[field]["total_lines"] += 1000
    elif field == "read_contract":
        body[field]["original_visual_content_observed"] = True
    elif field == "source_directory":
        body[field]["title"] = "FORGED-FIXED-SOURCE-DIRECTORY"
    else:
        body[field]["formula_recalculation_performed"] = True
        body[field]["cells"][0]["text"] = "FORGED-TABLE-CELL"
    replace_body(entry, body)
    assert entry["identity"] == original_identity
    calls = []
    def actual():
        calls.append(1)
        return source_tools.execute_source_tool(db, "read_attached_source", arguments, conversation_id=CID)
    second = read(db, binding, arguments, cache, action=actual)
    assert calls == [1]
    assert json.loads(second) == json.loads(first)
    assert next(iter(cache.values()))["result"] == second


def test_a_real_same_source_locator_for_another_range_cannot_replace_the_requested_read(db):
    name = "pilot.txt"
    record, binding = fixed_source(db, name)
    arguments = arguments_for(record, name)
    cache = {}
    first = read(db, binding, arguments, cache)
    other_arguments = {"source_revision_id": record["revision_id"], "line_start": 3, "line_end": 4}
    with bind_runtime_config(binding):
        other = source_tools.execute_source_tool(db, "read_attached_source", other_arguments, conversation_id=CID)
    db.commit()
    first_body, other_body = json.loads(first), json.loads(other)
    assert first_body["locator"]["locator_id"] != other_body["locator"]["locator_id"]
    assert other_body["source"]["revision_id"] == first_body["source"]["revision_id"]
    scope = source_store.SourceScope("conversation", CID)
    assert source_store.read_locator(db, other_body["locator"]["locator_id"], scope=scope)["quote"] == other_body["locator"]["quote"]
    entry = next(iter(cache.values()))
    original_identity = deepcopy(entry["identity"])
    replace_body(entry, other_body)
    assert entry["identity"] == original_identity
    calls = []
    def actual():
        calls.append(1)
        return source_tools.execute_source_tool(db, "read_attached_source", arguments, conversation_id=CID)
    second = read(db, binding, arguments, cache, action=actual)
    assert calls == [1] and json.loads(second) == first_body
