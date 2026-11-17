"""Local citation-discussion signals over fixed originals, never scientific validation."""
from copy import deepcopy
import re
from types import SimpleNamespace

from sqlalchemy import select, text

from .. import source_store as sources
from ..algorithms.citation_quality import score_occurrence
from ..citation_contexts import CITATION_CONTEXT_VERSION, extract_contexts, _passage_index
from ..db import SessionLocal
from ..library_sources import ensure_owned, copy_record
from ..models import Paper, Reference
from ..research_runs import descriptor, digest
from ..source_documents import prepare_extraction, save_extraction, read_document
from ..source_runtime import conversation_scope, validate_source_evidence

ALGORITHM = "citation-discussion-signals-v1"


def validate_inputs(value):
    if not isinstance(value, dict) or set(value) != {"paper_id"} or not isinstance(value["paper_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["paper_id"]):
        raise sources.SourceError("请选择有效的文献进行引用讨论分析")
    return {"paper_id": value["paper_id"]}


def goal_description(inputs):
    return "分析固定原文中的正文引用讨论信号并保存可追溯位置；不判定科学结论或引用目标正确。"


def capture_config(db, conversation_id):
    # A deterministic local operation must work without a configured cloud key
    # and must not inherit a paid model's identity or claim it was used.
    from ..model_profiles import options_snapshot
    options = options_snapshot(db, conversation_id)
    return {"version": 1, "mode": "local", "model_id": ALGORITHM, "profile_name": "本地引用讨论信号",
        "profile_id": None, "credential_ref": None, "approval_mode": options["approval_mode"],
        "policy_revision": options["revision"]}


def validate_binding(db, binding):
    if binding.get("mode") != "local" or binding.get("model_id") != ALGORITHM:
        raise sources.SourceConflict("引用讨论算法版本已变化，请保留旧结果并新建分析")


def snapshot_config(binding):
    return {"quality_algorithm": ALGORITHM, "citation_context_version": CITATION_CONTEXT_VERSION}


def initialize(db, state):
    job = descriptor(state)
    paper = db.get(Paper, job["inputs"]["paper_id"])
    if paper is None or paper.status != "parsed":
        raise sources.SourceNotFound("请选择已解析且保留原件的文献")
    original, _ = ensure_owned(db, paper.id)
    alias = copy_record(db, original, sources.SourceScope("library", paper.id),
        conversation_scope(db, state.conversation_id), "quality:" + state.attempt_id + ":" + original["revision_id"])
    refs = [{"ref_index": r.ref_index, "raw_text": r.raw_text or ""} for r in db.scalars(
        select(Reference).where(Reference.paper_id == paper.id).order_by(Reference.ref_index, Reference.id))]
    if len(refs) > 5000 or len({r["ref_index"] for r in refs}) != len(refs) or any(type(r["ref_index"]) is not int or not 1 <= r["ref_index"] <= 99999 for r in refs):
        raise sources.SourceConflict("参考文献编号重复、无效或超过5000条，请先核对文献解析")
    job["source_snapshot"] = [{"paper_id": paper.id, "title": paper.title,
        "revision_id": alias["revision_id"], "raw_sha256": original["raw_sha256"], "references": refs}]
    job["source_snapshot_sha256"] = digest(job["source_snapshot"])
    job["quality_results"] = []
    job["quality_results_sha256"] = digest([])


def _snapshot(job):
    rows = job.get("source_snapshot")
    if not isinstance(rows, list) or len(rows) != 1 or digest(rows) != job.get("source_snapshot_sha256"):
        raise sources.SourceCorruption("引用分析固定原件与参考条目快照校验失败")
    results = job.get("quality_results")
    if not isinstance(results, list) or digest(results) != job.get("quality_results_sha256"):
        raise sources.SourceCorruption("引用分析已保存进度校验失败")
    if [r.get("ref_index") for r in results] != [r["ref_index"] for r in rows[0]["references"][:len(results)]]:
        raise sources.SourceCorruption("引用分析进度不属于固定参考条目")
    return rows[0]


def authorize(db, state):
    job = descriptor(state)
    if job.get("quality_algorithm") != ALGORITHM or job.get("citation_context_version") != CITATION_CONTEXT_VERSION:
        raise sources.SourceConflict("引用分析算法版本已变化，请保留旧结果并新建分析")
    _authorize_source(db, state)


def _authorize_source(db, state):
    job = descriptor(state)
    snapshot = _snapshot(job)
    scope = conversation_scope(db, state.conversation_id, writing=False)
    record = sources.read_revision(db, snapshot["revision_id"], scope=scope)
    if record["raw_sha256"] != snapshot["raw_sha256"]:
        raise sources.SourceCorruption("引用分析固定原件指纹不一致")
    if job.get("extraction_fingerprint"):
        document = read_document(db, record["revision_id"], scope=scope)
        if document["fingerprint"] != job["extraction_fingerprint"]:
            raise sources.SourceCorruption("引用分析固定解析版本校验失败")


def public_authorize(db, state):
    _authorize_source(db, state)
    job = descriptor(state)
    if job.get("quality_algorithm") != ALGORITHM or job.get("citation_context_version") != CITATION_CONTEXT_VERSION:
        job["resume_blocked_reason"] = "分析算法已升级，旧结果保留；请新建分析。"
    evidence = [e for r in descriptor(state)["quality_results"] for e in r["evidence"]]
    validate_source_evidence(db, state.conversation_id, evidence)


def _payload(state):
    job = descriptor(state)
    snapshot = _snapshot(job)
    results = deepcopy(job["quality_results"])
    summary = {key: sum(r["label"] == key for r in results) for key in ("substantive", "neutral", "passing", "not_located")}
    return {"paper_id": snapshot["paper_id"], "title": snapshot["title"], "revision_id": snapshot["revision_id"],
        "raw_sha256": snapshot["raw_sha256"], "algorithm": ALGORITHM, "mode": "local_heuristic",
        "structure_valid": True, "source_acquired": True, "semantic_support": "unverified", "verified": False,
        "results": results, "summary": summary, "progress": {"completed": len(results), "total": len(snapshot["references"])},
        "supported_styles": ["numeric_square_brackets", "latin_surname_year"],
        "note": "本地规则只分析已定位原句的讨论信号，不证明引用对象、论点或科学结论正确。每条最多保留8处；未定位不等于未引用，暂未覆盖上标及全部引用样式。"}


def _locator(db, rid, document, context):
    location = deepcopy(context["location"])
    kind = location.pop("kind")
    location.update(raw_sha256=document["raw_sha256"], text_sha256=document["text_sha256"],
        extraction_fingerprint=document["fingerprint"])
    if kind == "pdf_page": location["page_index_base"] = 0
    elif kind == "text_lines": location["line_index_base"] = 1
    elif kind == "word_paragraphs": location["paragraph_index_base"] = 1
    elif kind == "word_block": location["rendered_page_available"] = False
    else: location.update(index_base=1, formula_values_recalculated=False)
    # Context and geometry come from read_document's verified immutable
    # extraction, not a request body or mutable Chunk/BBox metadata.
    return sources._save_locator(db, rid, kind, location, context["quote"])


def execute(context):
    state, job = context.state, descriptor(context.state)
    snapshot = _snapshot(job)
    rid = snapshot["revision_id"]
    scope = sources.SourceScope("conversation", state.conversation_id)
    context.authorize()
    with SessionLocal() as db:
        prepared = prepare_extraction(db, rid, scope=scope)
        db.rollback()
        context.authorize()
        if prepared:
            db.execute(text("BEGIN IMMEDIATE"))
            save_extraction(db, rid, scope=scope, extraction=prepared)
            db.commit()
        document = read_document(db, rid, scope=scope)
    fingerprint = document["fingerprint"]
    if job.get("extraction_fingerprint") not in (None, fingerprint):
        raise sources.SourceCorruption("引用分析解析版本不能被新版本替换")
    job["extraction_fingerprint"] = fingerprint
    context.set_payload(_payload(state))
    index = _passage_index(document)
    original_text = " ".join(" ".join(b.get("text", "") for b in document["blocks"]).split())
    completed = len(job["quality_results"])
    for reference in snapshot["references"][completed:]:
        context.authorize()
        match = extract_contexts(document, SimpleNamespace(**reference), _index=index)
        scores = [score_occurrence(c["quote"]) for c in match["contexts"]]
        score = max((s["score"] for s in scores), default=0)
        label = "not_located" if not scores else "substantive" if score >= 2 else "passing" if score <= 0 else "neutral"
        evidence = []
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            for number, passage in enumerate(match["contexts"], 1):
                saved = _locator(db, rid, document, passage)
                evidence.append({"id": f"Q{reference['ref_index']}E{number}", "paper_id": snapshot["paper_id"],
                    "source_id": document["source_id"], "revision_id": rid, "locator_id": saved["locator_id"],
                    "quote": passage["quote"], "marker": passage["marker"], "match_method": passage["match_method"],
                    "raw_sha256": snapshot["raw_sha256"], "source_sha256": snapshot["raw_sha256"],
                    "location": saved["location"], "structure_valid": True, "source_acquired": True,
                    "semantic_support": "unverified", "verification_status": "unverified",
                    "verification": "source_frozen_semantics_unverified"})
            db.commit()
        context.authorize()
        job["quality_results"].append({**reference, "label": label, "score": score,
            "located_contexts": len(evidence), "sample_limited": len(evidence) >= 8,
            "reference_entry_in_source": bool(reference["raw_text"].strip() and " ".join(reference["raw_text"].split()) in original_text),
            "signals": sorted({signal for s in scores for signal in s["signals"]}), "evidence": evidence,
            "structure_valid": True, "source_acquired": True, "semantic_support": "unverified"})
        job["quality_results_sha256"] = digest(job["quality_results"])
        context.activity("analyzing", f"已分析 {len(job['quality_results'])}/{len(snapshot['references'])} 条参考条目，原句位置已保存")
        context.set_payload(_payload(state))
    return _payload(state), True


def prepare_resume(state):
    _snapshot(descriptor(state))
