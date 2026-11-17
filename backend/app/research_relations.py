"""One revision stream for graph edges and their evidence/review panels."""
from copy import deepcopy
import hashlib
import json
from sqlalchemy import select
from .research_models import RelationEvidence, RelationRevision
from .timeutil import utcnow
from . import source_store as sources
from .models import Paper


def relation_output(item):
    return {"id": item.id, "version": item.version, "source_paper_id": item.source_paper_id,
        "extracted": item.extracted, "effective": item.effective, "manual": bool(item.manual)}


def record_revision(db, item, action, actor, reason=""):
    db.add(RelationRevision(relation_id=item.id, version=item.version, action=action,
        actor=actor, reason=reason, document=deepcopy(relation_output(item))))


def relation_id(ref, paper):
    # Parser database row IDs may change during re-extraction. Identity follows
    # the original bytes and normalized reference, never a fuzzy target match.
    identity = [paper.id, paper.content_hash, " ".join((ref.raw_text or ref.title or "").split())]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def sync_relation(db, ref, paper, source_group, target_id, match_type):
    from .citation_contexts import relation_contexts
    rid = relation_id(ref, paper)
    exact = match_type in ("doi", "arxiv_id")
    extracted = {"source": source_group, "target": target_id, "relation_type": "citation" if exact else "uncertain",
        "status": "unverified", "reference_entry": ref.raw_text or ref.title or "",
        "reference_title": ref.title or "", "reference_index": ref.ref_index,
        "match_type": match_type, "matching_basis": "标识符匹配" if exact else "参考条目标题词匹配，尚未人工确认",
        "source_sha256": paper.content_hash, "reference_page": ref.page, "reference_bbox": ref.bbox,
        "location_status": "reference_page" if ref.page and ref.page > 0 else "unavailable",
        **relation_contexts(db, paper, ref)}
    if extracted["citation_contexts"]:
        extracted["location_status"] = "citation_context"
    item = db.get(RelationEvidence, rid)
    if item is None:
        item = RelationEvidence(id=rid, source_paper_id=paper.id, extracted=extracted,
            effective=deepcopy(extracted), version=1, manual=0)
        db.add(item); db.flush(); record_revision(db, item, "extract", "reference_parser")
    elif item.extracted != extracted:
        item.extracted, item.version, item.updated_at = extracted, item.version + 1, utcnow()
        if not item.manual:
            item.effective = deepcopy(extracted)
        db.flush(); record_revision(db, item, "reextract", "reference_parser")
    return item


def get_relation(db, rid):
    item = db.get(RelationEvidence, rid, populate_existing=True)
    if not item or not db.get(Paper, item.source_paper_id):
        raise sources.SourceNotFound("关系或所属文献不存在")
    return item


def review_relation(db, item, expected, action, reason, target=None, relation_type=None, restore_version=None):
    if item.version != expected:
        raise sources.SourceConflict("关系已更新，请重新打开证据后修改")
    if action == "restore":
        old = db.scalar(select(RelationRevision).where(RelationRevision.relation_id == item.id,
            RelationRevision.version == restore_version))
        if not old:
            raise sources.SourceNotFound("待恢复的修订不存在")
        effective, manual = deepcopy(old.document["effective"]), int(old.document["manual"])
    elif action == "reset":
        effective, manual = deepcopy(item.extracted), 0
    else:
        effective, manual = deepcopy(item.effective), 1
        if target:
            paper = db.get(Paper, target)
            if not paper or paper.status != "parsed" or target == item.source_paper_id:
                raise sources.SourceError("请选择已解析的其他目标文献")
            effective["target"] = target
        if relation_type:
            effective["relation_type"] = relation_type
        effective["status"] = "excluded" if action == "exclude" else "human_confirmed"
    item.effective, item.manual, item.version, item.updated_at = effective, manual, item.version + 1, utcnow()
    db.flush()
    record_revision(db, item, action, "local_user", reason)
    return item
