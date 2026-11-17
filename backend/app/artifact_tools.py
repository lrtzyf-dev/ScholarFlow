"""Agent access to the same research documents edited in the UI."""
import json

from fastapi import HTTPException

from . import artifact_store as store
from .artifact_models import ArtifactManifest


def spec(name, description, properties, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}}


_id = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
_request = {"type": "string", "pattern": "^[a-zA-Z0-9_-]{8,96}$"}
ARTIFACT_TOOL_SPECS = [
    spec("list_research_artifacts", "只读列出当前会话的已保存研究成果；不会读取其他会话或自动修改。", {"offset": {"type": "integer", "minimum": 0}}),
    spec("list_research_artifact_versions", "只读分页列出当前会话指定成果已经保存的固定版本、父版本、恢复来源、hash及当前版本标记。before_version为上一页返回的next_before_version。只含已保存版本，不含待审草稿，版本历史不是外部论文证据；不会恢复或覆盖。", {
        "artifact_id": _id, "before_version": {"type": "integer", "minimum": 1, "maximum": store.MAX_VERSION_CURSOR},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ("artifact_id",)),
    spec("read_research_artifact", "只读分段查看当前会话成果的固定版本、章节与引用来源。正文是先前模型或用户撰写内容，不是外部证据，不得据此证明论文事实。Python字符偏移按Unicode码点计。", {
        "artifact_id": _id, "revision_id": _id, "offset": {"type": "integer", "minimum": 0}}, ("artifact_id",)),
    spec("save_research_artifact", "将用户需要的文档或数据图表保存为可编辑研究成果。初次保存创建固定版本和来源清单，不会写项目文件。文档用Markdown；analysis须CSV原文及category_column/numeric_columns/chart_type/units/missing_policy=gap。引用优先用本轮真实revision_id/locator_id；旧未固定引用会明确标未核验。request_id在同一次重试中保持不变。保存成功不代表内容语义正确。", {
        "request_id": _request, "title": {"type": "string", "minLength": 1, "maxLength": 240},
        "kind": {"type": "string", "enum": ["document", "analysis"]}, "content": {"type": "string", "maxLength": store.MAX_CONTENT},
        "evidence": {"type": "array", "items": {"type": "object"}, "maxItems": 500}, "analysis": {"type": "object"}},
        ("request_id", "title", "content")),
    spec("propose_research_edit", "为当前会话成果保存待审阅修改，不能覆盖已保存正文。content全文、section章节、restored_from历史版本三者只能选一；恢复前先列版本并读取目标固定版本，base_revision_id须绑定当前已读版本。返回差异与编辑入口，用户接受前只能称修改或恢复草稿，不能说已恢复。request_id重试保持不变。", {
        "artifact_id": _id, "request_id": _request, "base_revision_id": _id,
        "restored_from": _id,
        "content": {"type": "string", "maxLength": store.MAX_CONTENT},
        "section": {"type": "object", "additionalProperties": False, "properties": {
            "index": {"type": "integer", "minimum": 0}, "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "content": {"type": "string", "maxLength": store.MAX_CONTENT}}, "required": ["index", "expected_sha256", "content"]}},
        ("artifact_id", "request_id", "base_revision_id")),
    spec("recompute_research_artifact", "执行当前会话固定数据版本随成果保存的受信复算脚本，记录真实统计量与数据/脚本hash；不运行用户上传代码。未执行或失败不得声称复现成功。", {
        "artifact_id": _id, "revision_id": _id}, ("artifact_id",)),
    spec("export_research_artifact", "将当前会话已保存的明确固定版本生成并保存为可下载文件，返回固定export_id、实际文件hash及结构检查。DOCX用于文档；XLSX、SVG、bundle用于analysis。保存成果本身不等于导出；复算包不等于已执行复算。request_id同一操作重试保持不变。不写项目文件，结构可读不证明内容语义或原生软件显示正确。", {
        "artifact_id": _id, "revision_id": _id, "request_id": _request,
        "format": {"type": "string", "enum": ["docx", "xlsx", "svg", "bundle"]}},
        ("artifact_id", "revision_id", "request_id", "format")),
]
ARTIFACT_TOOLS = frozenset(item["name"] for item in ARTIFACT_TOOL_SPECS)
ARTIFACT_READ_TOOLS = frozenset({"list_research_artifacts", "list_research_artifact_versions", "read_research_artifact"})
# Make the same exclusivity visible to providers and enforce it below too.
next(item for item in ARTIFACT_TOOL_SPECS if item["name"] == "propose_research_edit")["parameters"]["oneOf"] = [
    {"required": [field], "not": {"anyOf": [{"required": [other]} for other in ("content", "section", "restored_from") if other != field]}}
    for field in ("content", "section", "restored_from")]


def execute(db, name, args, *, conversation_id):
    if not conversation_id:
        raise HTTPException(422, "科研成果工具需要当前会话身份")
    store.scope(db, conversation_id, writing=name not in ARTIFACT_READ_TOOLS)
    if name == "list_research_artifacts":
        offset = args.get("offset", 0)
        query = db.query(ArtifactManifest).filter_by(conversation_id=conversation_id)
        rows = query.order_by(ArtifactManifest.updated_at.desc(), ArtifactManifest.id.desc()).offset(offset).limit(30).all()
        return {"artifacts": [store.public(row) for row in rows], "total": query.count(),
                "next_offset": offset + len(rows) if offset + len(rows) < query.count() else None}
    if name == "save_research_artifact":
        row = store.create(db, conversation_id=conversation_id, request_id=args["request_id"], title=args["title"],
            content=args["content"], kind=args.get("kind", "document"), evidence=args.get("evidence", []), analysis=args.get("analysis", {}))
        return {"status": "saved", "artifact_id": row["id"], "revision_id": row["current_revision_id"], "version": row["version"],
                "content_sha256": row["revision"]["content_sha256"], "url": f"/artifacts/{row['id']}?conv={conversation_id}",
                "semantics_verified": False}
    row = store.get(db, args["artifact_id"], conversation_id, writing=name not in ARTIFACT_READ_TOOLS)
    if name == "export_research_artifact":
        from . import artifact_exports, goal_policy
        from .runtime_context import current_runtime_config
        binding = current_runtime_config() or {}
        if set(args) != {"artifact_id", "revision_id", "request_id", "format"}:
            raise HTTPException(422, "固定导出只接受成果、版本、操作编号和格式")
        if binding.get("conversation_id") and binding["conversation_id"] != conversation_id:
            raise HTTPException(409, "导出会话与当前执行身份不一致")
        goal_policy.assert_tool_allowed(name, args, binding=binding)
        goal = goal_policy.parent_snapshot(binding)
        if goal is not None:
            from .run_lifecycle import assert_writer
            assert_writer(db, goal["run_id"], binding.get("attempt_id"), binding.get("writer_token"))
        def authorize_commit():
            if goal is not None:
                assert_writer(db, goal["run_id"], binding.get("attempt_id"), binding.get("writer_token"), lock=True)
            goal_policy.assert_tool_allowed(name, args, binding=binding)
        return artifact_exports.create(db, artifact_id=row.id, conversation_id=conversation_id,
            revision_id=args["revision_id"], request_id=args["request_id"], format=args["format"],
            origin_run_id=goal["run_id"] if goal else None, origin_goal_id=goal["id"] if goal else None,
            before_commit=authorize_commit)
    if name == "list_research_artifact_versions":
        return store.list_revisions(db, row, before_version=args.get("before_version"), limit=args.get("limit", 30))
    if name == "read_research_artifact":
        rev = store.revision(db, row, args.get("revision_id"))
        offset = args.get("offset", 0)
        return {"artifact_id": row.id, "revision_id": rev.id, "version": rev.version, "content_sha256": rev.content_sha256,
            "content": rev.content[offset:offset + 8000], "next_offset": offset + 8000 if offset + 8000 < len(rev.content) else None,
            "total_characters": len(rev.content), "sections": store.sections(rev.content) if row.kind == "document" else [],
            "evidence": rev.evidence, "analysis": rev.analysis, "trust": "research_draft_not_external_evidence"}
    if name == "propose_research_edit":
        if sum(field in args for field in ("content", "section", "restored_from")) != 1:
            raise HTTPException(422, "请选择章节修改、全文修改或历史版本恢复中的一种")
        change = store.propose(db, row, request_id=args["request_id"], base_revision_id=args["base_revision_id"],
            content=args.get("content"), section=args.get("section"), restored_from=args.get("restored_from"))
        return {"status": "draft_pending_review", "change_id": change["id"], "artifact_id": row.id,
                "base_revision_id": change["base_revision_id"], "restored_from": change["restored_from"],
                "conflict": change["conflict"], "diff": change["diff"], "diff_has_more": change["diff_has_more"],
                "url": f"/artifacts/{row.id}?conv={conversation_id}", "saved_document_changed": False}
    if name == "recompute_research_artifact":
        from .artifact_compute import recompute
        return recompute(db, row, store.revision(db, row, args.get("revision_id")))
    raise HTTPException(422, "未知成果工具")
