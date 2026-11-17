"""Full-library projection correctness, transactionality and persistent reuse."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.orm import Session

from app.api import graph as api
from app.db import SessionLocal, engine
from app import graph_projection as projection
from app.graph_projection import GraphProjection
from app.models import Paper, Reference
from app.research_models import RelationEvidence, RelationRevision
from app.research_relations import review_relation


def seed(db, *, exact=False):
    papers = [
        Paper(id="source", title="Original Literature Survey", status="parsed", content_hash="source-bytes"),
        Paper(id="best", title="Quantum Entanglement Spectral Graph Compression", status="parsed", content_hash="best-bytes"),
        Paper(id="weaker", title="Quantum Entanglement Spectral Graph Compression Extensions", status="parsed", content_hash="weaker-bytes"),
    ]
    if exact:
        papers[1].arxiv_id = "2401.00001"
    db.add_all(papers)
    ref = Reference(id="ref", paper_id="source", ref_index=1,
                    title=papers[1].title,
                    raw_text="arXiv:2401.00001" if exact else papers[1].title,
                    page=2, bbox=[10, 20, 30, 40])
    db.add(ref)
    db.commit()
    return papers, ref


def cache(db):
    db.expire_all()
    return db.get(GraphProjection, 1)


def assert_no_match(*_args, **_kwargs):
    raise AssertionError("A warm graph read must not match references again")


def test_hot_reads_are_read_only_and_share_one_full_projection(db, monkeypatch):
    seed(db, exact=True)
    initial = api.get_graph(db=db)
    assert initial["edges"][0]["target"] == "best"
    assert cache(db).input_sha256 == projection.input_fingerprint(db)
    db.rollback()
    statements = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.strip().split()[0].upper())

    monkeypatch.setattr(api, "_match_reference", assert_no_match)
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert api.get_graph(db=db) == initial
        api.get_graph(db=db, limit=1, offset=1)
        api.get_graph(db=db, paper_ids="source,best", focus="source", relation_type="citation")
        assert '-->' in api.get_diagram(db=db)["script"]
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert not {"UPDATE", "INSERT", "DELETE", "REPLACE"}.intersection(statements)
    assert db.scalar(select(func.count()).select_from(GraphProjection)) == 1
    assert db.scalar(select(func.count()).select_from(RelationRevision)) == 1
    assert db.get(Reference, "ref").linked_paper_id == "best"


def test_subset_cannot_promote_weaker_match_or_change_review_history(db):
    seed(db)
    # Even the first request is a subset: matching must still use the full library.
    subset = api.get_graph(db=db, paper_ids="source,weaker")
    assert {n["id"] for n in subset["nodes"]} == {"source", "weaker"}
    assert subset["edges"] == []
    assert {n["pagerank"] for n in subset["nodes"]} == {0.5}
    all_graph = api.get_graph(db=db)
    rid = all_graph["edges"][0]["relation_id"]
    assert all_graph["edges"][0]["target"] == "best"
    item = db.get(RelationEvidence, rid)
    review_relation(db, item, 1, "correct", "checked", target="weaker", relation_type="citation")
    db.commit()
    reviewed = api.get_graph(db=db, paper_ids="source,best")
    assert reviewed["edges"] == []
    for subset_ids in ("source,weaker", "source,best", "best,weaker"):
        api.get_graph(db=db, paper_ids=subset_ids, relation_type="uncertain")
    db.expire_all()
    item = db.get(RelationEvidence, rid)
    assert item.version == 2 and item.extracted["target"] == "best"
    assert item.effective["target"] == "weaker" and item.manual
    assert db.scalar(select(func.count()).select_from(RelationRevision)) == 2
    assert db.get(Reference, "ref").linked_paper_id == "weaker"


def test_content_changes_with_identical_counts_and_timestamps_invalidate(db, monkeypatch):
    seed(db, exact=True)
    api.get_graph(db=db)
    count = 0
    original = api._compute_full_graph

    def counted(session):
        nonlocal count
        count += 1
        return original(session)

    monkeypatch.setattr(api, "_compute_full_graph", counted)
    # Each item changes an actual input without inserting a row or touching a
    # max timestamp. linked_paper_id is deliberately tested separately below.
    changes = [
        (Paper, "best", "page_count", 9),
        (Paper, "best", "published_at", "2026-11-15"),
        (Paper, "best", "version_group", "one-version"),
        (Paper, "best", "title", "Quantum Entanglement Spectral Graph Compression Updated"),
        (Paper, "best", "content_hash", "new-best-bytes"),
        (Reference, "ref", "page", 4),
        (Reference, "ref", "bbox", [11, 21, 31, 41]),
        (Reference, "ref", "ref_index", 7),
        (Reference, "ref", "title", "Updated reference heading"),
        (Reference, "ref", "raw_text", "arXiv:2401.00001 updated raw citation"),
        (Paper, "best", "arxiv_id", "2401.00002"),
        (Paper, "best", "status", "failed"),
    ]
    for expected, (model, identity, key, value) in enumerate(changes, 1):
        previous = cache(db).input_sha256
        setattr(db.get(model, identity), key, value)
        db.commit()
        api.get_graph(db=db)
        assert count == expected, key
        assert cache(db).input_sha256 != previous, key
    # A derived link must not make a hot read cold. It is repaired by the next
    # genuine input change, not treated as a matching input itself.
    db.get(Reference, "ref").linked_paper_id = "obsolete-derived-only"
    db.commit()
    api.get_graph(db=db)
    assert count == len(changes)


def test_relation_effective_content_and_algorithm_version_invalidate(db, monkeypatch):
    seed(db)
    first = api.get_graph(db=db)
    rid = first["edges"][0]["relation_id"]
    old_hash = cache(db).input_sha256
    item = db.get(RelationEvidence, rid)
    # Hash the actual content even if a writer bypasses the normal version bump.
    item.effective = {**item.effective, "status": "excluded"}
    db.commit()
    assert api.get_graph(db=db)["edges"] == []
    assert cache(db).input_sha256 != old_hash
    old_hash = cache(db).input_sha256
    monkeypatch.setattr(projection, "ALGORITHM_VERSION", "full-library-graph-next-test")
    api.get_graph(db=db)
    assert cache(db).algorithm_version == "full-library-graph-next-test"
    assert cache(db).input_sha256 != old_hash


@pytest.mark.parametrize("raw_sql", [False, True])
def test_fixed_citation_context_locator_delete_invalidates_and_rebuilds(db, monkeypatch, raw_sql):
    from app import citation_contexts
    from app import source_store as sources
    from app.source_models import SourceLibraryBinding, SourceLibraryImport, SourceLocator

    papers, _ = seed(db)
    raw = b"The measured improvement is discussed in the cited work [1].\n\nReferences\n[1] Quantum Entanglement Spectral Graph Compression\n"
    papers[0].content_hash = hashlib.sha256(raw).hexdigest()
    scope = sources.SourceScope("library", "source")
    record = sources.capture_source(db, scope=scope,
        authorization=sources.SourceAuthorization(scope, "projection-context-test", "library"),
        kind="library", title="citation-context.md", raw_bytes=raw, text=raw.decode(),
        media_type="text/markdown", origin_ref="library:source")
    db.add(SourceLibraryBinding(paper_id="source", revision_id=record["revision_id"]))
    db.add(SourceLibraryImport(revision_id=record["revision_id"], paper_id="source",
        raw_sha256=record["raw_sha256"], request_id="projection-context-import", chunk_locators={}))
    db.commit()
    edge = api.get_graph(db=db)["edges"][0]
    db.expire_all()
    item = db.get(RelationEvidence, edge["relation_id"])
    first = item.extracted["citation_contexts"][0]
    assert first["quote"].endswith("cited work [1].")
    assert first["revision_id"] == record["revision_id"]
    old_hash = cache(db).input_sha256
    # A verified cache hit does not parse or reopen its fixed original.
    with monkeypatch.context() as hot:
        hot.setattr(citation_contexts, "_fixed_document", assert_no_match)
        hot.setattr(api, "_match_reference", assert_no_match)
        assert api.get_graph(db=db)["edges"][0] == edge
    if raw_sql:
        db.execute(text("DELETE FROM source_locators WHERE id = :id"), {"id": first["locator_id"]})
    else:
        db.delete(db.get(SourceLocator, first["locator_id"]))
    db.commit()
    if raw_sql:
        assert cache(db).input_sha256 == old_hash
    else:
        assert cache(db) is None
    rebuilt = api.get_graph(db=db)["edges"][0]
    db.expire_all()
    updated = db.get(RelationEvidence, edge["relation_id"]).extracted["citation_contexts"][0]
    assert updated["locator_id"] != first["locator_id"]
    assert updated["quote"] == first["quote"]
    assert rebuilt["relation_version"] == edge["relation_version"] + 1
    assert cache(db).input_sha256 != old_hash


def test_reparse_preserves_review_and_updates_location_and_version_groups(db):
    seed(db)
    first = api.get_graph(db=db)
    rid = first["edges"][0]["relation_id"]
    item = db.get(RelationEvidence, rid)
    review_relation(db, item, 1, "correct", "checked", target="weaker", relation_type="shared_topic")
    db.commit()
    api.get_graph(db=db)
    ref = db.get(Reference, "ref")
    raw, title = ref.raw_text, ref.title
    db.execute(delete(Reference).where(Reference.id == "ref"))
    db.add(Reference(id="reparsed", paper_id="source", title=title, raw_text=raw,
                     ref_index=8, page=5, bbox=[1, 2, 3, 4]))
    db.commit()
    edge = api.get_graph(db=db)["edges"][0]
    assert edge["relation_id"] == rid and edge["target"] == "weaker"
    assert edge["relation_version"] == 3
    db.expire_all()
    item = db.get(RelationEvidence, rid)
    assert item.manual and item.extracted["reference_page"] == 5
    assert item.extracted["reference_bbox"] == [1, 2, 3, 4]
    db.get(Paper, "best").version_group = "target-versions"
    db.get(Paper, "weaker").version_group = "target-versions"
    db.commit()
    graph = api.get_graph(db=db, paper_ids="source,best", focus="best")
    assert graph["total_nodes"] == 2
    merged = next(n for n in graph["nodes"] if n["id"] != "source")
    assert merged["id"] == "weaker" and set(merged["member_ids"]) == {"best", "weaker"}
    assert graph["edges"][0]["target"] == "weaker"
    assert merged["in_centrality"] == 1


def test_focus_and_relation_filters_recompute_an_induced_graph(db):
    seed(db, exact=True)
    db.add(Paper(id="outside", title="Peripheral Retrieval Experiments", status="parsed", content_hash="outside"))
    db.add(Reference(id="outside-ref", paper_id="weaker", title="Peripheral Retrieval Experiments", raw_text="Peripheral Retrieval Experiments", ref_index=1))
    db.commit()
    all_graph = api.get_graph(db=db)
    focus = api.get_graph(db=db, focus="source")
    assert {n["id"] for n in focus["nodes"]} == {"source", "best"}
    assert len(focus["edges"]) == 1
    assert all(e["source"] in {"source", "best"} and e["target"] in {"source", "best"} for e in focus["edges"])
    assert sum(n["pagerank"] for n in focus["nodes"]) == pytest.approx(1)
    assert next(n for n in focus["nodes"] if n["id"] == "best")["in_centrality"] == 1
    filtered = api.get_graph(db=db, relation_type="similar")
    assert filtered["edges"] == []
    assert {n["pagerank"] for n in filtered["nodes"]} == {0.25}
    assert all(n["in_centrality"] == n["out_centrality"] == 0 for n in filtered["nodes"])
    # Pagination preserves boundary edges needed to load the next graph page.
    page = api.get_graph(db=db, limit=1)
    assert all(e["source"] == page["nodes"][0]["id"] or e["target"] == page["nodes"][0]["id"] for e in page["edges"])
    assert api.get_graph(db=db) == all_graph


@pytest.mark.parametrize("model", [Paper, Reference, RelationEvidence, RelationRevision])
@pytest.mark.parametrize("bulk", [False, True])
def test_delete_clears_projection_in_the_same_transaction_and_rollback_restores(db, model, bulk):
    seed(db)
    api.get_graph(db=db)
    obj = db.get(Paper, "source") if model is Paper else db.scalars(select(model)).first()
    assert cache(db) is not None
    if bulk:
        db.execute(delete(model).where(model.id == obj.id))
    else:
        db.delete(obj)
        db.flush()
    # Use a column query to avoid reusing an identity-map cache instance.
    assert db.scalar(select(GraphProjection.id)) is None
    db.rollback()
    assert db.scalar(select(GraphProjection.id)) == 1
    if model is RelationEvidence:
        # A coherent explicit relation removal includes its dependent audit
        # rows (SQLite FK cascading is not enabled by the legacy connection).
        db.execute(delete(RelationRevision).where(RelationRevision.relation_id == obj.id))
    db.execute(delete(model).where(model.id == obj.id))
    db.commit()
    assert db.scalar(select(GraphProjection.id)) is None
    graph = api.get_graph(db=db)
    if model in (Paper, Reference):
        assert graph["edges"] == []


def test_raw_sql_delete_still_invalidates_by_content_and_clears_stale_links(db):
    seed(db, exact=True)
    api.get_graph(db=db)
    previous = cache(db).input_sha256
    db.execute(text("DELETE FROM papers WHERE id = 'best'"))
    db.commit()
    # Raw SQL intentionally bypasses the ORM deletion hooks.
    assert cache(db).input_sha256 == previous
    graph = api.get_graph(db=db)
    assert "best" not in {n["id"] for n in graph["nodes"]}
    # A new full-library title match may remain, but the removed exact target
    # and its derived citation link must never survive the invalidation.
    assert all(e["target"] != "best" and e["relation_type"] == "uncertain" for e in graph["edges"])
    assert cache(db).input_sha256 != previous
    assert db.get(Reference, "ref").linked_paper_id is None


@pytest.mark.parametrize("damage", ["json", "checksum", "schema", "endpoint", "algorithm", "input"])
def test_damaged_projection_is_rebuilt_without_touching_review_history(db, damage):
    seed(db)
    expected = api.get_graph(db=db)
    item = cache(db)
    if damage == "json":
        item.document_json = "{broken"
        item.document_sha256 = hashlib.sha256(item.document_json.encode()).hexdigest()
    elif damage == "checksum":
        item.document_sha256 = "0" * 64
    elif damage in ("schema", "endpoint"):
        document = json.loads(item.document_json)
        if damage == "schema":
            document["nodes"][0]["member_ids"] = []
        else:
            document["edges"][0]["target"] = "missing-endpoint"
        item.document_json = json.dumps(document)
        item.document_sha256 = hashlib.sha256(item.document_json.encode()).hexdigest()
    elif damage == "algorithm":
        item.algorithm_version = "outdated-algorithm"
    else:
        item.input_sha256 = "0" * 64
    db.commit()
    assert api.get_graph(db=db) == expected
    assert cache(db).algorithm_version == projection.ALGORITHM_VERSION
    assert db.scalar(select(func.count()).select_from(RelationRevision)) == 1


def test_failed_build_rolls_back_derived_links_relations_and_cache(db, monkeypatch):
    seed(db, exact=True)
    original = api._compute_full_graph

    def broken(session):
        original(session)
        session.flush()
        raise RuntimeError("synthetic publication failure")

    monkeypatch.setattr(api, "_compute_full_graph", broken)
    with pytest.raises(RuntimeError, match="publication failure"):
        api.get_graph(db=db)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(GraphProjection)) == 0
    assert db.scalar(select(func.count()).select_from(RelationEvidence)) == 0
    assert db.scalar(select(func.count()).select_from(RelationRevision)) == 0
    assert db.get(Reference, "ref").linked_paper_id is None


def test_projection_read_does_not_commit_unrelated_caller_changes(db):
    seed(db, exact=True)
    pending = Paper(id="pending", title="Uncommitted caller data", status="parsed", content_hash="pending")
    db.add(pending)
    graph = api.get_graph(db=db)
    assert "pending" not in {n["id"] for n in graph["nodes"]}
    assert pending in db.new
    with SessionLocal() as other:
        assert other.get(Paper, "pending") is None
    db.rollback()


def test_busy_cold_projection_returns_retryable_response_without_partial_writes(db):
    seed(db, exact=True)
    impatient = create_engine(engine.url, connect_args={"check_same_thread": False, "timeout": 0.05})
    try:
        with engine.connect() as holder:
            holder.execute(text("BEGIN IMMEDIATE"))
            with Session(bind=impatient) as reader:
                with pytest.raises(HTTPException) as error:
                    api.get_graph(db=reader)
            assert error.value.status_code == 503
            assert error.value.headers["Retry-After"] == "2"
            holder.rollback()
    finally:
        impatient.dispose()
    assert db.scalar(select(func.count()).select_from(RelationEvidence)) == 0
    assert db.scalar(select(func.count()).select_from(GraphProjection)) == 0


def test_two_cold_readers_publish_only_once(db, monkeypatch):
    seed(db, exact=True)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    original = api._compute_full_graph
    calls = []
    reader = threading.local()

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if statement == "BEGIN IMMEDIATE" and getattr(reader, "second", False):
            second_started.set()

    def gated(session):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(session)

    monkeypatch.setattr(api, "_compute_full_graph", gated)

    def read(second=False):
        reader.second = second
        with SessionLocal() as session:
            return api.get_graph(db=session)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(read)
            assert entered.wait(10)
            second = pool.submit(read, True)
            try:
                assert second_started.wait(10)
            finally:
                release.set()
            assert first.result(timeout=15) == second.result(timeout=15)
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", observe)
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(GraphProjection)) == 1
    assert db.scalar(select(func.count()).select_from(RelationRevision)) == 1


def test_cold_build_and_source_writer_cannot_publish_an_outdated_snapshot(db, monkeypatch):
    seed(db, exact=True)
    entered, release, update_started = threading.Event(), threading.Event(), threading.Event()
    original = api._compute_full_graph

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith('UPDATE "references" SET raw_text'):
            update_started.set()

    def gated(session):
        graph = original(session)
        entered.set()
        assert release.wait(10)
        return graph

    monkeypatch.setattr(api, "_compute_full_graph", gated)

    def read():
        with SessionLocal() as session:
            return api.get_graph(db=session)

    def update():
        with SessionLocal() as session:
            # The cold builder holds SQLite's writer lock. This update commits
            # after publication; the subsequent GET must reject the old hash.
            session.execute(text("UPDATE \"references\" SET raw_text = :raw, title = :title WHERE id = 'ref'"),
                            {"raw": "Unrelated missing citation", "title": "Unrelated missing citation"})
            session.commit()

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(read)
            assert entered.wait(10)
            writer = pool.submit(update)
            try:
                assert update_started.wait(10)
            finally:
                release.set()
            assert first.result(timeout=15)["edges"][0]["target"] == "best"
            writer.result(timeout=15)
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", observe)
    assert api.get_graph(db=db)["edges"] == []
    assert cache(db).input_sha256 == projection.input_fingerprint(db)


def test_new_process_reuses_verified_projection_without_matching(db):
    seed(db, exact=True)
    expected = api.get_graph(db=db)
    db.rollback()
    script = """
import json
from app.api import graph
from app.db import SessionLocal
def forbidden(*args):
    raise AssertionError('persistent projection was not reused')
graph._match_reference = forbidden
with SessionLocal() as db:
    result = graph.get_graph(db=db)
print(json.dumps(result, sort_keys=True))
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
