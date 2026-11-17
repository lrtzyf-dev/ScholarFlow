"""Reviewable research revisions with CAS, stable sources and idempotent decisions.

This is the document layer, not another filesystem writer. Publishing to a
project deliberately uses the existing ProjectChange journal and approvals.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from .artifact_models import ArtifactManifest, ArtifactRevision, ArtifactChange
from .models import Conversation, DeletedConversation
from .source_store import SourceScope, SourceError, read_revision, read_locator
from .timeutil import utcnow

MAX_CONTENT = 1024 * 1024
MAX_VERSION_CURSOR = (1 << 63) - 1


def digest(value):
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def scope(db, conversation_id, *, writing=False):
    if conversation_id is None:
        return "local", None
    if db.get(DeletedConversation, conversation_id):
        raise HTTPException(410, "会话已删除")
    row = db.get(Conversation, conversation_id)
    if row is None:
        raise HTTPException(404, "会话不存在")
    if writing and row.archived_at is not None:
        raise HTTPException(409, "请先恢复已归档会话")
    return "conversation:" + conversation_id, row.project_id


def get(db, artifact_id, conversation_id=None, *, writing=False):
    scope(db, conversation_id, writing=writing)
    row = db.get(ArtifactManifest, artifact_id)
    if row is None or row.conversation_id != conversation_id:
        raise HTTPException(404, "成果不存在或不属于当前会话")
    return row


def revision(db, row, revision_id=None):
    result = db.get(ArtifactRevision, revision_id or row.current_revision_id)
    if result is None or result.artifact_id != row.id:
        raise HTTPException(404, "成果版本不存在")
    expected = digest(canonical({"content_sha256": digest(result.content),
                                "evidence": result.evidence, "analysis": result.analysis}))
    if digest(result.content) != result.content_sha256 or expected != result.manifest_sha256:
        raise HTTPException(409, "成果版本完整性校验失败，请从备份恢复")
    return result


def list_revisions(db, row, *, before_version=None, limit=30):
    """Bounded saved fixed revisions only; never drafts or external evidence."""
    scope(db, row.conversation_id)
    if ((before_version is not None and (type(before_version) is not int or not 1 <= before_version <= MAX_VERSION_CURSOR))
            or type(limit) is not int or not 1 <= limit <= 100):
        raise HTTPException(422, "版本分页须为正整数，单次最多100条")
    current = revision(db, row)
    if current.version != row.version:
        raise HTTPException(409, "成果当前版本记录不一致，请从备份恢复")
    query = db.query(ArtifactRevision).filter_by(artifact_id=row.id)
    if before_version is not None:
        query = query.filter(ArtifactRevision.version < before_version)
    rows = query.order_by(ArtifactRevision.version.desc()).limit(limit + 1).all()
    items = []
    for candidate in rows[:limit]:
        rev = revision(db, row, candidate.id)
        items.append({key: getattr(rev, key) for key in ("id", "version", "parent_revision_id", "restored_from",
            "content_sha256", "manifest_sha256")} | {"created_at": rev.created_at.isoformat(),
            "is_current": rev.id == row.current_revision_id})
    return {"artifact_id": row.id, "current_revision_id": row.current_revision_id,
        "current_version": row.version, "items": items,
        "next_before_version": rows[limit - 1].version if len(rows) > limit else None,
        "saved_fixed_revisions_only": True, "includes_pending_drafts": False,
        "trust": "research_draft_history_not_external_evidence"}


def _content(text):
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise HTTPException(422, "成果包含无效字符") from None
    if size > MAX_CONTENT or "\x00" in text:
        raise HTTPException(422, "成果须为不超过 1 MiB 的文本")
    return text


def freeze_evidence(db, evidence, conversation_id):
    """Never upgrade a model quote to verified evidence; freeze server locators."""
    if len(evidence) > 500:
        raise HTTPException(422, "单个成果最多保留 500 条来源")
    result, evidence_ids = [], set()
    for item in evidence:
        if not isinstance(item, dict):
            raise HTTPException(422, "来源格式不正确")
        identifier = item.get("id")
        if identifier is not None:
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", identifier) or identifier in evidence_ids:
                raise HTTPException(422, "引用编号须有效且在此成果中唯一")
            evidence_ids.add(identifier)
        rid, lid = item.get("revision_id"), item.get("locator_id")
        if rid:
            if not conversation_id:
                raise HTTPException(422, "会话来源须在原会话内保存")
            try:
                source = read_revision(db, rid, scope=SourceScope("conversation", conversation_id))
                fixed = {"revision_id": rid, "source_id": source["source_id"],
                         "source_title": source["title"], "source_fingerprint": source["snapshot_fingerprint"],
                         "verification": "source_frozen_semantics_unverified"}
                if lid:
                    locator = read_locator(db, lid, scope=SourceScope("conversation", conversation_id))
                    if locator["revision_id"] != rid:
                        raise HTTPException(422, "引用位置与来源版本不一致")
                    fixed.update(locator_id=lid, quote=locator["quote"], location=locator["location"])
                if item.get("id"):
                    fixed["id"] = str(item["id"])[:100]
                result.append(fixed)
            except SourceError:
                raise HTTPException(404, "来源版本或定位不可访问") from None
        else:
            # Legacy paper references are retained explicitly as unversioned,
            # never presented as a frozen source or certified citation.
            for key in ("paper_id", "paper_title", "quote", "url"):
                if key in item and not isinstance(item[key], str):
                    raise HTTPException(422, "旧版引用的编号、标题、原句和网址须为文本")
            if "page" in item and (type(item["page"]) is not int or item["page"] < 1):
                raise HTTPException(422, "引用页码须为正整数")
            result.append({k: item[k] for k in ("id", "paper_id", "paper_title", "page", "quote", "url") if k in item}
                          | {"verification": "legacy_unversioned_unverified"})
    if len(canonical(result).encode("utf-8")) > MAX_CONTENT:
        raise HTTPException(422, "来源清单过大")
    return result


def _new_revision(row, content, evidence, analysis, *, parent=None, restored_from=None):
    content_hash = digest(content)
    return ArtifactRevision(id=uuid4().hex, artifact_id=row.id, version=row.version,
        parent_revision_id=parent, restored_from=restored_from, content=content,
        content_sha256=content_hash, evidence=evidence, analysis=analysis,
        manifest_sha256=digest(canonical({"content_sha256": content_hash, "evidence": evidence, "analysis": analysis})))


def public(row, rev=None):
    value = {key: getattr(row, key) for key in ("id", "conversation_id", "project_id", "title", "kind", "version", "current_revision_id")}
    value.update(created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())
    if rev is not None:
        value["revision"] = {key: getattr(rev, key) for key in (
            "id", "version", "parent_revision_id", "restored_from", "content", "content_sha256", "manifest_sha256", "evidence", "analysis")}
        value["revision"]["created_at"] = rev.created_at.isoformat()
    return value


def _existing(db, scope_key, request_id, request_hash):
    found = db.query(ArtifactChange).filter_by(scope_key=scope_key, request_id=request_id).first()
    if found and found.request_sha256 != request_hash:
        raise HTTPException(409, "同一请求 ID 已使用不同内容，请保留草稿并新建请求")
    return found


def create(db, *, request_id, title, kind, content, evidence, analysis, conversation_id=None):
    key, project_id = scope(db, conversation_id, writing=True)
    title = title.strip()
    if not title or len(title) > 240:
        raise HTTPException(422, "成果标题须为 1–240 字")
    if kind not in ("document", "analysis"):
        raise HTTPException(422, "成果类型无效")
    _content(content)
    raw_hash = digest(canonical({"title": title, "kind": kind, "content": content, "evidence": evidence, "analysis": analysis}))
    found = _existing(db, key, request_id, raw_hash)
    if found:
        row = get(db, found.artifact_id, conversation_id)
        return public(row, revision(db, row, found.result_revision_id))
    fixed = freeze_evidence(db, evidence, conversation_id)
    from .artifact_compute import validate_analysis
    analysis = validate_analysis(content, analysis) if kind == "analysis" else {}
    row = ArtifactManifest(id=uuid4().hex, conversation_id=conversation_id, project_id=project_id,
        title=title, kind=kind, version=1, current_revision_id="")
    rev = _new_revision(row, content, fixed, analysis)
    row.current_revision_id = rev.id
    db.add_all([row, rev, ArtifactChange(artifact_id=row.id, scope_key=key, request_id=request_id,
        request_sha256=raw_hash, content=content, evidence=fixed, analysis=analysis,
        status="accepted", result_revision_id=rev.id, decided_at=utcnow())])
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        found = _existing(db, key, request_id, raw_hash)
        if not found:
            raise
        row = get(db, found.artifact_id, conversation_id)
        rev = revision(db, row, found.result_revision_id)
    return public(row, rev)


def sections(content):
    """Heading offsets outside fenced code; ranges include their subsections."""
    items, offset, fence = [], 0, None
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
        elif fence is None:
            match = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*\r?\n?$", line)
            if match:
                items.append({"index": len(items), "title": match[2], "level": len(match[1]), "start": offset})
        offset += len(line)
    for i, item in enumerate(items):
        item["end"] = next((x["start"] for x in items[i + 1:] if x["level"] <= item["level"]), len(content))
        item["sha256"] = digest(content[item["start"]:item["end"]])
    return items


def propose(db, row, *, request_id, base_revision_id, content=None, section=None, evidence=None, analysis=None, restored_from=None):
    scope_key, _ = scope(db, row.conversation_id, writing=True)
    raw_hash = digest(canonical({"artifact_id": row.id, "base": base_revision_id, "content": content,
        "section": section, "evidence": evidence, "analysis": analysis, "restored_from": restored_from}))
    found = _existing(db, scope_key, request_id, raw_hash)
    if found:
        return change_public(db, row, found)
    base = revision(db, row, base_revision_id)
    if restored_from:
        if content is not None or section is not None or evidence is not None or analysis is not None:
            raise HTTPException(422, "恢复版本不能混入其他编辑")
        original = revision(db, row, restored_from)
        content, evidence, analysis = original.content, original.evidence, original.analysis
    elif section is not None:
        if content is not None or row.kind != "document":
            raise HTTPException(422, "局部修改须指定一个文档章节")
        candidates = sections(base.content)
        index = section.get("index")
        if type(index) is not int or not 0 <= index < len(candidates):
            raise HTTPException(422, "章节不存在")
        selected = candidates[index]
        if section.get("expected_sha256") != selected["sha256"]:
            raise HTTPException(409, "章节内容已变化，请重新审阅")
        replacement = _content(section.get("content", ""))
        content = base.content[:selected["start"]] + replacement + base.content[selected["end"]:]
    if content is None:
        raise HTTPException(422, "请提供修改内容")
    _content(content)
    fixed = base.evidence if evidence is None or restored_from else freeze_evidence(db, evidence, row.conversation_id)
    if restored_from:
        fixed = evidence
    from .artifact_compute import validate_analysis
    config = base.analysis if analysis is None else analysis
    config = validate_analysis(content, config) if row.kind == "analysis" else {}
    change = ArtifactChange(id=uuid4().hex, artifact_id=row.id, scope_key=scope_key, request_id=request_id,
        request_sha256=raw_hash, base_revision_id=base.id, content=content, evidence=fixed,
        analysis=config, restored_from=restored_from)
    db.add(change)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        change = _existing(db, scope_key, request_id, raw_hash)
        if change is None:
            raise
    return change_public(db, row, change)


def change_public(db, row, change, *, offset=0, limit=400):
    if change.artifact_id != row.id:
        raise HTTPException(404, "修改草稿不存在")
    base = revision(db, row, change.base_revision_id) if change.base_revision_id else None
    lines = list(difflib.unified_diff((base.content if base else "").splitlines(keepends=True),
        change.content.splitlines(keepends=True), fromfile="已保存版本", tofile="待审阅修改"))
    return {"id": change.id, "artifact_id": row.id, "base_revision_id": change.base_revision_id,
        "restored_from": change.restored_from,
        "status": change.status, "content": change.content, "evidence": change.evidence,
        "analysis": change.analysis, "result_revision_id": change.result_revision_id,
        "conflict": change.base_revision_id != row.current_revision_id and change.status == "proposed",
        "diff": "".join(lines[offset:offset + limit]), "diff_total_lines": len(lines),
        "diff_offset": offset, "diff_has_more": len(lines) > offset + limit,
        "diff_next_offset": offset + limit if len(lines) > offset + limit else None}


def decide(db, row, change, *, accept, expected_revision_id):
    scope(db, row.conversation_id, writing=True)
    if change.artifact_id != row.id:
        raise HTTPException(404, "修改草稿不存在")
    desired = "accepted" if accept else "rejected"
    if change.status != "proposed":
        if change.status != desired:
            raise HTTPException(409, "该修改已作出不同决定")
        return change_public(db, row, change)
    if not accept:
        changed = db.execute(update(ArtifactChange).where(ArtifactChange.id == change.id,
            ArtifactChange.status == "proposed").values(status="rejected", decided_at=utcnow())).rowcount
        if changed != 1:
            db.rollback()
            raise HTTPException(409, "草稿已被其他窗口处理，请刷新")
        db.commit()
        db.refresh(change)
        return change_public(db, row, change)
    if expected_revision_id != change.base_revision_id or row.current_revision_id != expected_revision_id:
        raise HTTPException(409, "成果已更新，双方版本和草稿均已保留；请比较后重新提交")
    # Compare-and-swap is the writer fence across processes and sessions.
    new_version = row.version + 1
    new_id = uuid4().hex
    changed = db.execute(update(ArtifactManifest).where(ArtifactManifest.id == row.id,
        ArtifactManifest.current_revision_id == expected_revision_id, ArtifactManifest.version == row.version)
        .values(current_revision_id=new_id, version=new_version, updated_at=utcnow())
        .execution_options(synchronize_session=False)).rowcount
    decided = db.execute(update(ArtifactChange).where(ArtifactChange.id == change.id,
        ArtifactChange.status == "proposed").values(status="accepted", result_revision_id=new_id, decided_at=utcnow())
        .execution_options(synchronize_session=False)).rowcount
    if changed != 1 or decided != 1:
        db.rollback()
        raise HTTPException(409, "成果或草稿已变化，未覆盖原内容")
    db.refresh(row)
    rev = _new_revision(row, change.content, change.evidence, change.analysis,
        parent=expected_revision_id, restored_from=change.restored_from)
    rev.id = new_id
    db.add(rev)
    db.commit()
    db.refresh(change)
    return change_public(db, row, change)
