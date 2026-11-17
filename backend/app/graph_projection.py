"""One durable full-library graph, validated against a consistent input snapshot.

Queries only filter this projection. A hot read never matches references or writes
relation history. Cold builds serialize with all SQLite writers and publish the
relation revisions and their projection in the same transaction.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable

from sqlalchemy import CheckConstraint, Integer, String, Text, delete, event, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base
from .models import Paper, Reference
from .research_models import RelationEvidence, RelationRevision
from .source_models import (Source, SourceRevision, SourceSnapshot, SourceExtraction,
                            SourceLibraryBinding, SourceLibraryImport, SourceLocator)


# Bump whenever matching, grouping, graph serialization or centrality changes.
ALGORITHM_VERSION = "full-library-graph-1"
PROJECTION_ID = 1


class GraphProjection(Base):
    __tablename__ = "graph_projection"
    __table_args__ = (CheckConstraint("id = 1", name="graph_projection_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    algorithm_version: Mapped[str] = mapped_column(String(80))
    input_sha256: Mapped[str] = mapped_column(String(64))
    # Text allows damaged JSON to be rejected and rebuilt without an ORM JSON
    # decoder failing before we can validate the cached value.
    document_json: Mapped[str] = mapped_column(Text)
    document_sha256: Mapped[str] = mapped_column(String(64))


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def input_fingerprint(db: Session) -> str:
    """Hash actual matching/locating inputs, never row counts or timestamps.

    linked_paper_id is an output of the projection, so including it would make
    the projection invalidate itself. Queries are ordered for restart stability.
    """
    fields = (
        ("papers", (Paper.id, Paper.status, Paper.title, Paper.arxiv_id,
                    Paper.version_group, Paper.page_count, Paper.published_at,
                    Paper.content_hash)),
        ("references", (Reference.id, Reference.paper_id, Reference.ref_index,
                        Reference.raw_text, Reference.title, Reference.page,
                        Reference.bbox)),
        ("relations", (RelationEvidence.id, RelationEvidence.source_paper_id,
                       RelationEvidence.version, RelationEvidence.extracted,
                       RelationEvidence.effective, RelationEvidence.manual)),
    )
    digest = hashlib.sha256()
    digest.update(_canonical(ALGORITHM_VERSION).encode("utf-8"))
    for name, columns in fields:
        digest.update(_canonical(name).encode("utf-8"))
        for row in db.execute(select(*columns).order_by(columns[0])):
            # Each row is a complete JSON array; separate with a newline to
            # preserve table and row boundaries even for arbitrary file text.
            digest.update(_canonical(list(row)).encode("utf-8"))
            digest.update(b"\n")
    # Citation passages are extracted only on cold builds. Their immutable
    # source identities and extractor version must invalidate an otherwise
    # unchanged match, without reparsing original files on warm graph reads.
    from .citation_contexts import CITATION_CONTEXT_VERSION, fingerprint_inputs
    digest.update(_canonical(["citation_context_version", CITATION_CONTEXT_VERSION]).encode("utf-8"))
    for row in fingerprint_inputs(db):
        digest.update(_canonical(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _valid_document(graph) -> bool:
    if not isinstance(graph, dict) or set(graph) != {"nodes", "edges", "ranking"}:
        return False
    if any(not isinstance(graph[k], list) for k in graph):
        return False
    ids, members = set(), set()
    node_keys = {"id", "title", "member_ids", "version_count", "page_count",
                 "published_at", "pagerank", "in_centrality", "out_centrality"}
    for node in graph["nodes"]:
        if not isinstance(node, dict) or set(node) != node_keys:
            return False
        if not isinstance(node["id"], str) or not node["id"] or node["id"] in ids:
            return False
        if not isinstance(node["title"], str):
            return False
        if not isinstance(node["member_ids"], list) or not node["member_ids"]:
            return False
        if any(not isinstance(m, str) or not m or m in members for m in node["member_ids"]):
            return False
        if len(set(node["member_ids"])) != len(node["member_ids"]):
            return False
        if node["id"] not in node["member_ids"]:
            return False
        if type(node["version_count"]) is not int or node["version_count"] != len(node["member_ids"]):
            return False
        if type(node["page_count"]) is not int or node["page_count"] < 0:
            return False
        if node["published_at"] is not None and not isinstance(node["published_at"], str):
            return False
        for key in ("pagerank", "in_centrality", "out_centrality"):
            value = node[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                return False
        ids.add(node["id"])
        members.update(node["member_ids"])
    edge_keys = {"source", "target", "via", "match_type", "relation_id",
                 "relation_version", "relation_type", "status"}
    edge_ids = set()
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or set(edge) != edge_keys:
            return False
        if any(not isinstance(edge[k], str) for k in edge_keys - {"relation_version"}):
            return False
        if edge["source"] not in ids or edge["target"] not in ids or edge["source"] == edge["target"]:
            return False
        if not edge["relation_id"] or type(edge["relation_version"]) is not int or edge["relation_version"] < 1:
            return False
        identity = (edge["source"], edge["target"], edge["relation_id"])
        if identity in edge_ids:
            return False
        edge_ids.add(identity)
    expected = sorted(
        ({"id": n["id"], "title": n["title"], "pagerank": n["pagerank"]} for n in graph["nodes"]),
        key=lambda n: n["pagerank"], reverse=True,
    )
    return graph["ranking"] == expected


def _read_cached(db: Session, fingerprint: str) -> dict | None:
    item = db.get(GraphProjection, PROJECTION_ID)
    if (item is None or item.algorithm_version != ALGORITHM_VERSION
            or item.input_sha256 != fingerprint
            or _sha(item.document_json) != item.document_sha256):
        return None
    try:
        graph = json.loads(item.document_json)
        return graph if _valid_document(graph) else None
    except (ValueError, TypeError, OverflowError):
        return None


def load_graph(db: Session, build: Callable[[Session], dict]) -> dict:
    """Load one full graph without committing or reusing the caller's state.

    SQLite's explicit BEGIN is required for a real read snapshot even with its
    legacy driver transaction mode. A cold read releases its snapshot before
    taking BEGIN IMMEDIATE, then checks again under the cross-process writer
    lock. This prevents an older build from overwriting newer source/review data.
    """
    bind = db.get_bind()
    with Session(bind=bind, autoflush=False) as snapshot:
        snapshot.execute(text("BEGIN"))
        graph = _read_cached(snapshot, input_fingerprint(snapshot))
        snapshot.rollback()
        if graph is not None:
            return graph

    with Session(bind=bind, autoflush=False) as writer:
        try:
            writer.execute(text("BEGIN IMMEDIATE"))
            graph = _read_cached(writer, input_fingerprint(writer))
            if graph is not None:
                writer.rollback()
                return graph
            graph = build(writer)
            if not _valid_document(graph):
                raise ValueError("图谱构建结果不完整，未保存派生关系")
            writer.flush()
            # Matching may have created/revised relations, which are inputs of
            # the next read. Fingerprint only after those writes have flushed.
            fingerprint = input_fingerprint(writer)
            document = _canonical(graph)
            item = writer.get(GraphProjection, PROJECTION_ID)
            if item is None:
                item = GraphProjection(id=PROJECTION_ID)
                writer.add(item)
            item.algorithm_version = ALGORITHM_VERSION
            item.input_sha256 = fingerprint
            item.document_json = document
            item.document_sha256 = _sha(document)
            writer.commit()
            return graph
        except BaseException:
            writer.rollback()
            raise


# Derived excerpts must not survive source deletion until the next graph GET.
# ORM object deletes and bulk ORM/Core deletes invalidate in the same source
# transaction. Raw SQL bypasses hooks, but still cannot pass the next input hash.
# No persistent SQLite trigger is added: backups reject untrusted triggers.
_INPUT_MODELS = (Paper, Reference, RelationEvidence, RelationRevision, Source,
                 SourceRevision, SourceSnapshot, SourceExtraction,
                 SourceLibraryBinding, SourceLibraryImport, SourceLocator)
_INPUT_TABLES = {model.__tablename__ for model in _INPUT_MODELS}


@event.listens_for(Session, "before_flush")
def _invalidate_object_deletes(db, _context, _instances):
    if any(isinstance(obj, _INPUT_MODELS) for obj in db.deleted):
        db.connection().execute(delete(GraphProjection))


@event.listens_for(Session, "do_orm_execute")
def _invalidate_bulk_deletes(state):
    if state.is_delete and getattr(state.statement.table, "name", None) in _INPUT_TABLES:
        state.session.connection().execute(delete(GraphProjection))
