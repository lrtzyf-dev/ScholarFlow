"""SQLite + SQLAlchemy，含 FTS5 全文索引。"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def fts_available() -> bool:
    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)"))
            conn.execute(text("DROP TABLE IF EXISTS _fts_probe"))
            conn.commit()
            return True
        except Exception:
            return False


def _lightweight_migrations() -> None:
    """对已存在的旧表补列（create_all 不会给旧表加列）。"""
    with engine.begin() as conn:
        draft_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(source_update_drafts)"))}
        if draft_cols and "kind" not in draft_cols:
            conn.execute(text("ALTER TABLE source_update_drafts ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'upload'"))
        if draft_cols and "local_context" not in draft_cols:
            conn.execute(text("ALTER TABLE source_update_drafts ADD COLUMN local_context JSON"))
        profile_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(model_profiles)"))}
        for column in ("context_window_tokens", "max_output_tokens"):
            if profile_cols and column not in profile_cols:
                # NULL preserves the distinction between old unverified
                # compatibility values and an explicitly stored configuration.
                conn.execute(text(f"ALTER TABLE model_profiles ADD COLUMN {column} INTEGER"))
        source_import_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(source_library_imports)"))}
        if source_import_cols and "chunk_locators" not in source_import_cols:
            conn.execute(text("ALTER TABLE source_library_imports ADD COLUMN chunk_locators JSON NOT NULL DEFAULT '{}'"))
        scols = {r[1] for r in conn.execute(text("PRAGMA table_info(source_capture_receipts)")).fetchall()}
        if scols and "revision_fingerprint" not in scols:
            # An older unbound receipt cannot establish its original capture
            # identity. Leave NULL and require a fresh request instead of
            # blessing whichever revision it happens to reference now.
            conn.execute(text("ALTER TABLE source_capture_receipts ADD COLUMN revision_fingerprint VARCHAR(64)"))
        mcols = {r[1] for r in conn.execute(text("PRAGMA table_info(model_credentials)")).fetchall()}
        if mcols and "legacy_generation" not in mcols:
            conn.execute(text("ALTER TABLE model_credentials ADD COLUMN legacy_generation INTEGER NOT NULL DEFAULT 0"))
        wcols = {r[1] for r in conn.execute(text("PRAGMA table_info(managed_worktrees)")).fetchall()}
        if wcols and "archive_info" not in wcols:
            conn.execute(text("ALTER TABLE managed_worktrees ADD COLUMN archive_info JSON"))
        gcols = {r[1] for r in conn.execute(text("PRAGMA table_info(git_operations)")).fetchall()}
        if gcols and "agent_owner" not in gcols:
            conn.execute(text("ALTER TABLE git_operations ADD COLUMN agent_owner JSON"))
        ccols = {r[1] for r in conn.execute(text("PRAGMA table_info(conversations)")).fetchall()}
        if ccols and "workspace_revision" not in ccols:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN workspace_revision INTEGER NOT NULL DEFAULT 0"))
        if ccols and "project_id" not in ccols:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN project_id VARCHAR(32)"))
        if ccols and "pinned_at" not in ccols:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN pinned_at DATETIME"))
        if ccols and "archived_at" not in ccols:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN archived_at DATETIME"))
        if ccols and "archive_revision" not in ccols:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN archive_revision INTEGER NOT NULL DEFAULT 0"))
        if ccols and "pin_position" not in ccols:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN pin_position INTEGER"))
            rows = conn.execute(text("SELECT id FROM conversations WHERE pinned_at IS NOT NULL "
                                     "ORDER BY pinned_at DESC, id DESC")).all()
            for position, row in enumerate(rows):
                conn.execute(text("UPDATE conversations SET pin_position = :position WHERE id = :id"),
                             {"position": position, "id": row.id})
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(qa_records)")).fetchall()}
        if cols and "read_at" not in cols:
            conn.execute(text("ALTER TABLE qa_records ADD COLUMN read_at DATETIME"))
            # Existing history predates read receipts; only new answers start unread.
            conn.execute(text("UPDATE qa_records SET read_at = CURRENT_TIMESTAMP"))
        if cols and "conversation_id" not in cols:
            conn.execute(text("ALTER TABLE qa_records ADD COLUMN conversation_id VARCHAR(32)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_qa_records_conversation_id "
                              "ON qa_records (conversation_id)"))
        # qa_records 补列：证据快照、技能
        qa_add = {"workspace": "JSON", "evidence": "JSON", "skills": "JSON",
                  "execution_mode": "VARCHAR(16) NOT NULL DEFAULT 'execute'", "plan_run_id": "VARCHAR(96)", "plan_steps": "JSON",
                  "reviewed_plan_id": "VARCHAR(64)",
                  "plan_images": "JSON",
                  "in_memory": "INTEGER DEFAULT 1", "usage": "JSON",
                  "usage_origin_id": "VARCHAR(32)", "runtime_config": "JSON",
                  "provider_context": "JSON NOT NULL DEFAULT '{}'"}
        for col, decl in qa_add.items():
            if cols and col not in cols:
                conn.execute(text(f"ALTER TABLE qa_records ADD COLUMN {col} {decl}"))
        # 后台任务表补列
        tcols = {r[1] for r in conn.execute(text("PRAGMA table_info(tasks)")).fetchall()}
        task_add = {
            "conversation_id": "VARCHAR(32)", "title": "VARCHAR(512)",
            "plan": "JSON", "events": "JSON", "result": "JSON",
            "updated_at": "DATETIME", "checkpoint": "JSON", "runtime_config": "JSON",
        }
        for col, decl in task_add.items():
            if tcols and col not in tcols:
                conn.execute(text(f"ALTER TABLE tasks ADD COLUMN {col} {decl}"))
        icols = {r[1] for r in conn.execute(text("PRAGMA table_info(run_inputs)")).fetchall()}
        input_add = {"runtime_config": "JSON", "workspace": "JSON", "dispatch_options": "JSON",
                     "child_run_id": "VARCHAR(80)", "dispatch_started_at": "DATETIME"}
        for col, decl in input_add.items():
            if icols and col not in icols:
                conn.execute(text(f"ALTER TABLE run_inputs ADD COLUMN {col} {decl}"))
        ucols = {r[1] for r in conn.execute(text("PRAGMA table_info(desktop_state)")).fetchall()}
        if ucols and "updated_at" not in ucols:
            conn.execute(text("ALTER TABLE desktop_state ADD COLUMN updated_at DATETIME"))
        fcols = {r[1] for r in conn.execute(text("PRAGMA table_info(foreground_runs)")).fetchall()}
        for col, decl in {"workspace": "JSON", "execution_mode": "VARCHAR(16) NOT NULL DEFAULT 'execute'", "reviewed_plan_id": "VARCHAR(64)", "started_at": "DATETIME", "agent_checkpoint": "JSON", "runtime_config": "JSON", "display_snapshot": "JSON"}.items():
            if fcols and col not in fcols:
                conn.execute(text(f"ALTER TABLE foreground_runs ADD COLUMN {col} {decl}"))
        if fcols and "heartbeat_at" not in fcols:
            conn.execute(text("ALTER TABLE foreground_runs ADD COLUMN heartbeat_at DATETIME"))
            conn.execute(text("UPDATE foreground_runs SET heartbeat_at = updated_at WHERE heartbeat_at IS NULL"))
        # references 是 SQL 保留字，标识符必须加引号
        rcols = {r[1] for r in conn.execute(text('PRAGMA table_info("references")')).fetchall()}
        if rcols and "page" not in rcols:
            conn.execute(text('ALTER TABLE "references" ADD COLUMN page INTEGER'))
        if rcols and "bbox" not in rcols:
            conn.execute(text('ALTER TABLE "references" ADD COLUMN bbox JSON'))


def init_db() -> None:
    """建表（在 import 模型之后调用）。"""
    from . import models  # noqa: F401  确保模型已注册
    from . import runtime_models, run_inputs, source_models  # noqa: F401  共享执行/输入/来源实体
    from . import library_index_models  # noqa: F401  原子索引代次、付费准备批次与历史原文
    from . import service_tool_models  # noqa: F401  独立工具服务凭证与调用回执
    from . import provider_models  # noqa: F401  私有供应商回传与云文件绑定
    from . import history_models  # noqa: F401  不可变历史修订与分支关系
    from . import run_center_models  # noqa: F401  运行待办已读回执
    from . import product_journey  # noqa: F401  可恢复首启引导状态
    from . import diagnostic_records  # noqa: F401  脱敏诊断与恢复建议
    from . import artifact_models  # noqa: F401  成果清单、不可变版本与复算记录
    from . import resource_models  # noqa: F401  共享预算、请求预留与持久子任务
    from . import mcp_auth_models  # noqa: F401  系统凭证保护的用户 OAuth 授权
    from . import pricing_models  # noqa: F401  不可变价目、调用估算与预算对账
    from . import execution_environments  # noqa: F401  会话本机/容器执行环境配置
    from . import research_models  # noqa: F401  证据矩阵、阅读和图谱修订
    from . import graph_projection  # noqa: F401  全库引用图谱投影及同事务删除失效
    from . import memory_governance_models  # noqa: F401  独立记忆覆盖、撤回墓碑与载入回执
    from . import bibliography_models  # noqa: F401  书目导入预览与连接器回执
    from . import storage_models  # noqa: F401  不含原文的数据清理回执
    from . import table_models  # noqa: F401  固定表格版本的实际复算记录
    from . import quota_models  # noqa: F401  明确账号分组、共享并发及派发路由版本
    from . import timing_models  # noqa: F401  持久确认的任务阶段与请求耗时
    from . import source_update_models  # noqa: F401  待确认更新原件的可恢复暂存
    from .schema_backups import prepare_schema_snapshot, finish_schema_snapshot

    snapshot = prepare_schema_snapshot(engine, Base.metadata)
    try:
        Base.metadata.create_all(engine)
        _lightweight_migrations()
        if fts_available():
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                        "chunk_id UNINDEXED, paper_id UNINDEXED, content, tokenize='unicode61')"
                    )
                )
    except BaseException:
        finish_schema_snapshot(snapshot, succeeded=False)
        raise
    finish_schema_snapshot(snapshot, succeeded=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
