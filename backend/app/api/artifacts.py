"""Versioned editable deliverables and reproducible analysis downloads."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from .. import artifact_store as store
from ..artifact_models import ArtifactManifest, ArtifactRevision, ArtifactChange, ArtifactComputation
from ..db import get_db

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")


class Create(Scope):
    request_id: str = Field(pattern=r"^[\w-]{8,96}$")
    title: str = Field(min_length=1, max_length=240)
    kind: Literal["document", "analysis"] = "document"
    content: str = Field(max_length=store.MAX_CONTENT)
    evidence: list[dict] = Field(default_factory=list, max_length=500)
    analysis: dict = Field(default_factory=dict)


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=store.MAX_CONTENT)


class Edit(Scope):
    request_id: str = Field(pattern=r"^[\w-]{8,96}$")
    base_revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    content: str | None = Field(default=None, max_length=store.MAX_CONTENT)
    section: Section | None = None
    evidence: list[dict] | None = Field(default=None, max_length=500)
    analysis: dict | None = None
    restored_from: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def one_edit(self):
        if sum(x is not None for x in (self.content, self.section, self.restored_from)) != 1:
            raise ValueError("请选择全文、章节修改或恢复一个历史版本")
        return self


class Decision(Scope):
    accept: bool
    expected_revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Publication(Scope):
    request_id: str = Field(pattern=r"^[\w-]{8,96}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace_revision: int = Field(ge=0)
    path: str = Field(min_length=1, max_length=1024)
    expected_file_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ExportCreate(Scope):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,96}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    format: Literal["md", "docx", "xlsx", "bundle", "svg", "manifest"]


def export_sources(rev):
    return [{"id": item.get("id"), "title": item.get("source_title") or item.get("paper_title") or "来源",
             "url": item.get("url", ""), "revision_id": item.get("revision_id", ""),
             "source_id": item.get("source_id", ""), "source_fingerprint": item.get("source_fingerprint", ""),
             "location": item.get("location", {}), "locator_id": item.get("locator_id", ""), "quote": item.get("quote", ""),
             "verification": item.get("verification", "unverified")} for item in rev.evidence]


@router.post("/{artifact_id}/publish-proposal")
def publish(artifact_id: str, req: Publication, db: Session = Depends(get_db)):
    """Freeze a chosen revision into the existing file review/undo journal."""
    from .. import project_changes, workspace
    from ..exporter import build_markdown
    if not req.conversation_id:
        raise HTTPException(422, "请从关联了项目的原会话成果中写入项目")
    row = store.get(db, artifact_id, req.conversation_id, writing=True)
    rev = store.revision(db, row, req.revision_id)
    if row.kind != "document":
        raise HTTPException(422, "数据图表请下载含数据与脚本的完整复算包")
    scope = workspace.capture(db, req.conversation_id, project_id=req.project_id, workspace_revision=req.workspace_revision)
    if not req.path.lower().endswith('.md'):
        raise HTTPException(422, "文档发布保留 Markdown 正文与来源，请使用 .md 后缀")
    markdown = build_markdown(row.title, rev.content, export_sources(rev))
    label = f"研究成果 {row.id} v{rev.version}（版本 {rev.id}）"
    db.rollback()  # No outer transaction may span the journal's independent commit.
    return project_changes.prepare(scope, req.conversation_id,
        {"path": req.path, "operation": "update" if req.expected_file_sha256 else "create",
         "expected_sha256": req.expected_file_sha256, "content": markdown, "summary": label},
        operation_key="artifact-publish:" + req.request_id, exact=True)


@router.get("")
def listing(conversation_id: str | None = None, offset: int = Query(default=0, ge=0),
            limit: int = Query(default=30, ge=1, le=100), db: Session = Depends(get_db)):
    store.scope(db, conversation_id)
    query = db.query(ArtifactManifest).filter_by(conversation_id=conversation_id)
    total = query.count()
    rows = query.order_by(ArtifactManifest.updated_at.desc(), ArtifactManifest.id.desc()).offset(offset).limit(limit).all()
    return {"items": [store.public(row) for row in rows], "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


@router.post("")
def create(req: Create, db: Session = Depends(get_db)):
    return store.create(db, **req.model_dump())


@router.get("/{artifact_id}")
def detail(artifact_id: str, conversation_id: str | None = None, revision_id: str | None = None,
           db: Session = Depends(get_db)):
    row = store.get(db, artifact_id, conversation_id)
    rev = store.revision(db, row, revision_id)
    return {**store.public(row, rev), "sections": store.sections(rev.content) if row.kind == "document" else []}


@router.get("/{artifact_id}/revisions")
def history(artifact_id: str, conversation_id: str | None = None, before_version: int | None = Query(default=None, ge=1, le=store.MAX_VERSION_CURSOR),
            limit: int = Query(default=30, ge=1, le=100), db: Session = Depends(get_db)):
    row = store.get(db, artifact_id, conversation_id)
    return store.list_revisions(db, row, before_version=before_version, limit=limit)


@router.post("/{artifact_id}/changes")
def propose(artifact_id: str, req: Edit, db: Session = Depends(get_db)):
    row = store.get(db, artifact_id, req.conversation_id, writing=True)
    return store.propose(db, row, **req.model_dump(exclude={"conversation_id"}))


@router.get("/{artifact_id}/changes")
def changes(artifact_id: str, conversation_id: str | None = None, offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    row = store.get(db, artifact_id, conversation_id)
    query = db.query(ArtifactChange).filter_by(artifact_id=row.id, status="proposed").order_by(ArtifactChange.created_at.desc())
    rows = query.offset(offset).limit(30).all()
    return {"items": [store.change_public(db, row, item) for item in rows], "total": query.count()}


def _change(db, change_id):
    value = db.get(ArtifactChange, change_id)
    if value is None:
        raise HTTPException(404, "成果修改不存在")
    return value


@router.get("/{artifact_id}/changes/{change_id}")
def change(artifact_id: str, change_id: str, conversation_id: str | None = None,
           offset: int = Query(default=0, ge=0), limit: int = Query(default=400, ge=1, le=2000), db: Session = Depends(get_db)):
    row = store.get(db, artifact_id, conversation_id)
    return store.change_public(db, row, _change(db, change_id), offset=offset, limit=limit)


@router.post("/{artifact_id}/changes/{change_id}/decision")
def decide(artifact_id: str, change_id: str, req: Decision, db: Session = Depends(get_db)):
    row = store.get(db, artifact_id, req.conversation_id, writing=True)
    return store.decide(db, row, _change(db, change_id), accept=req.accept, expected_revision_id=req.expected_revision_id)


@router.post("/{artifact_id}/recompute")
def recompute(artifact_id: str, req: Scope, revision_id: str | None = None, db: Session = Depends(get_db)):
    from ..artifact_compute import recompute
    row = store.get(db, artifact_id, req.conversation_id, writing=True)
    return recompute(db, row, store.revision(db, row, revision_id))


@router.get("/{artifact_id}/computations")
def computations(artifact_id: str, conversation_id: str | None = None, revision_id: str | None = None, db: Session = Depends(get_db)):
    row = store.get(db, artifact_id, conversation_id)
    rev = store.revision(db, row, revision_id)
    rows = db.query(ArtifactComputation).filter_by(artifact_id=row.id, revision_id=rev.id).order_by(ArtifactComputation.created_at.desc()).limit(30).all()
    return {"items": [{"id": item.id, "status": item.status, "result": item.result, "error": item.error,
                       "script_sha256": item.script_sha256, "created_at": item.created_at.isoformat()} for item in rows]}


@router.get("/{artifact_id}/download")
def download(artifact_id: str, format: Literal["md", "docx", "bundle", "svg", "manifest"] = "md",
             conversation_id: str | None = None, revision_id: str | None = None, db: Session = Depends(get_db)):
    from ..artifact_compute import bundle, execute
    from ..exporter import build_docx, build_markdown
    row = store.get(db, artifact_id, conversation_id)
    rev = store.revision(db, row, revision_id)
    papers = export_sources(rev)
    if format == "bundle":
        content, media, ext = bundle(db, row, rev), "application/zip", "zip"
    elif format == "manifest":
        content, media, ext = store.canonical(store.public(row, rev)), "application/json", "json"
    elif format == "svg":
        if row.kind != "analysis":
            raise HTTPException(422, "此成果不包含数据图表")
        content, media, ext = execute(rev.content, rev.analysis)["svg"], "image/svg+xml", "svg"
    elif format == "docx":
        from .. import artifact_export_formats
        content, generation = artifact_export_formats.render(db, row, rev, row.title, 'docx')
        artifact_export_formats.inspect(db, row, rev, row.title, 'docx', content, generation)
        media, ext = artifact_export_formats.FORMATS['docx']
    else:
        content, media, ext = build_markdown(row.title, rev.content, papers), "text/markdown; charset=utf-8", "md"
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return Response(payload, media_type=media, headers={"Content-Disposition": f'attachment; filename="ScholarFlow-{row.id}-v{rev.version}.{ext}"',
        "X-Content-SHA256": store.digest(payload), "X-Source-Content-SHA256": rev.content_sha256,
        "X-Artifact-Revision": rev.id, "X-Content-Type-Options": "nosniff"})


@router.post("/{artifact_id}/exports")
def create_export(artifact_id: str, req: ExportCreate, db: Session = Depends(get_db)):
    from .. import artifact_exports
    return artifact_exports.create(db, artifact_id=artifact_id, **req.model_dump())


@router.get("/{artifact_id}/exports")
def list_exports(artifact_id: str, conversation_id: str | None = None, revision_id: str | None = None,
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100),
                 db: Session = Depends(get_db)):
    from .. import artifact_exports
    return artifact_exports.listing(db, artifact_id=artifact_id, conversation_id=conversation_id,
        revision_id=revision_id, offset=offset, limit=limit)


@router.get("/{artifact_id}/exports/{export_id}")
def export_detail(artifact_id: str, export_id: str, conversation_id: str | None = None, db: Session = Depends(get_db)):
    from .. import artifact_exports
    return artifact_exports.public(artifact_exports.get(db, artifact_id, export_id, conversation_id))


@router.get("/{artifact_id}/exports/{export_id}/download")
def export_download(artifact_id: str, export_id: str, conversation_id: str | None = None, db: Session = Depends(get_db)):
    from .. import artifact_exports
    row = artifact_exports.get(db, artifact_id, export_id, conversation_id)
    return Response(row.payload, media_type=row.media_type, headers={
        "Content-Disposition": f'attachment; filename="{row.filename}"', "Content-Length": str(row.payload_size),
        "X-Export-ID": row.id, "X-Export-SHA256": row.payload_sha256, "X-Content-SHA256": row.payload_sha256,
        "X-Artifact-Revision": row.revision_id, "X-Source-Content-SHA256": row.content_sha256,
        "ETag": f'"{row.payload_sha256}"', "X-Content-Type-Options": "nosniff"})
