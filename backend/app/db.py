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
                  "usage_origin_id": "VARCHAR(32)"}
        for col, decl in qa_add.items():
            if cols and col not in cols:
                conn.execute(text(f"ALTER TABLE qa_records ADD COLUMN {col} {decl}"))
        # 后台任务表补列
        tcols = {r[1] for r in conn.execute(text("PRAGMA table_info(tasks)")).fetchall()}
        task_add = {
            "conversation_id": "VARCHAR(32)", "title": "VARCHAR(512)",
            "plan": "JSON", "events": "JSON", "result": "JSON",
            "updated_at": "DATETIME", "checkpoint": "JSON",
        }
        for col, decl in task_add.items():
            if tcols and col not in tcols:
                conn.execute(text(f"ALTER TABLE tasks ADD COLUMN {col} {decl}"))
        ucols = {r[1] for r in conn.execute(text("PRAGMA table_info(desktop_state)")).fetchall()}
        if ucols and "updated_at" not in ucols:
            conn.execute(text("ALTER TABLE desktop_state ADD COLUMN updated_at DATETIME"))
        fcols = {r[1] for r in conn.execute(text("PRAGMA table_info(foreground_runs)")).fetchall()}
        for col, decl in {"workspace": "JSON", "execution_mode": "VARCHAR(16) NOT NULL DEFAULT 'execute'", "reviewed_plan_id": "VARCHAR(64)", "started_at": "DATETIME"}.items():
            if fcols and col not in fcols:
                conn.execute(text(f"ALTER TABLE foreground_runs ADD COLUMN {col} {decl}"))
        if fcols and "heartbeat_at" not in fcols:
            conn.execute(text("ALTER TABLE foreground_runs ADD COLUMN heartbeat_at DATETIME"))
            conn.execute(text("UPDATE foreground_runs SET heartbeat_at = updated_at WHERE heartbeat_at IS NULL"))


def init_db() -> None:
    """建表（在 import 模型之后调用）。"""
    from . import models  # noqa: F401  确保模型已注册

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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
