"""Durable matrix extraction over immutable source/corpus data snapshots.

Only query embeddings can be paid operations. Their exact responses use the
shared research checkpoint journal; deterministic ranking and cell publication
never start an independent task state machine or re-buy known responses.
"""
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
import re
import sqlite3
from types import SimpleNamespace

import numpy as np
from sqlalchemy import select, text, update

from . import research_store as matrices, source_store as sources
from .db import SessionLocal
from .models import Chunk
from .research_models import ResearchMatrix, MatrixRevision, MatrixRunSnapshot
from .research_runs import descriptor, digest
from .source_documents import prepare_extraction, save_extraction, read_document
from .source_runtime import conversation_scope, validate_source_evidence
from .timeutil import utcnow

ALGORITHM = "fixed-matrix-retrieval-v1"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_VECTOR_BYTES = 128 * 1024 * 1024
MAX_PASSAGES = 200_000


def validate_inputs(value):
    if not isinstance(value, dict) or set(value) != {"matrix_id", "expected_version", "field_keys", "paper_ids"}:
        raise sources.SourceError("矩阵任务输入格式无效")
    if not isinstance(value["matrix_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["matrix_id"]):
        raise sources.SourceError("请选择已有证据矩阵")
    if type(value["expected_version"]) is not int or value["expected_version"] < 1:
        raise sources.SourceError("请提供当前矩阵版本")
    for key, limit, pattern in (("field_keys", 40, r"[A-Za-z][A-Za-z0-9_-]{0,63}"),
                                ("paper_ids", 200, r"[a-f0-9]{32}")):
        values = value[key]
        if values is not None and (not isinstance(values, list) or not 1 <= len(values) <= limit
                or any(not isinstance(v, str) or not re.fullmatch(pattern, v) for v in values)
                or len(set(values)) != len(values)):
            raise sources.SourceError("矩阵提取选择无效或重复")
    return deepcopy(value)


def goal_description(inputs):
    return "从固定来源逐格提取证据候选并保存矩阵；保留人工复核、缺项和版本冲突，不将检索命中宣称为科研结论核验。"


def capture_config(db, conversation_id):
    from .model_profiles import options_snapshot
    from .pipeline.embeddings import embedding_connection
    from .pipeline.vectordb import current_embedding_fingerprint
    from .resource_policy import snapshot_policy
    from .config import settings, require_available_credentials, _settings_lock
    options = options_snapshot(db, conversation_id)
    with _settings_lock:
        require_available_credentials("embedding_api_key")
        # Local retrieval must work without opening an unrelated chat credential.
        connection = (embedding_connection() if settings.embedding_mode in ("api", "litellm") else
            {"mode": "local", "base_url": "", "model_id": "hash-ngram-v1"})
        connection["mode"] = "api" if connection["mode"] in ("api", "litellm") else "local"
        fingerprint = current_embedding_fingerprint()
        dimension, minimum = settings.vector_dim, float(settings.min_relevance_score)
    return {"version": 1, "mode": "local", "model_id": ALGORITHM, "profile_name": "固定来源矩阵检索",
        "profile_id": None, "credential_ref": None, "approval_mode": options["approval_mode"],
        "policy_revision": options["revision"], "resource_policy": snapshot_policy(db, conversation_id),
        "matrix_embedding_fingerprint": fingerprint,
        "matrix_embedding": {k: connection[k] for k in ("mode", "base_url", "model_id")},
        "matrix_local_dimension": dimension, "matrix_min_relevance": minimum}


def validate_binding(db, binding):
    from .pipeline.vectordb import current_embedding_fingerprint
    from .config import require_available_credentials, _settings_lock
    with _settings_lock:
        require_available_credentials("embedding_api_key")
        fingerprint = current_embedding_fingerprint()
    if binding.get("model_id") != ALGORITHM or binding.get("mode") != "local":
        raise sources.SourceConflict("矩阵检索算法已变化，已知结果保留，请新建任务")
    if binding.get("matrix_embedding_fingerprint") != fingerprint:
        raise sources.SourceConflict("Embedding 配置已变化，未借用旧凭证或更换检索方法；请恢复原配置或新建任务")


def snapshot_config(binding):
    return {"matrix_algorithm": ALGORITHM, "cell_results": [], "cell_results_sha256": digest([])}


def _encoded(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _fingerprint(row):
    return digest({"run_id": row.run_id, "conversation_id": row.conversation_id, "matrix_id": row.matrix_id,
        "kind": row.kind, "document": row.document, "vectors_sha256": row.vectors_sha256, "byte_count": row.byte_count})


def _save_snapshot(db, state, kind, document, vectors=b""):
    encoded = _encoded(document)
    if len(encoded) > MAX_JSON_BYTES or len(vectors) > MAX_VECTOR_BYTES:
        raise sources.SourceError("固定矩阵检索快照超过本次内存与存储预算，请减少选中资料后分批提取；未发送新的模型请求")
    row = MatrixRunSnapshot(run_id=state.runtime_config["run_id"], conversation_id=state.conversation_id,
        matrix_id=descriptor(state)["inputs"]["matrix_id"], kind=kind, document=deepcopy(document),
        vectors=vectors, vectors_sha256=hashlib.sha256(vectors).hexdigest(), byte_count=len(encoded) + len(vectors))
    row.fingerprint = _fingerprint(row)
    db.add(row)
    db.flush()
    return row


def _snapshot(db, state, kind):
    row = db.scalar(select(MatrixRunSnapshot).where(MatrixRunSnapshot.run_id == state.runtime_config["run_id"],
        MatrixRunSnapshot.kind == kind).execution_options(populate_existing=True))
    if row is None or row.conversation_id != state.conversation_id or row.matrix_id != descriptor(state)["inputs"]["matrix_id"]:
        raise sources.SourceCorruption("矩阵固定快照缺失，未自动重新提取或付费")
    if not isinstance(row.document, dict) or not isinstance(row.vectors, bytes):
        raise sources.SourceCorruption("矩阵固定快照结构损坏，未自动重新调用模型")
    encoded = _encoded(row.document)
    if (len(encoded) > MAX_JSON_BYTES or len(row.vectors) > MAX_VECTOR_BYTES
            or row.byte_count != len(encoded) + len(row.vectors)
            or hashlib.sha256(row.vectors).hexdigest() != row.vectors_sha256 or _fingerprint(row) != row.fingerprint
            or descriptor(state).get(kind + "_snapshot_fingerprint") not in (None, row.fingerprint)):
        raise sources.SourceCorruption("矩阵固定快照字节或摘要损坏，未自动重新调用模型")
    return row


def initialize(db, state):
    job, inputs = descriptor(state), descriptor(state)["inputs"]
    item = matrices.matrix(db, inputs["matrix_id"], state.conversation_id, writing=True)
    if item.version != inputs["expected_version"]:
        raise sources.SourceConflict("矩阵已更新，请载入当前版本再创建提取任务")
    document = deepcopy(item.document)
    fields = inputs["field_keys"] or [f["key"] for f in document["dimensions"]]
    ids = inputs["paper_ids"] or [p["id"] for p in document["papers"]]
    if not set(fields) <= {f["key"] for f in document["dimensions"]} or not set(ids) <= {p["id"] for p in document["papers"]}:
        raise sources.SourceError("提取选择不属于此矩阵固定版本")
    scope = conversation_scope(db, state.conversation_id)
    from .source_models import SourceExtraction
    snapshots = []
    for paper in document["papers"]:
        if paper["id"] not in ids:
            continue
        record = sources.read_revision(db, paper["revision_id"], scope=scope)
        if record["raw_sha256"] != paper["content_sha256"]:
            raise sources.SourceCorruption("矩阵来源原件与版本摘要不一致")
        extraction = db.get(SourceExtraction, paper["revision_id"])
        fingerprint = read_document(db, paper["revision_id"], scope=scope)["fingerprint"] if extraction else None
        snapshots.append({"paper_id": paper["id"], "revision_id": paper["revision_id"],
            "raw_sha256": record["raw_sha256"], "extraction_fingerprint": fingerprint})
    row = _save_snapshot(db, state, "matrix", {"document": document, "base_version": item.version,
        "fields": fields, "paper_ids": ids, "title": item.title})
    job.update(matrix_snapshot_fingerprint=row.fingerprint, source_snapshot=snapshots,
        source_snapshot_sha256=digest(snapshots), base_version=item.version)


def _results(job):
    cells = job.get("cell_results")
    if (not isinstance(cells, list) or any(not isinstance(c, dict) or not isinstance(c.get("cell"), dict)
            or not isinstance(c["cell"].get("evidence"), list) for c in cells)
            or digest(cells) != job.get("cell_results_sha256")):
        raise sources.SourceCorruption("矩阵逐格结果校验失败，未重新收费")
    identities = [(c.get("field_key"), c.get("paper_id")) for c in cells]
    if len(set(identities)) != len(identities):
        raise sources.SourceCorruption("矩阵逐格进度重复或损坏")
    return cells


def _authorize_sources(db, state, writing):
    scope = conversation_scope(db, state.conversation_id, writing=writing)
    job = descriptor(state)
    rows = job.get("source_snapshot")
    if not isinstance(rows, list) or digest(rows) != job.get("source_snapshot_sha256"):
        raise sources.SourceCorruption("矩阵固定来源快照校验失败")
    # Extraction may be prepared after admission, before any paid query. From
    # that point the immutable corpus supplies the exact extraction identity.
    columns = ("run_id", "conversation_id", "matrix_id", "kind", "document", "vectors_sha256", "byte_count", "fingerprint")
    metadata = db.execute(select(*(getattr(MatrixRunSnapshot, k) for k in columns)).where(
        MatrixRunSnapshot.run_id == state.runtime_config["run_id"], MatrixRunSnapshot.kind == "corpus")).mappings().first()
    if metadata is None and job.get("corpus_snapshot_fingerprint"):
        raise sources.SourceCorruption("固定检索语料已缺失，未重新提取或收费")
    if metadata is not None and (_fingerprint(SimpleNamespace(**metadata)) != metadata["fingerprint"]
            or job.get("corpus_snapshot_fingerprint") not in (None, metadata["fingerprint"])):
        raise sources.SourceCorruption("固定检索语料身份或内容校验失败，未重新收费")
    corpus = metadata["document"] if metadata else None
    frozen_extractions = {p["revision_id"]: p["extraction_fingerprint"] for p in corpus["papers"]} if corpus else {}
    for source in rows:
        record = sources.read_revision(db, source["revision_id"], scope=scope)
        if record["raw_sha256"] != source["raw_sha256"]:
            raise sources.SourceCorruption("矩阵固定原件损坏")
        expected_extraction = source.get("extraction_fingerprint") or frozen_extractions.get(source["revision_id"])
        if expected_extraction:
            document = read_document(db, source["revision_id"], scope=scope)
            if document["fingerprint"] != expected_extraction:
                raise sources.SourceCorruption("矩阵解析版本已变化，未替换固定来源")
    evidence = [e for cell in _results(job) for e in cell["cell"].get("evidence", [])]
    evidence.extend(_document_evidence(_snapshot(db, state, "matrix").document["document"]))
    validate_source_evidence(db, state.conversation_id, evidence)


def authorize(db, state):
    if descriptor(state).get("matrix_algorithm") != ALGORITHM:
        raise sources.SourceConflict("矩阵执行版本不兼容，已知结果保留")
    _snapshot(db, state, "matrix")
    matrices.matrix(db, descriptor(state)["inputs"]["matrix_id"], state.conversation_id, writing=True)
    _authorize_sources(db, state, True)


def public_authorize(db, state):
    _snapshot(db, state, "matrix")
    matrices.matrix(db, descriptor(state)["inputs"]["matrix_id"], state.conversation_id)
    job = descriptor(state)
    if job.get("corpus_snapshot_fingerprint"):
        _snapshot(db, state, "corpus")
    _authorize_sources(db, state, False)
    from .research_runs import checked_payload
    payload, _ = checked_payload(state, job)
    if payload is not None:
        validate_source_evidence(db, state.conversation_id, _document_evidence(payload["document"]))


def _location(block):
    kind = block.get("kind")
    if kind == "pdf_page" and block.get("geometry_verified"):
        return {"kind": "pdf_page", **{k: block[k] for k in ("page_index", "page_number", "bbox", "page_width",
            "page_height", "rotation", "coordinate_space") if k in block}, "geometry_verified": True, "precision": "page"}
    if kind == "text_line":
        return {"kind": "text_lines", "line_start": block["line_start"], "line_end": block["line_end"]}
    if kind in ("word_paragraph", "word_table_row"):
        return {"kind": "word_paragraphs", "paragraph_start": block["paragraph_index"], "paragraph_end": block["paragraph_index"]}
    if kind == "word_part":
        return {"kind": "word_block", **{k: block[k] for k in ("block_id", "part_name", "xml_path", "section_kind", "part_label",
            "table_path", "table_row", "table_column", "column_span", "vertical_merge", "table_depth", "logical_grid") if k in block},
            "rendered_page_available": False}
    if kind == "sheet_row":
        return {"kind": "sheet_cells", "sheet": block["sheet"], "cell_range": block["cell_range"],
                "index_base": 1, "formula_values_recalculated": False}
    return None


def _preserve_library_extraction(db, paper, document):
    """Materialize the same parse on its already authorized library version.

    Old originals can predate structured extraction. Parsing only their chat
    alias would break the established equivalence required before deletion.
    Existing different parses are immutable and are never silently replaced.
    """
    from .source_models import SourceLibraryRevisionOrigin
    from .source_parsing import Extraction
    mapping = db.get(SourceLibraryRevisionOrigin, paper["revision_id"])
    if mapping is None:
        return
    if mapping.paper_id != paper["id"]:
        raise sources.SourceCorruption("固定矩阵来源与已授权文献库版本映射不一致")
    scope = sources.SourceScope("library", paper["id"])
    original = sources.read_revision(db, mapping.library_revision_id, scope=scope)
    if original["raw_sha256"] != document["raw_sha256"]:
        raise sources.SourceCorruption("文献库原件与矩阵固定来源不一致，未复制解析")
    saved = {k: deepcopy(v) for k, v in document.items() if k not in ("source_id", "revision_id", "fingerprint", "title")}
    retained = save_extraction(db, mapping.library_revision_id, scope=scope,
        extraction=Extraction(document["text"], document["parser"], saved))
    if retained["fingerprint"] != document["fingerprint"]:
        raise sources.SourceCorruption("文献库已有不同的固定解析，未覆盖或替换原件")


def _prepare_corpus(context):
    from .pipeline.embeddings import LocalEmbedder
    from .pipeline.vectordb import NumpyVectorStore
    from .library_index_vectors import query_store
    state, job = context.state, descriptor(context.state)
    with SessionLocal() as db:
        existing = db.scalar(select(MatrixRunSnapshot.id).where(MatrixRunSnapshot.run_id == context.run_id,
            MatrixRunSnapshot.kind == "corpus"))
        if existing:
            row = _snapshot(db, state, "corpus")
            job["corpus_snapshot_fingerprint"] = row.fingerprint
            return deepcopy(row.document), bytes(row.vectors)
        if job.get("corpus_snapshot_fingerprint") or _results(job):
            raise sources.SourceCorruption("矩阵固定语料已缺失，已知进度保留，未重新收费")
        fixed = deepcopy(_snapshot(db, state, "matrix").document)
    corpus, binary, total = [], bytearray(), 0
    scope = sources.SourceScope("conversation", state.conversation_id)
    for paper in fixed["document"]["papers"]:
        if paper["id"] not in fixed["paper_ids"]:
            continue
        context.authorize()
        with SessionLocal() as db:
            prepared = prepare_extraction(db, paper["revision_id"], scope=scope)
            db.rollback()
            context.authorize()
            db.execute(text("BEGIN IMMEDIATE"))
            from . import run_lifecycle
            run_lifecycle.assert_writer(db, context.run_id, state.attempt_id, state.writer_token, lock=True)
            authorize(db, state)
            if prepared:
                save_extraction(db, paper["revision_id"], scope=scope, extraction=prepared)
            document = read_document(db, paper["revision_id"], scope=scope)
            _preserve_library_extraction(db, paper, document)
            db.commit()
            by_quote = {}
            if state.runtime_config["matrix_embedding"]["mode"] == "api":
                index = query_store(db, NumpyVectorStore())
                chunks = list(db.scalars(select(Chunk).where(Chunk.paper_id == paper["id"])))
                vectors = index.vectors_for([c.id for c in chunks]) if index.embedding_cache_matches_current() else {}
                for chunk in chunks:
                    if chunk.id in vectors:
                        by_quote.setdefault(chunk.content, vectors[chunk.id])
        passages = []
        text_lines = document["text"].splitlines(keepends=True) if document["format"] == "text" else []
        for block in document["blocks"]:
            location, quote = _location(block), block.get("text", "")
            if block.get("kind") == "text_line":
                # Library text locators embed the exact slice including CR/LF.
                # Reuse only that same input, never a stripped approximation.
                quote = "".join(text_lines[block["line_start"] - 1:block["line_end"]])
            if not location or not quote.strip() or block.get("retrieval_role") == "compatibility":
                continue
            total += 1
            if total > MAX_PASSAGES:
                raise sources.SourceError("固定矩阵语料超过200000段，请减少资料并分批提取；尚未发送查询向量请求")
            vector = (LocalEmbedder(state.runtime_config["matrix_local_dimension"]).encode([quote])[0]
                if state.runtime_config["matrix_embedding"]["mode"] == "local" else by_quote.get(quote))
            item = {"id": f"passage-{total}", "quote": quote, "location": location}
            if vector is not None:
                values = np.asarray(vector, dtype="<f4")
                if values.ndim != 1 or not 1 <= len(values) <= 65536 or not np.isfinite(values).all():
                    raise sources.SourceCorruption("固定检索向量数据无效，未改用其他方法")
                item.update(vector_offset=len(binary), vector_dimension=len(values))
                binary.extend(values.tobytes())
                if len(binary) > MAX_VECTOR_BYTES:
                    raise sources.SourceError("固定向量快照超过128MiB，请减少资料后分批提取；尚未发起查询向量请求")
            passages.append(item)
        corpus.append({"paper_id": paper["id"], "title": paper["title"], "revision_id": paper["revision_id"],
            "source_id": paper["source_id"], "raw_sha256": document["raw_sha256"], "text_sha256": document["text_sha256"],
            "extraction_fingerprint": document["fingerprint"], "source_version": paper["source_version"],
            "source_media_type": paper["source_media_type"], "passages": passages})
    context.authorize()
    data = {"papers": corpus, "algorithm": ALGORITHM, "embedding": state.runtime_config["matrix_embedding"],
        "min_relevance": state.runtime_config["matrix_min_relevance"], "passage_count": total}
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        from . import run_lifecycle
        run_lifecycle.assert_writer(db, context.run_id, state.attempt_id, state.writer_token, lock=True)
        from .research_children import authorize_state
        authorize_state(db, state)
        authorize(db, state)
        row = _save_snapshot(db, state, "corpus", data, bytes(binary))
        db.commit()
        job["corpus_snapshot_fingerprint"] = row.fingerprint
    context.save()
    return data, bytes(binary)


def _query_vector(context, field, corpus):
    from .pipeline.embeddings import LocalEmbedder, APIEmbedder, embedding_connection
    if not any("vector_dimension" in p for paper in corpus["papers"] for p in paper["passages"]):
        return None
    binding = context.state.runtime_config
    if binding["matrix_embedding"]["mode"] == "local":
        return LocalEmbedder(binding["matrix_local_dimension"]).encode([field["query"]])[0]
    def request():
        connection = embedding_connection()
        client = APIEmbedder(connection=connection, authorization_check=context.authorize)
        return client.encode([field["query"]]).tolist()
    value = context.perform("matrix-query/" + digest(field), "matrix_query_embedding", field, request)
    vector = np.asarray(value, dtype=np.float32)
    if vector.ndim != 2 or vector.shape[0] != 1 or not 1 <= vector.shape[1] <= 65536 or not np.isfinite(vector).all():
        raise sources.SourceCorruption("已保存的查询向量无效，未重新收费")
    dimensions = {p["vector_dimension"] for paper in corpus["papers"] for p in paper["passages"] if "vector_dimension" in p}
    if dimensions != {vector.shape[1]}:
        raise sources.SourceConflict("查询模型返回的向量维度与固定语料不一致；已知响应保留，未静默降级或再次收费")
    return vector[0]


def _rank(paper, query, vector, binary, *, local_mode, minimum):
    from .rag.retriever import _fts_query, _strong_tokens, rrf_fuse, _mmr_select
    from .pipeline.indexer import cjk_spaced
    passages = paper["passages"]
    by_id = {p["id"]: p for p in passages}
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE VIRTUAL TABLE passages USING fts5(id UNINDEXED,content)")
        db.executemany("INSERT INTO passages(id,content) VALUES (?,?)", [(p["id"], cjk_spaced(p["quote"])) for p in passages])
        match = _fts_query(query)
        lexical = [r[0] for r in db.execute("SELECT id FROM passages WHERE passages MATCH ? ORDER BY rank LIMIT 8", (match,))] if match else []
    scores, vectors = {}, {}
    if vector is not None:
        for passage in passages:
            if passage.get("vector_dimension") == len(vector):
                start, size = passage["vector_offset"], len(vector) * 4
                if type(start) is not int or start < 0 or start + size > len(binary):
                    raise sources.SourceCorruption("固定向量偏移无效")
                value = np.frombuffer(binary[start:start + size], dtype="<f4")
                if not np.isfinite(value).all():
                    raise sources.SourceCorruption("固定向量包含无效数值")
                value = value / (np.linalg.norm(value) or 1.0)
                vectors[passage["id"]] = value
                scores[passage["id"]] = float(value @ (vector / (np.linalg.norm(vector) or 1.0)))
    dense = sorted(scores, key=scores.get, reverse=True)[:8]
    fused = rrf_fuse([lexical, dense], topk=8)
    strong = _strong_tokens(query)
    selected = []
    for ident, score in fused:
        overlap = strong & _strong_tokens(by_id[ident]["quote"])
        relevant = bool(overlap) if local_mode or ident not in scores else scores[ident] >= minimum
        if relevant:
            selected.append((ident, score, scores.get(ident, len(overlap) / max(1, len(strong)))))
    # Same MMR helper as library retrieval when all candidates have vectors.
    if selected and vector is not None and all(i in vectors for i, _, _ in selected):
        chosen = _mmr_select(vector / (np.linalg.norm(vector) or 1.0),
            np.asarray([vectors[i] for i, _, _ in selected]), [s for _, s, _ in selected], topk=2)
        selected = [selected[i] for i in chosen]
    else:
        selected = selected[:2]
    method = "local_hash_ngram_fts_rrf" if local_mode else "api_embedding_fts_rrf" if scores else "lexical_no_compatible_fixed_vectors"
    return [{**by_id[i], "score": score, "relevance": rel} for i, score, rel in selected], method


def _evidence(context, paper, passages):
    state = context.state
    scope = sources.SourceScope("conversation", state.conversation_id)
    result = []
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        from . import run_lifecycle
        run_lifecycle.assert_writer(db, context.run_id, state.attempt_id, state.writer_token, lock=True)
        authorize(db, state)
        for number, passage in enumerate(passages, 1):
            location = deepcopy(passage["location"])
            kind = location.pop("kind")
            location.update(raw_sha256=paper["raw_sha256"], text_sha256=paper["text_sha256"],
                extraction_fingerprint=paper["extraction_fingerprint"])
            if kind == "pdf_page": location["page_index_base"] = 0
            elif kind == "text_lines": location["line_index_base"] = 1
            elif kind == "word_paragraphs": location["paragraph_index_base"] = 1
            locator = sources._save_locator(db, paper["revision_id"], kind, location, passage["quote"])
            result.append({"id": f"M{number}", "paper_id": paper["paper_id"], "paper_title": paper["title"],
                "source_id": paper["source_id"], "revision_id": paper["revision_id"], "locator_id": locator["locator_id"],
                "quote": passage["quote"], "page": location.get("page_number", 0), "bbox": location.get("bbox"),
                "source_location": location, "locator_kind": kind, "source_sha256": paper["raw_sha256"],
                "source_version": paper["source_version"], "source_media_type": paper["source_media_type"],
                "location_status": "fixed_source", "verification_status": "unverified", "semantic_support": "unverified",
                "score": passage["score"], "relevance": passage["relevance"]})
        db.commit()
    return result


def _payload(state, *, applied_version=None, apply_conflict=None, db=None):
    job = descriptor(state)
    with (SessionLocal() if db is None else nullcontext(db)) as reader:
        fixed = deepcopy(_snapshot(reader, state, "matrix").document)
        if applied_version is not None:
            saved = reader.scalar(select(MatrixRevision).where(MatrixRevision.matrix_id == job["inputs"]["matrix_id"],
                MatrixRevision.version == applied_version))
            if (saved is None or saved.document.get("matrix_run", {}).get("run_id") != state.runtime_config["run_id"]
                    or digest(saved.document.get("data")) != saved.document["matrix_run"].get("document_sha256")):
                raise sources.SourceCorruption("已交付矩阵版本的保存回执缺失，未重新写入")
            document = deepcopy(saved.document["data"])
        else:
            document = fixed["document"]
    cells = _results(job)
    for result in cells if applied_version is None else []:
        row = next((r for r in document["rows"] if r["key"] == result["field_key"]), None)
        if row is None or result["paper_id"] not in row["cells"]:
            raise sources.SourceCorruption("矩阵结果不属于固定选择")
        row["cells"][result["paper_id"]] = _merged_cell(row["cells"][result["paper_id"]], result,
            state.runtime_config["run_id"], history=False)
    return {"matrix_id": job["inputs"]["matrix_id"], "base_version": fixed["base_version"], "document": document,
        "progress": {"completed": len(cells), "total": len(fixed["fields"]) * len(fixed["paper_ids"])},
        "applied_version": applied_version, "apply_conflict": apply_conflict, "method": "retrieved_passages",
        "method_details": {"algorithm": ALGORITHM, "embedding": deepcopy(state.runtime_config["matrix_embedding"]),
            "cells": [{"field_key": c["field_key"], "paper_id": c["paper_id"], "method": c["method"]} for c in cells],
            "note": "检索候选不是语义支持核验；本地哈希向量不等同云端语义模型。无兼容固定向量时逐格明确标为词法检索。"}}


def _same_schema(left, right):
    return (left.get("dimensions") == right.get("dimensions") and left.get("schema_version") == right.get("schema_version")
        and [{k: p.get(k) for k in ("id", "revision_id", "content_sha256")} for p in left.get("papers", [])]
        == [{k: p.get(k) for k in ("id", "revision_id", "content_sha256")} for p in right.get("papers", [])])


def _document_evidence(document):
    return [e for row in document["rows"] for cell in row["cells"].values()
        for e in [*cell.get("evidence", []), *(cell.get("extraction_candidate") or {}).get("evidence", [])]]


def _merged_cell(current, result, run_id, *, history=True):
    if current.get("manual"):
        # An audited conclusion and its evidence are one reviewed version.
        # New retrieval cannot borrow that human verification for new quotes.
        updated = deepcopy(current)
        candidate = {k: deepcopy(result["cell"].get(k)) for k in ("evidence", "quote", "extraction_status")}
        candidate.update(verification_status="unverified", method=result["method"], run_id=run_id, requires_review=True)
        candidate["sha256"] = digest(candidate)
        updated["extraction_candidate"] = candidate
    else:
        updated = deepcopy(result["cell"])
    if history:
        updated["extraction_history"] = [*deepcopy(current.get("extraction_history", [])),
            {k: deepcopy(current.get(k)) for k in ("evidence", "quote", "status", "extraction_status",
                "verification_status", "manual", "extraction_candidate")}]
    return updated


def _apply_receipt(db, state, expected_version, request_id):
    job = descriptor(state)
    intent = digest({"run_id": state.runtime_config["run_id"], "expected_version": expected_version, "request_id": request_id,
                     "matrix_snapshot_fingerprint": job["matrix_snapshot_fingerprint"]})
    for revision in db.scalars(select(MatrixRevision).where(MatrixRevision.matrix_id == job["inputs"]["matrix_id"],
            MatrixRevision.action.in_(("extract_run", "merge_run")))):
        receipt = revision.document.get("matrix_run", {})
        if receipt.get("run_id") == state.runtime_config["run_id"] and receipt.get("request_id") == request_id:
            if receipt.get("intent_sha256") != intent:
                raise sources.SourceConflict("合并请求编号已绑定不同输入")
            if digest(revision.document.get("data")) != receipt.get("document_sha256"):
                raise sources.SourceCorruption("矩阵保存回执内容损坏，未重复写入")
            return revision.version, intent
    return None, intent


def _apply(db, state, expected_version, request_id, *, automatic=False):
    job = descriptor(state)
    fixed = _snapshot(db, state, "matrix").document
    item = matrices.matrix(db, job["inputs"]["matrix_id"], state.conversation_id, writing=True)
    previous, intent = _apply_receipt(db, state, expected_version, request_id)
    if previous is not None:
        return previous, True
    if item.version != expected_version:
        raise sources.SourceConflict("矩阵已被其他窗口修改；提取结果已保存，可基于当前版本明确合并，未重新收费")
    if not _same_schema(fixed["document"], item.document):
        raise sources.SourceConflict("矩阵字段或固定来源版本已变化，不能把旧提取合并到新结构")
    document = deepcopy(item.document)
    for result in _results(job):
        row = next(r for r in document["rows"] if r["key"] == result["field_key"])
        current = row["cells"][result["paper_id"]]
        row["cells"][result["paper_id"]] = _merged_cell(current, result, state.runtime_config["run_id"])
    validate_source_evidence(db, state.conversation_id, _document_evidence(document))
    advanced = db.execute(update(ResearchMatrix).where(ResearchMatrix.id == item.id, ResearchMatrix.version == expected_version)
        .values(document=document, version=expected_version + 1, updated_at=utcnow()), execution_options={"synchronize_session": False})
    if advanced.rowcount != 1:
        raise sources.SourceConflict("矩阵版本冲突，人工修改未被覆盖")
    db.add(MatrixRevision(matrix_id=item.id, version=expected_version + 1, action="extract_run" if automatic else "merge_run",
        actor="matrix_run", document={"title": item.title, "data": deepcopy(document), "matrix_run": {
            "run_id": state.runtime_config["run_id"], "request_id": request_id, "intent_sha256": intent,
            "matrix_snapshot_fingerprint": job["matrix_snapshot_fingerprint"], "document_sha256": digest(document)}}))
    db.flush()
    return expected_version + 1, False


def execute(context):
    state, job = context.state, descriptor(context.state)
    corpus, binary = _prepare_corpus(context)
    with SessionLocal() as db:
        fixed = deepcopy(_snapshot(db, state, "matrix").document)
    context.set_payload(_payload(state))
    completed = {(c["field_key"], c["paper_id"]) for c in _results(job)}
    for field in fixed["document"]["dimensions"]:
        if field["key"] not in fixed["fields"] or all((field["key"], p) in completed for p in fixed["paper_ids"]):
            continue
        context.authorize()
        vector = _query_vector(context, field, corpus)
        for paper in corpus["papers"]:
            identity = (field["key"], paper["paper_id"])
            if identity in completed:
                continue
            context.authorize()
            passages, method = _rank(paper, field["query"], vector, binary,
                local_mode=state.runtime_config["matrix_embedding"]["mode"] == "local",
                minimum=corpus["min_relevance"])
            evidence = _evidence(context, paper, passages)
            base_row = next(r for r in fixed["document"]["rows"] if r["key"] == field["key"])
            cell = deepcopy(base_row["cells"][paper["paper_id"]])
            cell.update(evidence=evidence, quote=" ".join(e["quote"] for e in evidence),
                        extraction_status="retrieved" if evidence else "not_found")
            if not cell.get("manual"):
                cell.update(status="unverified" if evidence else "not_found", verification_status="unverified")
            job["cell_results"].append({"field_key": field["key"], "paper_id": paper["paper_id"], "cell": cell, "method": method})
            job["cell_results_sha256"] = digest(job["cell_results"])
            context.set_payload(_payload(state))
            context.activity("extracting", f"已保存 {len(job['cell_results'])}/{len(fixed['fields']) * len(fixed['paper_ids'])} 格固定来源候选")
            completed.add(identity)
    context.authorize()
    applied, conflict = None, None
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        from . import run_lifecycle
        run_lifecycle.assert_writer(db, context.run_id, state.attempt_id, state.writer_token, lock=True)
        authorize(db, state)
        try:
            applied, _ = _apply(db, state, job.get("automatic_apply_base_version", fixed["base_version"]),
                "automatic-" + job["cell_results_sha256"], automatic=True)
            db.commit()
        except sources.SourceConflict as exc:
            db.rollback()
            conflict = {"code": "matrix_version_conflict", "message": str(exc), "requires_explicit_apply": True}
    payload = _payload(state, applied_version=applied, apply_conflict=conflict)
    if conflict:
        job["resume_blocked_reason"] = "提取候选已保存；矩阵有人工修改，请核对预览并明确合并到当前版本，无需重新提取。"
    else:
        job.pop("resume_blocked_reason", None)
    context.set_payload(payload)
    return payload, conflict is None


def apply_saved(db, run_id, conversation_id, expected_version, request_id, *, parent_binding=None):
    from . import research_runs, run_lifecycle
    from .agent.checkpoint import AgentState
    run, attempt, state = research_runs.checked(db, run_id, conversation_id, writing=True)
    from .research_children import assert_linked_control, attach_attempt, parent_record
    link = assert_linked_control(db, run_id, parent_binding)
    if link:
        state.runtime_config = {**state.runtime_config, "research_parent": parent_record(parent_binding, link.id)}
    if run.kind != "matrix":
        raise sources.SourceNotFound("此任务不是证据矩阵提取")
    if run_id in research_runs.active() or run.status in ("queued", "running"):
        raise sources.SourceConflict("任务仍在执行，请停止或等待后再合并已知结果")
    public_authorize(db, state)
    if not _results(descriptor(state)):
        raise sources.SourceConflict("尚无已知提取结果可合并；未重新发送请求")
    run_lifecycle.assert_writer(db, run_id, attempt.id, attempt.writer_token, allow_terminal=True, lock=True)
    previous, intent = _apply_receipt(db, state, expected_version, request_id)
    if previous is not None:
        return True
    if run_lifecycle.admitted_request(db, run_id, request_id) is not None:
        raise sources.SourceConflict("合并请求编号已用于另一执行输入")
    prior_payload, _ = research_runs.checked_payload(state, descriptor(state))
    if (prior_payload or {}).get("applied_version") is not None:
        raise sources.SourceConflict("已知结果已保存到矩阵，无需重复合并；未重新发送请求")
    run_lifecycle.assert_conversation_idle(db, conversation_id, exclude_run_id=run_id, trusted_parent=parent_binding)
    applied, _ = _apply(db, state, expected_version, request_id)
    payload = _payload(state, applied_version=applied, db=db)
    job = descriptor(state)
    job.pop("resume_blocked_reason", None)
    job.update(last_payload=deepcopy(payload), last_payload_sha256=digest(payload), automatic_apply_base_version=applied,
        delivery_input={"request_id": request_id, "expected_matrix_version": expected_version, "intent_sha256": intent,
                        "cell_results_sha256": job["cell_results_sha256"], "source_attempt_id": attempt.id})
    complete = payload["progress"]["completed"] == payload["progress"]["total"] and not state.pending_operation
    state.result = {"payload": payload, "completed": complete, "outcome": "completed" if complete else "partial",
        "incomplete_steps": [] if complete else [{"title": "补齐矩阵未完成的提取格子"}],
        "stop_reason": "" if complete else "matrix_cells_incomplete"}
    state.phase, job["stage"] = "deliver", "completed" if complete else "partial"
    # Persist a new delivery attempt on the same run/goal; never rewrite a
    # terminal extraction attempt, even when only part of its output is merged.
    delivered, _ = run_lifecycle.admit_attempt(db, run_id, conversation_id, state.question, kind="matrix",
        checkpoint=state.snapshot(), resume=True, request_id=request_id, expected_revision=run.revision)
    frozen = AgentState.model_validate(delivered.checkpoint)
    frozen.runtime_config = {**frozen.runtime_config, "attempt_id": delivered.id}
    if link:
        delivered.checkpoint = frozen.snapshot()
        attach_attempt(db, link, delivered, parent_binding)
        frozen = AgentState.model_validate(delivered.checkpoint)
    frozen.phase = "completed"
    run_lifecycle.save_checkpoint(db, run_id, frozen.snapshot())
    db.flush()
    return False


def prepare_resume(state):
    job = descriptor(state)
    _results(job)
    if job.get("resume_blocked_reason"):
        raise sources.SourceConflict(job["resume_blocked_reason"])
