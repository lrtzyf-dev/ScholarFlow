"""Versioned, code-reviewed exact-schema import transforms; no plugins or SQL input.

The first supported contract copies an already-current schema into separate
dormant data. It does not migrate an unknown historical schema or its runtime
protocol. Every old checkpoint remains only in the retained preparation archive.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import sqlite3

from .release_info import ReleaseError, canonical_json
from .release_schema import schema_snapshot
from .legacy_cross_schema import FTS_ID, NO_FTS_ID, PRE_EXPORT_ID, SOURCES
from .legacy_schema_contracts import OLD20_COLUMNS, PRE_EXPORT_SHA256, TARGET_SHA256


REGISTRY_REVISION = 3
MIGRATION_ID = "current-schema-dormant-copy-20261116-v2"
CURRENT_SCHEMA = TARGET_SHA256
CONTRACT = {"generation": 1, "checkpoint_protocol": 1,
            "rollback_policy": "exact_schema_or_verified_compatible_snapshot"}

# Not populated from the candidate, archive or CLI. Supporting another schema
# requires a source change, explicit transform and independently reviewed tests.
_REGISTRY = {MIGRATION_ID: {"registry_revision": REGISTRY_REVISION, "transform_version": 1,
    "source_schema_sha256": CURRENT_SCHEMA, "target_schema_sha256": CURRENT_SCHEMA,
    "target_data_contract": CONTRACT, "structure_changed": False,
    "old_checkpoint_protocol_accepted": False}}
for _ident, _source in SOURCES.items():
    _REGISTRY[_ident] = {"registry_revision": REGISTRY_REVISION, "transform_version": 2,
        "source_schema_sha256": _source, "target_schema_sha256": CURRENT_SCHEMA,
        "target_data_contract": CONTRACT, "structure_changed": True,
        "old_checkpoint_protocol_accepted": False,
        "mapping_protocol": "git_old20_named_rows_and_fixed_raw_v1"}
_REGISTRY[PRE_EXPORT_ID] = {"registry_revision": REGISTRY_REVISION, "transform_version": 1,
    "source_schema_sha256": PRE_EXPORT_SHA256, "target_schema_sha256": CURRENT_SCHEMA,
    "target_data_contract": CONTRACT, "structure_changed": True,
    "old_checkpoint_protocol_accepted": False, "mapping_protocol": "fixed_export_table_addition_v1"}

_EFFECTS = ("workspace_switches", "workspace_transfers", "managed_worktrees", "project_changes",
            "project_commands", "git_operations", "artifact_changes", "artifact_computations")
_TERMINAL = "'completed','done','failed','cancelled','ended','stopped','applied','rejected','committed','succeeded'"
_MUTABLE = {
    "subscriptions": {"active"}, "run_inputs": {"status", "reason"},
    "conversation_options": {"approval_mode", "revision"}, "papers": {"pdf_path", "parsed_path"},
    "runtime_runs": {"current_attempt_id", "status", "outcome", "revision"},
    "run_attempts": {"checkpoint", "status", "writer_token", "error"},
    "foreground_runs": {"agent_checkpoint", "status", "phase", "error"},
    "tasks": {"checkpoint", "status", "error"},
    "persistent_subtasks": {"checkpoint", "state", "writer_token", "cancel_requested"},
    "persistent_subtask_attempts": {"checkpoint", "state", "writer_token"},
    "library_index_operations": {"status", "prepared"}, "qa_records": {"provider_context"},
    "service_tool_invocations": {"state"}, "compaction_calls": {"status"},
    "resource_reservations": {"state"}, **{table: {"status"} for table in _EFFECTS}}
_REASON = "旧数据分离导入：仅保留历史，不恢复旧队列或检查点；请新建任务并重新配置凭证"


def resolve(migration_id, source_schema, target_schema, target_contract):
    item = _REGISTRY.get(migration_id)
    if (item is None or source_schema != item["source_schema_sha256"]
            or target_schema != item["target_schema_sha256"]
            or canonical_json(target_contract) != canonical_json(item["target_data_contract"])):
        raise ReleaseError("没有此精确来源结构、目标结构及契约的已审核分离导入项；未知结构不自动迁移")
    return {"id": migration_id, **item, "target_data_contract": dict(item["target_data_contract"])}


def immutable_rows(connection, migration_id=MIGRATION_ID):
    """Hash all retained rows/columns outside the small audited mutable set."""
    result = {}
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if table.startswith("sqlite_"):
            continue
        cross = migration_id in SOURCES
        if migration_id == PRE_EXPORT_ID and table == "artifact_exports":
            # The exact former schema has no exports. Separate empty-table
            # checks cover this addition without changing retained-row proof.
            continue
        if cross and table not in OLD20_COLUMNS:
            # The rebuilt FTS and entirely new tables have separate exact
            # target/default/source checks; this proof covers retained rows.
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        available = [r[1] for r in connection.execute("PRAGMA table_info(" + quoted + ")")]
        old_columns = OLD20_COLUMNS[table] if cross else available
        mutable = _MUTABLE.get(table, set()) | ({"status", "error"} if cross and table == "papers" else set())
        columns = [name for name in old_columns if name not in mutable]
        if not set(columns) <= set(available):
            raise ReleaseError("旧结构映射丢失已审核的历史列")
        if not columns:
            raise ReleaseError("审核导入项没有保留表的稳定身份字段")
        names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
        # SQLite sorts bounded pages on disk rather than retaining a Python
        # digest list proportional to the entire research library. Explicit
        # types/collation keep mixed retained values in a stable order.
        ordering = ','.join('typeof("' + name.replace('"', '""') + '"),"' +
                            name.replace('"', '""') + '" COLLATE BINARY' for name in columns)
        hasher, count = hashlib.sha256(), 0
        for row in connection.execute("SELECT " + names + " FROM " + quoted + " ORDER BY " + ordering):
            encoded = [{"blob": value.hex()} if isinstance(value, bytes) else value for value in row]
            raw = canonical_json(encoded)
            hasher.update(len(raw).to_bytes(8, 'big'))
            hasher.update(raw)
            count += 1
        result[table] = {"rows": count, "retained_columns_sha256": hasher.hexdigest()}
    return result


def transform(database, migration_id):
    """Only a private extracted copy is passed by legacy_import, never old data."""
    if migration_id not in _REGISTRY or schema_snapshot(database)["sha256"] != CURRENT_SCHEMA:
        raise ReleaseError("分离导入副本结构与审核注册项不符")
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN IMMEDIATE")
        # Queued effects become uncertain history, never fake successful work.
        db.execute(f"UPDATE runtime_runs SET outcome=CASE WHEN status NOT IN ({_TERMINAL}) THEN 'incomplete' ELSE outcome END, "
                   f"status=CASE WHEN status NOT IN ({_TERMINAL}) THEN 'cancelled' ELSE status END,current_attempt_id=NULL,revision=revision+1")
        db.execute(f"UPDATE run_attempts SET checkpoint=NULL,writer_token='legacy-import-inert',error=?, "
                   f"status=CASE WHEN status NOT IN ({_TERMINAL}) THEN 'cancelled' ELSE status END", (_REASON,))
        db.execute(f"UPDATE foreground_runs SET agent_checkpoint=NULL,phase='archived_import',error=?, "
                   f"status=CASE WHEN status NOT IN ({_TERMINAL}) THEN 'cancelled' ELSE status END", (_REASON,))
        db.execute(f"UPDATE tasks SET checkpoint=NULL,error=?,status=CASE WHEN status NOT IN ({_TERMINAL}) THEN 'cancelled' ELSE status END", (_REASON,))
        for table in ("persistent_subtasks", "persistent_subtask_attempts"):
            extra = ",cancel_requested=1,writer_token=NULL" if table == "persistent_subtasks" else ",writer_token='legacy-import-inert'"
            db.execute(f"UPDATE {table} SET checkpoint='{{}}',state=CASE WHEN state NOT IN ({_TERMINAL}) THEN 'blocked' ELSE state END" + extra)
        db.execute(f"UPDATE library_index_operations SET prepared=NULL,status=CASE WHEN status NOT IN ({_TERMINAL}) THEN 'cancelled' ELSE status END")
        for table in _EFFECTS:
            db.execute(f"UPDATE {table} SET status='unknown' WHERE status NOT IN ({_TERMINAL})")
        db.execute("UPDATE service_tool_invocations SET state='unknown' WHERE state IN ('prepared','started')")
        db.execute("UPDATE compaction_calls SET status='failed' WHERE status='started'")
        db.execute("UPDATE resource_reservations SET state='unknown' WHERE state IN ('reserved','dispatching','dispatched')")
        db.execute("UPDATE qa_records SET provider_context='{}'")
        db.commit()
        db.execute("VACUUM")
    verify_dormant(database)


def prepare_structure(database, migration_id):
    """Retain the original current-copy contract and use only registered DDL."""
    if migration_id not in _REGISTRY:
        raise ReleaseError("没有此精确结构迁移的已审核注册项")
    if migration_id in SOURCES:
        from .legacy_cross_schema import upgrade
        upgrade(database, migration_id)
    elif migration_id == PRE_EXPORT_ID:
        from .legacy_cross_schema import upgrade_previous_current
        upgrade_previous_current(database)
    elif schema_snapshot(database)["sha256"] != CURRENT_SCHEMA:
        raise ReleaseError("当前结构复制来源与审核注册项不符")


def finish_cross_schema(database, migration_id, *, stage, output, manifest, transaction_id):
    if migration_id == PRE_EXPORT_ID:
        from .legacy_cross_schema import verify_empty_exports
        verify_empty_exports(database)
        return {"mapping_protocol": "fixed_export_table_addition_v1", "fixed_exports_created": 0}
    if migration_id not in SOURCES:
        return None
    from .legacy_cross_schema import rebuild_fts, verify_new_defaults, verify_new_tables, quarantine_vectors
    from .legacy_fixed_sources import build
    verify_new_defaults(database)
    sources = build(database, stage=stage, output=output, manifest=manifest,
                    transaction_id=transaction_id, source_schema=SOURCES[migration_id])
    fts = rebuild_fts(database)
    vectors = quarantine_vectors(stage, manifest, transaction_id)
    verify_new_tables(database)
    return {"source_migration": sources, "full_text_index": fts, "legacy_vectors": vectors}


def verify_dormant(database):
    with closing(sqlite3.connect(database)) as db:
        checks = {
            "runtime_runs": f"current_attempt_id IS NOT NULL OR status NOT IN ({_TERMINAL})",
            "run_attempts": f"checkpoint IS NOT NULL OR status NOT IN ({_TERMINAL}) OR writer_token!='legacy-import-inert'",
            "foreground_runs": f"agent_checkpoint IS NOT NULL OR status NOT IN ({_TERMINAL}) OR phase!='archived_import'",
            "tasks": f"checkpoint IS NOT NULL OR status NOT IN ({_TERMINAL})",
            "persistent_subtasks": f"checkpoint!='{{}}' OR writer_token IS NOT NULL OR cancel_requested!=1 OR state NOT IN ({_TERMINAL},'blocked')",
            "persistent_subtask_attempts": f"checkpoint!='{{}}' OR writer_token!='legacy-import-inert' OR state NOT IN ({_TERMINAL},'blocked')",
            "library_index_operations": f"prepared IS NOT NULL OR status NOT IN ({_TERMINAL})",
            "service_tool_invocations": "state IN ('prepared','started')",
            "compaction_calls": "status='started'", "resource_reservations": "state IN ('reserved','dispatching','dispatched')",
            "qa_records": "provider_context!='{}'", **{table: f"status NOT IN ({_TERMINAL},'unknown')" for table in _EFFECTS}}
        for table, predicate in checks.items():
            if db.execute(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1").fetchone():
                raise ReleaseError("分离导入副本仍有可执行的旧任务状态")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ReleaseError("分离导入副本完整性或关联约束未通过")
