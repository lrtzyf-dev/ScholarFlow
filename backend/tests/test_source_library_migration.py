import json

from sqlalchemy import create_engine, inspect, text
from app import db as database


def test_early_source_import_receipt_gets_empty_map_without_fabricating_locators(tmp_path, monkeypatch):
    path = tmp_path / "old-library.db"
    engine = create_engine(f"sqlite:///{path}")
    database.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE source_library_imports"))
        conn.execute(text("""CREATE TABLE source_library_imports (
            revision_id VARCHAR(32) PRIMARY KEY, raw_sha256 VARCHAR(64) NOT NULL,
            paper_id VARCHAR(32) NOT NULL, request_id VARCHAR(96) NOT NULL, created_at DATETIME NOT NULL)"""))
        conn.execute(text("INSERT INTO source_library_imports VALUES ('original-revision','original-hash','original-paper','original-request',CURRENT_TIMESTAMP)"))
    monkeypatch.setattr(database, "engine", engine)
    try:
        database.init_db()
        database.init_db()
        assert "chunk_locators" in {item["name"] for item in inspect(engine).get_columns("source_library_imports")}
        with engine.connect() as conn:
            row = conn.execute(text("SELECT revision_id, raw_sha256, paper_id, request_id, chunk_locators FROM source_library_imports")).one()
            assert row[:4] == ("original-revision", "original-hash", "original-paper", "original-request")
            assert json.loads(row[4]) == {}
        snapshots = list((path.parent / "backups/schema").glob("*.json"))
        assert len(snapshots) == 1
        assert json.loads(snapshots[0].read_text())["status"] == "migration_succeeded"
    finally:
        engine.dispose()
