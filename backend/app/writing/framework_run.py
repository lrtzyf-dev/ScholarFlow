"""Framework adapter: immutable library inputs and replayable paid operations."""
import numpy as np
from sqlalchemy import select, text

from .. import source_store as sources
from ..db import SessionLocal
from ..library_sources import ensure_owned
from ..models import Paper
from ..source_models import SourceLibraryBinding
from ..source_documents import read_document, prepare_extraction, save_extraction
from ..research_runs import descriptor, digest
from ..pipeline.embeddings import APIEmbedder, LocalEmbedder, embedding_connection, get_embedder
from ..pipeline.vectordb import current_embedding_fingerprint
from ..llm import APIClient, LLMResponse, get_llm
from .framework import build_framework


def validate_inputs(value):
    if not isinstance(value, dict) or set(value) != {"idea", "max_sources"}:
        raise sources.SourceError("论文框架输入格式无效")
    idea, count = value["idea"], value["max_sources"]
    if not isinstance(idea, str) or not 1 <= len(idea.strip()) <= 10000 or type(count) is not int or not 1 <= count <= 20:
        raise sources.SourceError("研究想法须为1至10000字，参考文献上限须为1至20")
    return {"idea": idea.strip(), "max_sources": count}


def goal_description(inputs):
    return "生成可追溯的论文框架草稿。研究想法：" + inputs["idea"]


def snapshot_config(binding):
    from ..config import settings
    return {"embedding_fingerprint": current_embedding_fingerprint() if binding["mode"] != "mock" else None,
        "offline_vector_dim": settings.vector_dim}


def authorize(db, state):
    job = descriptor(state)
    if job["embedding_fingerprint"] and current_embedding_fingerprint() != job["embedding_fingerprint"]:
        raise sources.SourceConflict("Embedding 配置已变化；原任务及已知调用结果保留，请恢复原配置或新建任务")
    for row in job.get("source_snapshot", []):
        paper = db.get(Paper, row["id"], populate_existing=True)
        if paper is None or paper.content_hash != row["raw_sha256"]:
            raise sources.SourceConflict("框架使用的文献已删除或原件已变更，未继续发送请求")
        scope = sources.SourceScope("library", row["id"])
        doc = read_document(db, row["library_revision_id"], scope=scope)
        if doc["fingerprint"] != row["extraction_fingerprint"]:
            raise sources.SourceCorruption("框架固定解析版本校验失败")


def _preserve_existing_aliases(db, paper_id, revision_id, document):
    """Copy a known parse to missing, already-authorized aliases only.

    Existing parses are immutable. This does not reparse a document, create a
    new original version, or expand which library original an alias names.
    """
    from ..source_models import SourceLibraryRevisionOrigin, SourceExtraction
    from ..source_parsing import Extraction
    from ..library_sources import scope_of, equivalent_extraction
    fixed = sources.read_revision(db, revision_id, scope=sources.SourceScope("library", paper_id))
    aliases = db.scalars(select(SourceLibraryRevisionOrigin).where(
        SourceLibraryRevisionOrigin.library_revision_id == revision_id, SourceLibraryRevisionOrigin.paper_id == paper_id))
    for alias in aliases:
        scope = scope_of(db, alias.origin_revision_id)
        origin = sources.read_revision(db, alias.origin_revision_id, scope=scope)
        if not all(origin.get(key) == fixed.get(key) for key in ("raw_sha256", "text_sha256", "media_type", "parser", "text_origin")):
            raise sources.SourceCorruption("框架原件与既有会话版本不同，未复制解析")
        if db.get(SourceExtraction, alias.origin_revision_id) is None:
            saved = {k: v for k, v in document.items() if k not in ("source_id", "revision_id", "fingerprint", "title")}
            save_extraction(db, alias.origin_revision_id, scope=scope,
                extraction=Extraction(document["text"], document["parser"], saved))
        if not equivalent_extraction(db, origin, fixed):
            raise sources.SourceCorruption("框架来源已有不同固定解析，未覆盖原解析")


def freeze(context):
    job = descriptor(context.state)
    if "candidate_snapshot" not in job:
        with SessionLocal() as db:
            query = select(Paper).where(Paper.status == "parsed")
            parent = (context.state.runtime_config or {}).get("research_parent")
            if parent:
                from ..research_children import selected_parent_paper
                selected = selected_parent_paper(db, parent)
                if selected:
                    query = query.where(Paper.id == selected)
            papers = list(db.scalars(query.order_by(Paper.id)))
            bindings = {row.paper_id: row.revision_id for row in db.scalars(select(SourceLibraryBinding))}
            job["candidate_snapshot"] = [{"id": p.id, "title": p.title, "authors": p.authors,
                "arxiv_id": p.arxiv_id, "abstract": p.abstract, "published_at": p.published_at,
                "raw_sha256": p.content_hash, "library_revision_id": bindings.get(p.id)} for p in papers]
        job["source_snapshot"] = []
        context.save()
    completed = {row["id"] for row in job["source_snapshot"]}
    for candidate in job["candidate_snapshot"]:
        if candidate["id"] in completed: continue
        context.authorize()
        scope = sources.SourceScope("library", candidate["id"])
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            fixed, _ = ensure_owned(db, candidate["id"])
            rid = candidate["library_revision_id"] or fixed["revision_id"]
            record = sources.read_revision(db, rid, scope=scope)
            if record["raw_sha256"] != candidate["raw_sha256"]:
                raise sources.SourceConflict("待准备文献原件已改变，请新建框架任务")
            db.commit()
            extraction = prepare_extraction(db, rid, scope=scope)
            db.rollback()
            context.authorize()
            if extraction:
                db.execute(text("BEGIN IMMEDIATE"))
                doc = save_extraction(db, rid, scope=scope, extraction=extraction)
                _preserve_existing_aliases(db, candidate["id"], rid, doc)
                db.commit()
            else:
                db.execute(text("BEGIN IMMEDIATE"))
                doc = read_document(db, rid, scope=scope)
                _preserve_existing_aliases(db, candidate["id"], rid, doc)
                db.commit()
            doc = read_document(db, rid, scope=scope)
        job["source_snapshot"].append({**candidate, "library_revision_id": rid, "extraction_fingerprint": doc["fingerprint"]})
        context.save()
    context.authorize()


class JournalLLM:
    def __init__(self, context, client):
        self.context, self.client, self.index = context, client, 0

    def chat(self, messages, **options):
        number = self.index; self.index += 1
        identity = digest([messages, options])
        key = f"research:llm:{number}:{identity}"
        review = "独立的论点支持审查者" in messages[0].get("content", "")
        name = "framework_support_review" if review else "framework_generation"
        job = descriptor(self.context.state)
        job.setdefault("call_names", {})[key] = name
        if key in self.context.state.operations:
            self.context.activity("restoring", "正在恢复已保存的论点复核" if review else "正在恢复已保存的章节草稿")
        else:
            self.context.activity("reviewing" if review else "generating", "正在复核论点与固定证据" if review else "正在组织章节与论点")
        payload = self.context.perform(key, name, {"request_sha256": identity}, lambda: self.client.chat(messages, **options).to_dict())
        return LLMResponse(**payload)


class JournalEmbedder:
    def __init__(self, context, client):
        self.context, self.client, self.index = context, client, 0

    def encode(self, texts):
        number = self.index; self.index += 1
        arrays = []
        for start in range(0, len(texts), 64):
            batch = texts[start:start + 64]; identity = digest(batch)
            key = f"research:embedding:{number}:{start}:{identity}"
            payload = self.context.perform(key, "framework_source_ranking", {"request_sha256": identity, "count": len(batch)},
                lambda: self.client.encode(batch).tolist())
            values = np.asarray(payload, dtype=np.float32)
            if values.ndim != 2 or len(values) != len(batch) or not np.isfinite(values).all():
                raise sources.SourceCorruption("框架文献排序向量回执无效")
            if arrays and values.shape[1] != arrays[0].shape[1]: raise sources.SourceConflict("分批文献排序向量维度不一致")
            arrays.append(values)
        return np.concatenate(arrays) if arrays else np.empty((0, 0), dtype=np.float32)


def execute(context):
    freeze(context)
    state, job = context.state, descriptor(context.state)
    llm = get_llm()
    if isinstance(llm, APIClient):
        llm._authorization_check = context.authorize
    if type(llm).__name__ != "MockLLM": llm = JournalLLM(context, llm)
    if state.runtime_config["mode"] == "mock":
        embedder = LocalEmbedder(job["offline_vector_dim"])
    else:
        embedder = get_embedder()
        if isinstance(embedder, APIEmbedder):
            embedder = JournalEmbedder(context, APIEmbedder(connection=embedding_connection(), synthetic=embedder._synthetic,
                authorization_check=context.authorize))
    papers = job["source_snapshot"]
    def save_draft(payload):
        context.set_payload(payload)
    with SessionLocal() as db:
        result = build_framework(db, job["inputs"]["idea"], llm=llm, embedder=embedder,
            max_sources=job["inputs"]["max_sources"], conversation_id=state.conversation_id,
            paper_snapshot=papers, fixed_revisions={row["id"]: row["library_revision_id"] for row in papers}, on_draft=save_draft)
    complete = bool(result.get("structure_valid") and result.get("semantic_verification") != "incomplete"
        and (state.runtime_config["mode"] == "mock" or result.get("mode") == "llm"))
    job["retry_call_keys"] = [key for key, name in job.get("call_names", {}).items()
        if (name == "framework_support_review" and result.get("semantic_verification") == "incomplete")
        or (name == "framework_generation" and not result.get("structure_valid"))]
    return result, complete and not state.pending_operation


def prepare_resume(state):
    job = descriptor(state)
    for key in job.get("retry_call_keys", []):
        # Unknown calls remain pending until Checkpointer.perform consumes the
        # user's explicit retry. Known invalid responses may be regenerated.
        state.operations.pop(key, None)
    job["retry_call_keys"] = []
