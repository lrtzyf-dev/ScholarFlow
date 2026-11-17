"""Transactional removal of conversation-owned content, without deleting projects.

Relationships missing SQL foreign keys are declared here as well. Preview and
execution share a plan, so newly introduced scoped records cannot be forgotten.
This is logical removal, not a promise to erase historical backups or disk blocks.
"""
from __future__ import annotations

from sqlalchemy import and_, or_, select, func, literal, union_all, cast, LargeBinary, String, Text, JSON
from fastapi import HTTPException

from .db import Base

# Project actions survive transcripts: their journal remains necessary to review
# and undo changes already made on disk. They are named explicitly in the UI.
RETAINED = {"project_changes", "project_commands", "workspace_switches", "workspace_transfers"}
SAFE_FILE_STATES = {"prepared", "deleted", "manual_cleanup_acknowledged"}

# child table, child reference, parent table, parent identity
LINKS = [
    ("library_index_prepared_batches", "operation_id", "library_index_operations", "id"),
    ("call_records", "run_id", "library_index_operations", "run_id"),
    ("diagnostic_recovery_actions", "diagnostic_id", "diagnostic_records", "id"),
    ("diagnostic_records", "run_id", "runtime_runs", "id"),
    ("diagnostic_records", "run_id", "foreground_runs", "id"),
    ("diagnostic_records", "run_id", "tasks", "id"),
    ("diagnostic_records", "source_revision_id", "source_revisions", "id"),
    ("call_price_assessments", "call_id", "call_records", "id"),
    ("call_charge_confirmations", "call_id", "call_records", "id"),
    ("budget_charge_events", "reservation_id", "resource_reservations", "id"),
    ("budget_charge_events", "call_id", "call_records", "id"),
    ("resource_startup_recoveries", "reservation_id", "resource_reservations", "id"),
    ("resource_startup_recoveries", "budget_id", "resource_budgets", "id"),
    ("bibliography_import_entries", "batch_id", "bibliography_import_batches", "id"),
    ("bibliography_import_batches", "source_revision_id", "source_revisions", "id"),
    ("zotero_items", "binding_id", "zotero_bindings", "id"),
    ("zotero_items", "source_revision_id", "source_revisions", "id"),
    ("zotero_item_revisions", "item_id", "zotero_items", "id"),
    ("zotero_item_revisions", "source_revision_id", "source_revisions", "id"),
    ("zotero_attachment_versions", "item_id", "zotero_items", "id"),
    ("zotero_attachment_versions", "source_revision_id", "source_revisions", "id"),
    ("source_library_annotation_origins", "library_annotation_id", "research_annotations", "id"),
    ("run_attempts", "run_id", "runtime_runs", "id"),
    ("run_timing_spans", "run_id", "runtime_runs", "id"),
    ("run_admission_requests", "run_id", "runtime_runs", "id"),
    ("call_records", "run_id", "runtime_runs", "id"),
    ("call_records", "run_id", "foreground_runs", "id"),
    ("call_records", "run_id", "tasks", "id"),
    ("call_records", "run_id", "persistent_subtasks", "id"),
    ("call_usage_receipts", "call_id", "call_records", "id"),
    ("subtask_call_links", "call_id", "call_records", "id"),
    ("subtask_call_links", "subtask_id", "persistent_subtasks", "id"),
    ("resource_budgets", "owner_run_id", "runtime_runs", "id"),
    ("resource_budgets", "owner_run_id", "foreground_runs", "id"),
    ("resource_budgets", "owner_run_id", "tasks", "id"),
    ("resource_reservations", "budget_id", "resource_budgets", "id"),
    ("persistent_subtasks", "parent_run_id", "runtime_runs", "id"),
    ("persistent_subtask_attempts", "subtask_id", "persistent_subtasks", "id"),
    ("subtask_operation_resolutions", "subtask_id", "persistent_subtasks", "id"),
    ("subtask_operation_resolutions", "parent_run_id", "runtime_runs", "id"),
    ("goals", "run_id", "runtime_runs", "id"),
    ("goal_requirements", "goal_id", "goals", "id"),
    ("goal_revisions", "goal_id", "goals", "id"),
    ("artifact_revisions", "artifact_id", "artifact_manifests", "id"),
    ("artifact_changes", "artifact_id", "artifact_manifests", "id"),
    ("artifact_computations", "artifact_id", "artifact_manifests", "id"),
    ("artifact_exports", "artifact_id", "artifact_manifests", "id"),
    ("artifact_exports", "revision_id", "artifact_revisions", "id"),
    ("research_matrix_revisions", "matrix_id", "research_matrices", "id"),
    ("research_matrix_creation_requests", "matrix_id", "research_matrices", "id"),
    ("research_matrix_run_snapshots", "matrix_id", "research_matrices", "id"),
    ("research_matrix_run_snapshots", "run_id", "runtime_runs", "id"),
    ("research_child_links", "parent_run_id", "runtime_runs", "id"),
    ("research_child_links", "child_run_id", "runtime_runs", "id"),
    ("research_child_attempt_links", "link_id", "research_child_links", "id"),
    ("research_child_commands", "link_id", "research_child_links", "id"),
    ("research_child_retry_grants", "link_id", "research_child_links", "id"),
    ("research_child_retry_consumptions", "grant_id", "research_child_retry_grants", "id"),
    ("source_revisions", "source_id", "sources", "id"),
    ("source_capture_receipts", "revision_id", "source_revisions", "id"),
    ("source_extractions", "revision_id", "source_revisions", "id"),
    ("table_computations", "revision_id", "source_revisions", "id"),
    ("source_library_imports", "revision_id", "source_revisions", "id"),
    ("source_locators", "revision_id", "source_revisions", "id"),
    ("research_reading_states", "revision_id", "source_revisions", "id"),
    ("research_annotations", "revision_id", "source_revisions", "id"),
    ("research_annotation_revisions", "annotation_id", "research_annotations", "id"),
]


def _plan(conversation_id):
    tables = Base.metadata.tables
    direct, parents = {}, {}
    for name, table in tables.items():
        if "conversation_id" in table.c and name not in RETAINED:
            direct[name] = table.c.conversation_id == conversation_id
    for name in ("sources", "source_snapshots", "source_capture_receipts"):
        if name in tables:
            direct[name] = and_(tables[name].c.scope_type == "conversation", tables[name].c.scope_id == conversation_id)
    direct["desktop_state"] = tables["desktop_state"].c.key == "conversation:" + conversation_id
    direct["conversations"] = tables["conversations"].c.id == conversation_id
    for child, column, parent, identifier in LINKS:
        if child in tables and parent in tables:
            parents.setdefault(child, []).append((column, parent, identifier))
    conditions, visiting = {}, set()

    def condition(name):
        if name in conditions:
            return conditions[name]
        if name in visiting:
            raise RuntimeError("清理依赖循环，数据未删除")
        visiting.add(name)
        parts = [direct[name]] if name in direct else []
        for column, parent, identifier in parents.get(name, []):
            parent_condition = condition(parent)
            if parent_condition is not None:
                parts.append(tables[name].c[column].in_(select(tables[parent].c[identifier]).where(parent_condition)))
        visiting.remove(name)
        conditions[name] = or_(*parts) if parts else None
        return conditions[name]

    for name in tables:
        condition(name)
    # These records deliberately contain no conversation ID or transcript.
    # Their stable projection IDs still belong to the deleted execution owner.
    receipt_ids = [select((literal(prefix) + tables[name].c[identifier]).label('item_id')).where(conditions[name])
        for name, identifier, prefix in (
            ('foreground_runs', 'id', 'foreground:'), ('tasks', 'id', 'background:'),
            ('runtime_runs', 'id', 'run:'), ('persistent_subtasks', 'id', 'subtask:'),
            ('run_inputs', 'input_id', 'input:'))
        if name in tables and conditions.get(name) is not None]
    if receipt_ids:
        identities = union_all(*receipt_ids).subquery()
        for name in ('run_attention_receipts', 'run_notification_receipts'):
            if name in tables:
                conditions[name] = tables[name].c.item_id.in_(select(identities.c.item_id))
    # All children must be removed before parent-dependent subqueries go empty.
    children = {}
    for child, links in parents.items():
        for _, parent, _ in links:
            children.setdefault(parent, set()).add(child)
    # Snapshots have no FK deletion cascade: discard them after revisions.
    children.setdefault("source_snapshots", set()).add("source_revisions")
    # Locator FK is independent of source revision in annotations.
    children.setdefault("source_locators", set()).add("research_annotations")
    ordered, visited = [], set()
    def order(name):
        if name in visited:
            return
        visited.add(name)
        for child in sorted(children.get(name, [])):
            order(child)
        if conditions.get(name) is not None:
            ordered.append(name)
    for name in sorted(tables):
        order(name)
    return tables, conditions, ordered


def preview(db, conversation_id):
    tables, conditions, ordered = _plan(conversation_id)
    counts = {name: db.scalar(select(func.count()).select_from(tables[name]).where(conditions[name])) for name in ordered}
    payloads = {}
    for name in ordered:
        if not counts[name]:
            continue
        columns = [c for c in tables[name].c if isinstance(c.type, (String, Text, JSON, LargeBinary))]
        if columns:
            size = sum((func.coalesce(func.length(cast(c, LargeBinary)), 0) for c in columns), literal(0))
            payloads[name] = int(db.scalar(select(func.coalesce(func.sum(size), 0)).where(conditions[name])) or 0)
    categories = {}
    for name, size in payloads.items():
        label = ("待确认更新原件与选择" if name == "source_update_drafts" else "来源原件与文本" if name == "source_snapshots" else "解析结果与引用位置" if name.startswith(("source_", "research_"))
                 else "会话与摘要" if name in ("qa_records", "conversation_memory", "conversations", "compaction_calls")
                 else "研究成果与版本" if name.startswith("artifact_") else "任务与相关记录")
        group = categories.setdefault(label, {"label": label, "records": 0, "logical_payload_bytes": 0})
        group["records"] += counts[name]
        group["logical_payload_bytes"] += size
    retained = {name: db.scalar(select(func.count()).select_from(tables[name]).where(tables[name].c.conversation_id == conversation_id))
                for name in RETAINED if name in tables}
    cloud = tables.get("provider_file_bindings")
    blockers = []
    if cloud is not None:
        rows = db.execute(select(cloud.c.id, cloud.c.state).where(cloud.c.conversation_id == conversation_id,
            cloud.c.state.not_in(SAFE_FILE_STATES))).all()
        blockers = [{"binding_id": row.id, "state": row.state, "reason": "请先在会话资料的 DeepSeek 云文件中核对并删除远端文件"} for row in rows]
    return {"conversation_id": conversation_id, "records": {key: count for key, count in counts.items() if count},
            "total_records": sum(counts.values()), "retained_project_journals": {key: count for key, count in retained.items() if count},
            "cloud_file_blockers": blockers, "can_delete": not blockers,
            "storage_preview": {"logical_payload_bytes": sum(payloads.values()), "by_table": payloads,
                "categories": list(categories.values()),
                "measurement": "sqlite_variable_length_payload_utf8_bytes", "physical_reclaimed_bytes": None,
                "includes_database_pages_or_indexes": False, "external_project_files_deleted": False,
                "backup_copies_deleted": False},
            "preserved": ["已加入文献库的独立文献及原件", "其他会话与分支", "项目文件、Git历史及项目操作日志", "已有备份与恢复快照", "供应商计费与服务端记录"],
            "disk_erasure": "logical_removal_only"}


def remove(db, conversation_id):
    """Caller holds the conversation writer lock and commits once with tombstone."""
    from .library_sources import preserve_conversation_library
    from .source_store import SourceError
    try:
        preserve_conversation_library(db, conversation_id)
    except SourceError:
        raise HTTPException(409, '文献库原件迁移校验失败，原会话未删除；请先核对文献原件与版本。') from None
    result = preview(db, conversation_id)
    if not result["can_delete"]:
        raise HTTPException(409, {"message": "仍有云文件未确认清除，请先完成远端清理。", **result})
    from .history_revisions import scrub_deleted_origin
    scrub_deleted_origin(db, conversation_id)
    tables, conditions, ordered = _plan(conversation_id)
    # Capture every affected row identity before deleting anything. Some child
    # predicates depend on multiple ancestor tables that are not SQL FKs.
    identities = {}
    for name in ordered:
        table = tables[name]
        primary = list(table.primary_key.columns)
        identities[name] = (primary, db.execute(select(*primary).where(conditions[name])).all())
    for name in ordered:
        table = tables[name]
        primary, rows = identities[name]
        # Bound parameter count even for very large histories and composite IDs.
        for start in range(0, len(rows), 200):
            predicates = [and_(*(column == value for column, value in zip(primary, row))) for row in rows[start:start + 200]]
            db.execute(table.delete().where(or_(*predicates)))
    from .models import DesktopState
    selection = db.get(DesktopState, "selection", populate_existing=True)
    if selection and selection.value.get("conversation_id") == conversation_id:
        selection.value = {**selection.value, "conversation_id": None}
        selection.revision += 1
    from .storage_policy import record_cleanup
    result["receipt_id"] = record_cleanup(db, conversation_id, result)
    return result
