"""User-facing matrices, reading state, annotations and relation revisions."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from typing import Literal
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from ..db import get_db
from .. import research_store as store, source_store as sources
from ..research_models import ResearchMatrix, MatrixRevision, ReadingState, ReadingAnnotation, AnnotationRevision
from ..timeutil import utcnow
from .sources import _scope, _source_errors

router = APIRouter(prefix="/api/research", tags=["research"])


class Scoped(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[\w-]{1,64}$")


@router.post("/library/{paper_id}/open")
def open_fixed_library(paper_id: str, req: Scoped, db: Session = Depends(get_db)):
    from ..models import Paper
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        _scope(db, req.conversation_id, writing=True)
        paper = db.get(Paper, paper_id)
        if not paper:
            raise sources.SourceNotFound("文献不存在")
        result = store.pin_paper(db, paper, req.conversation_id)
        db.commit()
        return {**result, "conversation_id": req.conversation_id}


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=1000)


class MatrixInput(Scoped):
    request_id: str | None = Field(default=None, min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    paper_ids: list[str] = Field(min_length=2, max_length=200)
    fields: list[FieldSpec] = Field(min_length=1, max_length=40)
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def unique(self):
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("字段编号不能重复")
        return self


class ExtractInput(Scoped):
    expected_version: int = Field(ge=1)
    field_keys: list[str] | None = Field(default=None, min_length=1, max_length=40)
    paper_ids: list[str] | None = Field(default=None, min_length=1, max_length=200)


class CellInput(Scoped):
    expected_version: int = Field(ge=1)
    field_key: str
    paper_id: str
    value: str = Field(max_length=10000)
    status: Literal["supported", "conflict", "not_found", "not_processed", "unverified"]
    note: str = Field(default="", max_length=10000)
    use_candidate: bool = Field(default=False, strict=True)
    candidate_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def candidate_intent(self):
        if self.use_candidate != (self.candidate_sha256 is not None):
            raise ValueError("采用候选需同时指定已查看的候选摘要")
        return self


@router.get("/matrices")
def list_matrices(conversation_id: str, db: Session = Depends(get_db)):
    with _source_errors(db):
        _scope(db, conversation_id)
        return {"items": [{k: v for k, v in store.matrix_output(m).items() if k != "document"}
            for m in db.scalars(select(ResearchMatrix).where(ResearchMatrix.conversation_id == conversation_id)
                .order_by(ResearchMatrix.updated_at.desc()))]}


@router.post("/matrices")
def create_matrix(req: MatrixInput, db: Session = Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        _scope(db, req.conversation_id, writing=True)
        item=store.create_matrix(db,req.conversation_id,req.title,req.paper_ids,[f.model_dump() for f in req.fields],req.request_id)
        db.commit()
        return store.matrix_output(item)


class ImportInput(Scoped):
    package: dict


@router.post("/matrices/import")
def import_matrix(req: ImportInput, db: Session = Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        _scope(db, req.conversation_id, writing=True)
        try:
            document = store.validate_import(db, req.package, req.conversation_id)
        except (KeyError, TypeError, AttributeError):
            raise sources.SourceError("矩阵文件结构损坏，未导入") from None
        item = ResearchMatrix(conversation_id=req.conversation_id, title=str(req.package.get("title") or "导入的矩阵")[:200], document=document)
        db.add(item)
        db.flush()
        db.add(MatrixRevision(matrix_id=item.id, version=1, action="import", document={"title": item.title, "data": document}))
        db.commit()
        return store.matrix_output(item)


@router.get("/matrices/{matrix_id}")
def get_matrix(matrix_id: str, conversation_id: str, db: Session = Depends(get_db)):
    with _source_errors(db):
        return store.matrix_output(store.matrix(db, matrix_id, conversation_id))


@router.put("/matrices/{matrix_id}")
def update_matrix(matrix_id: str, req: MatrixInput, db: Session = Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        item = store.matrix(db, matrix_id, req.conversation_id, writing=True)
        if req.expected_version is None or item.version != req.expected_version:
            raise sources.SourceConflict("请提供当前矩阵版本后修改")
        document = store.matrix_document(db, req.conversation_id, req.paper_ids,
            [f.model_dump() for f in req.fields], previous=item.document)
        item = store.save_matrix(db, item, document, req.expected_version, "schema", title=req.title)
        db.commit()
        return store.matrix_output(item)


@router.post("/matrices/{matrix_id}/extract")
def extract_matrix(matrix_id: str, req: ExtractInput, db: Session = Depends(get_db)):
    # Kept only to explain migration to older clients; no retrieval or HTTP.
    with _source_errors(db):
        store.matrix(db, matrix_id, req.conversation_id, writing=True)
    raise HTTPException(409, {"code": "migration_required", "not_executed": True,
        "new_route": "/api/research/matrix-runs", "matrix_id": matrix_id,
        "message": "矩阵提取已迁移为可恢复任务，请保留选择并通过新任务入口继续；本次未调用模型。"})


class MatrixRunInput(ExtractInput):
    expected_version: int = Field(ge=1, strict=True)
    run_id: str = Field(pattern=r"^research_[a-f0-9]{32}$")
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    matrix_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class MatrixRunResume(Scoped):
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=1, strict=True)
    retry_unknown: bool = False


class MatrixRunCancel(Scoped):
    expected_revision: int = Field(ge=1, strict=True)
    expected_attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class MatrixRunApply(Scoped):
    expected_matrix_version: int = Field(ge=1, strict=True)
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")


class ResearchChildGrantInput(Scoped):
    parent_run_id: str = Field(min_length=1, max_length=96)
    expected_parent_revision: int = Field(ge=1, strict=True)
    expected_parent_attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_child_attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    pending_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    confirm_duplicate_cost: bool = Field(strict=True)

    @model_validator(mode="after")
    def explicit_cost_confirmation(self):
        if self.confirm_duplicate_cost is not True:
            raise ValueError("请明确确认本次未确认调用可能产生重复费用")
        return self


@router.get("/children")
def list_research_children(conversation_id: str, parent_run_id: str, db: Session = Depends(get_db)):
    from .. import research_children
    from ..research_models import ResearchChildLink
    from ..runtime_models import RuntimeRun
    with _source_errors(db):
        _scope(db, conversation_id)
        parent = db.get(RuntimeRun, parent_run_id)
        if parent is None or parent.conversation_id != conversation_id:
            raise sources.SourceNotFound("当前会话没有此父任务")
        links = db.scalars(select(ResearchChildLink).where(ResearchChildLink.parent_run_id == parent_run_id)
            .order_by(ResearchChildLink.created_at, ResearchChildLink.id))
        return {"items": [research_children.public(db, link.child_run_id, conversation_id,
            parent_run_id=parent_run_id) for link in links]}


@router.post("/children/{child_run_id}/retry-grants")
def approve_research_child_retry(child_run_id: str, req: ResearchChildGrantInput, db: Session = Depends(get_db)):
    from .. import research_children, run_store
    from ..goal_policy import GoalPolicyChanged
    with _matrix_run_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            grant, fresh = research_children.create_retry_grant(db, child_run_id=child_run_id,
                **req.model_dump(exclude={"confirm_duplicate_cost"}))
        except GoalPolicyChanged as exc:
            db.rollback()
            raise HTTPException(409, exc.denial) from None
        db.commit()
        return {"grant_id": grant.id, "child_run_id": child_run_id, "parent_run_id": req.parent_run_id,
            "pending_sha256": grant.pending_sha256, "replayed": not fresh,
            "message": "仅批准本次未确认调用；从父任务继续时消费，不会在此处发送请求。"}


@contextmanager
def _matrix_run_errors(db):
    from ..run_lifecycle import RunConflict
    from ..model_profiles import ModelConfigurationError
    from ..credential_protection import CredentialProtectionError
    try:
        with _source_errors(db):
            yield
    except (RunConflict, ModelConfigurationError, CredentialProtectionError) as exc:
        db.rollback()
        raise HTTPException(getattr(exc, "status_code", 409), str(exc)) from None


@router.post("/matrix-runs")
def create_matrix_run(req: MatrixRunInput, db: Session = Depends(get_db)):
    from .. import research_runs, run_store
    with _matrix_run_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        _, _, fresh = research_runs.create(db, kind="matrix", run_id=req.run_id,
            conversation_id=req.conversation_id, request_id=req.request_id,
            inputs={k: getattr(req, k) for k in ("matrix_id", "expected_version", "field_keys", "paper_ids")})
        db.commit()
        if fresh:
            research_runs.start(req.run_id)
        return {**research_runs.public(db, req.run_id, req.conversation_id, expected_kind="matrix"), "replayed": not fresh}


@router.get("/matrix-runs")
def list_matrix_runs(conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"),
                     matrix_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
                     limit: int = Query(default=20, ge=1, le=50), db: Session = Depends(get_db)):
    from .. import research_runs
    from ..runtime_models import RuntimeRun
    from ..research_models import MatrixRunSnapshot
    with _matrix_run_errors(db):
        _scope(db, conversation_id)
        query = select(RuntimeRun.id).where(RuntimeRun.kind == "matrix", RuntimeRun.conversation_id == conversation_id)
        if matrix_id:
            store.matrix(db, matrix_id, conversation_id)
            query = query.where(RuntimeRun.id.in_(select(MatrixRunSnapshot.run_id).where(MatrixRunSnapshot.matrix_id == matrix_id)))
        ids = db.scalars(query.order_by(RuntimeRun.created_at.desc(), RuntimeRun.id.desc()).limit(limit))
        return {"items": [research_runs.public(db, ident, conversation_id, expected_kind="matrix") for ident in ids]}


@router.get("/matrix-runs/{run_id}")
def get_matrix_run(run_id: str, conversation_id: str = Query(pattern=r"^[\w-]{1,64}$"), db: Session = Depends(get_db)):
    from .. import research_runs
    with _matrix_run_errors(db):
        return research_runs.public(db, run_id, conversation_id, expected_kind="matrix")


@router.post("/matrix-runs/{run_id}/resume")
def resume_matrix_run(run_id: str, req: MatrixRunResume, db: Session = Depends(get_db)):
    from .. import research_runs, run_store
    with _matrix_run_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        research_runs.public(db, run_id, req.conversation_id, expected_kind="matrix")
        _, _, fresh = research_runs.resume(db, run_id, conversation_id=req.conversation_id,
            request_id=req.request_id, expected_revision=req.expected_revision, retry_unknown=req.retry_unknown)
        db.commit()
        if fresh:
            research_runs.start(run_id)
        return {**research_runs.public(db, run_id, req.conversation_id, expected_kind="matrix"), "replayed": not fresh}


@router.post("/matrix-runs/{run_id}/cancel")
def cancel_matrix_run(run_id: str, req: MatrixRunCancel, db: Session = Depends(get_db)):
    from .. import research_runs, run_store
    with _matrix_run_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        run, attempt, _ = research_runs.checked(db, run_id, req.conversation_id, writing=True)
        if run.kind != "matrix":
            raise sources.SourceNotFound("当前会话没有此矩阵任务")
        if run.revision != req.expected_revision or attempt.id != req.expected_attempt_id:
            raise sources.SourceConflict("执行尝试或版本已变化，旧窗口停止请求未影响当前任务；请重新载入")
        status = research_runs.cancel(db, run_id, req.conversation_id)
        db.commit()
        return {**research_runs.public(db, run_id, req.conversation_id, expected_kind="matrix"), "cancel_status": status}


@router.post("/matrix-runs/{run_id}/apply")
def apply_matrix_run(run_id: str, req: MatrixRunApply, db: Session = Depends(get_db)):
    from .. import research_runs, matrix_run, run_store
    with _matrix_run_errors(db), run_store.coordinated():
        db.execute(text("BEGIN IMMEDIATE"))
        replayed = matrix_run.apply_saved(db, run_id, req.conversation_id, req.expected_matrix_version, req.request_id)
        db.commit()
        return {**research_runs.public(db, run_id, req.conversation_id, expected_kind="matrix"), "replayed": replayed}


@router.patch("/matrices/{matrix_id}/cell")
def edit_cell(matrix_id: str, req: CellInput, db: Session = Depends(get_db)):
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        item = store.matrix(db, matrix_id, req.conversation_id, writing=True)
        document = deepcopy(item.document)
        row = next((r for r in document["rows"] if r["key"] == req.field_key), None)
        if not row or req.paper_id not in row["cells"]:
            raise sources.SourceNotFound("单元格不存在")
        cell = row["cells"][req.paper_id]
        if req.use_candidate:
            from ..research_runs import digest
            from ..source_runtime import validate_source_evidence
            candidate = cell.get("extraction_candidate")
            if (not isinstance(candidate, dict) or candidate.get("sha256") != req.candidate_sha256
                    or digest({k: v for k, v in candidate.items() if k != "sha256"}) != req.candidate_sha256
                    or candidate.get("requires_review") is not True):
                raise sources.SourceConflict("候选已变化或损坏，请重新查看后采用；人工结论未修改")
            validate_source_evidence(db, req.conversation_id, candidate["evidence"])
            fixed_paper = next(p for p in document["papers"] if p["id"] == req.paper_id)
            if any(e.get("revision_id") != fixed_paper["revision_id"] for e in candidate["evidence"]):
                raise sources.SourceConflict("候选证据不属于此单元格固定来源，未采用")
            cell["extraction_history"] = [*deepcopy(cell.get("extraction_history", [])),
                {k: deepcopy(cell.get(k)) for k in ("evidence", "quote", "status", "extraction_status",
                    "verification_status", "manual", "extraction_candidate")}]
            cell.update({k: deepcopy(candidate[k]) for k in ("evidence", "quote", "extraction_status")})
            cell.pop("extraction_candidate", None)
        if req.status in ("supported", "conflict") and not cell.get("evidence"):
            raise sources.SourceError("没有可定位证据时不能标记支持或冲突")
        cell["manual"] = {"value": req.value, "note": req.note, "status": req.status,
            "actor": "local_user", "at": utcnow().isoformat(), "original_quote": cell.get("quote", "")}
        cell.update(status=req.status, verification_status="human_reviewed")
        item = store.save_matrix(db, item, document, req.expected_version, "cell_review")
        db.commit()
        return store.matrix_output(item)


@router.get("/matrices/{matrix_id}/export")
def export_matrix(matrix_id: str, conversation_id: str, db: Session = Depends(get_db)):
    with _source_errors(db):
        return store.export_matrix(db, store.matrix(db, matrix_id, conversation_id))


class ReadingInput(Scoped):
    expected_version: int = Field(ge=0)
    position: dict = Field(default_factory=dict)
    user_read: bool | None = None


def reading_result(state):
    return {"version": state.version if state else 0, "position": state.position if state else {},
        "user_read": bool(state.user_read) if state else False}


@router.get("/reading/{revision_id}")
def get_reading(revision_id: str, conversation_id: str, db: Session = Depends(get_db)):
    from ..library_sources import reading_target,copy_locator
    with _source_errors(db):
        scope=_scope(db,conversation_id)
        target,target_scope=reading_target(db,revision_id,scope)
        result=reading_result(db.get(ReadingState,target))
        result['position']=deepcopy(result['position'])
        if target!=revision_id and result['position'].get('locator_id'):
            result['position']['locator_id']=copy_locator(db,result['position']['locator_id'],target_scope,revision_id,scope)['locator_id']
        db.commit();return result


@router.put("/reading/{revision_id}")
def save_reading(revision_id: str, req: ReadingInput, db: Session = Depends(get_db)):
    from ..library_sources import reading_target,copy_locator
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        scope = _scope(db, req.conversation_id, writing=True)
        record = sources.read_revision(db, revision_id, scope=scope)
        allowed = {"page_index", "line_start", "offset", "sheet", "scroll_top", "zoom", "locator_id"}
        if set(req.position) - allowed:
            raise sources.SourceError("未知阅读位置字段")
        for key, value in req.position.items():
            if key in ("sheet", "locator_id"):
                if not isinstance(value, str) or len(value) > 128:
                    raise sources.SourceError("阅读位置文本无效")
            elif type(value) not in (int, float) or not 0 <= value <= 10_000_000:
                raise sources.SourceError("阅读位置数值无效")
        if req.position.get("locator_id"):
            loc = sources.read_locator(db, req.position["locator_id"], scope=scope)
            if loc["revision_id"] != revision_id:
                raise sources.SourceConflict("阅读位置属于其他版本，不自动映射")
        target,target_scope=reading_target(db,revision_id,scope)
        position=deepcopy(req.position)
        if target!=revision_id and position.get('locator_id'):
            position['locator_id']=copy_locator(db,position['locator_id'],scope,target,target_scope)['locator_id']
        state = db.get(ReadingState, target)
        if (state.version if state else 0) != req.expected_version:
            raise sources.SourceConflict("另一窗口已更新阅读进度，请重新打开")
        if state is None:
            state = ReadingState(revision_id=target, version=0)
            db.add(state)
        state.position, state.version, state.updated_at = position, state.version + 1, utcnow()
        if req.user_read is not None:
            state.user_read = int(req.user_read)
        db.commit()
        result=reading_result(state);result['position']=deepcopy(req.position);return result


class AnnotationInput(Scoped):
    locator_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["highlight", "annotation", "note"]
    body: str = Field(default="", max_length=20000)
    selected_text: str = Field(default="", max_length=20000)


class AnnotationEdit(Scoped):
    expected_version: int = Field(ge=1)
    body: str = Field(max_length=20000)
    deleted: bool = False


def annotation_result(a):
    return {"id": a.id, "revision_id": a.revision_id, "locator_id": a.locator_id, "version": a.version,
        "kind": a.kind, "body": a.body, "selected_text": a.selected_text, "deleted": bool(a.deleted)}


@router.get("/reading/{revision_id}/annotations")
def annotations(revision_id: str, conversation_id: str, db: Session = Depends(get_db)):
    from ..library_sources import reading_target,copy_locator
    with _source_errors(db):
        scope = _scope(db, conversation_id)
        record = sources.read_revision(db, revision_id, scope=scope)
        target,target_scope=reading_target(db,revision_id,scope)
        items = []
        for a in db.scalars(select(ReadingAnnotation).where(ReadingAnnotation.revision_id == target,
                ReadingAnnotation.deleted == 0).order_by(ReadingAnnotation.created_at)):
            result = annotation_result(a)
            try:
                result["locator"] = (copy_locator(db,a.locator_id,target_scope,revision_id,scope) if target!=revision_id
                    else sources.read_locator(db,a.locator_id,scope=scope))
                result['revision_id']=revision_id;result['locator_id']=result['locator']['locator_id']
                result["anchor_status"] = "fixed_version"
            except sources.SourceError:
                result["anchor_status"] = "unavailable"
            items.append(result)
        db.commit()
        return {"items": items, "current_revision_id": record.get("current_revision_id"),
            "mapping_policy": "version_exact_only"}


@router.post("/reading/{revision_id}/annotations")
def create_annotation(revision_id: str, req: AnnotationInput, db: Session = Depends(get_db)):
    from ..library_sources import reading_target,copy_locator
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        scope = _scope(db, req.conversation_id, writing=True)
        loc = sources.read_locator(db, req.locator_id, scope=scope)
        if loc["revision_id"] != revision_id:
            raise sources.SourceConflict("批注定位不属于此固定版本")
        # Selection is user supplied, but must be grounded in the actual server
        # extracted range. Whitespace normalization tolerates PDF text layers.
        if req.selected_text and "".join(req.selected_text.split()) not in "".join(loc["quote"].split()):
            raise sources.SourceConflict("所选文字与原文范围不一致，请重新选择；未生成虚假锚点")
        target,target_scope=reading_target(db,revision_id,scope)
        stored_locator=copy_locator(db,req.locator_id,scope,target,target_scope)['locator_id'] if target!=revision_id else req.locator_id
        a = ReadingAnnotation(revision_id=target, locator_id=stored_locator, kind=req.kind,
            body=req.body, selected_text=req.selected_text)
        db.add(a)
        db.flush()
        db.add(AnnotationRevision(annotation_id=a.id, version=1, document=annotation_result(a)))
        db.commit()
        return {**annotation_result(a),'revision_id':revision_id,'locator_id':req.locator_id}


@router.patch("/annotations/{annotation_id}")
def edit_annotation(annotation_id: str, req: AnnotationEdit, db: Session = Depends(get_db)):
    from ..library_sources import authorize_annotation
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        scope = _scope(db, req.conversation_id, writing=True)
        a = db.get(ReadingAnnotation, annotation_id)
        if not a:
            raise sources.SourceNotFound("批注不存在")
        a=authorize_annotation(db,a,req.conversation_id)
        if a.version != req.expected_version:
            raise sources.SourceConflict("批注已在另一窗口修改")
        a.body, a.deleted, a.version, a.updated_at = req.body, int(req.deleted), a.version + 1, utcnow()
        db.add(AnnotationRevision(annotation_id=a.id, version=a.version, document=annotation_result(a)))
        db.commit()
        return annotation_result(a)


class RelationReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    action: Literal["confirm", "exclude", "correct", "restore", "reset"]
    reason: str = Field(min_length=1, max_length=2000)
    target: str | None = None
    relation_type: Literal["citation", "similar", "shared_topic", "uncertain"] | None = None
    restore_version: int | None = Field(default=None, ge=1)


@router.get("/relations/{relation_id}")
def relation_detail(relation_id: str, db: Session = Depends(get_db)):
    from ..research_relations import get_relation, relation_output
    from ..research_models import RelationRevision
    with _source_errors(db):
        item = get_relation(db, relation_id)
        return {**relation_output(item), "history": [{"version": r.version, "action": r.action,
            "actor": r.actor, "reason": r.reason, "created_at": r.created_at.isoformat(), "document": r.document}
            for r in db.scalars(select(RelationRevision).where(RelationRevision.relation_id == item.id).order_by(RelationRevision.version.desc()))]}


@router.patch("/relations/{relation_id}")
def relation_review(relation_id: str, req: RelationReview, db: Session = Depends(get_db)):
    from ..research_relations import get_relation, review_relation, relation_output
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        item = review_relation(db, get_relation(db, relation_id), req.expected_version, req.action,
            req.reason, req.target, req.relation_type, req.restore_version)
        db.commit()
        return relation_output(item)


class RelationLocation(Scoped):
    context_index: int | None = Field(default=None, ge=0, le=7)


@router.post("/relations/{relation_id}/locate")
def locate_relation(relation_id: str, req: RelationLocation, db: Session = Depends(get_db)):
    from ..research_relations import get_relation
    from ..models import Paper
    with _source_errors(db):
        db.execute(text("BEGIN IMMEDIATE"))
        scope = _scope(db, req.conversation_id, writing=True)
        item = get_relation(db, relation_id)
        if req.context_index is not None:
            from ..citation_contexts import locate_context
            result = locate_context(db, item, req.conversation_id, req.context_index)
            db.commit()
            return result
        paper = db.get(Paper, item.source_paper_id)
        if paper.content_hash != item.extracted["source_sha256"]:
            raise sources.SourceConflict("来源版本已变化，不能将旧关系定位到新原件")
        page = item.extracted.get("reference_page")
        if not page or page < 1:
            raise sources.SourceNotFound("该条目没有精确位置；原句仍标记缺失")
        fixed = store.pin_paper(db, paper, req.conversation_id)
        loc = sources.add_pdf_locator(db, fixed["revision_id"], scope=scope,
            page_index=page - 1, bbox=item.extracted.get("reference_bbox"))
        db.commit()
        return {"revision_id": fixed["revision_id"], "locator_id": loc["locator_id"], "conversation_id": req.conversation_id,
            "relation_id": item.id, "relation_version": item.version, "location_kind": "reference_entry", "citation_sentence_verified": False}
