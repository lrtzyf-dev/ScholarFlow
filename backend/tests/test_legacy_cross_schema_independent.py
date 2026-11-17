"""Independent old20 importer probes against current readers, synthetic only.

Uses the author archive/signature fixture for the external transaction shell;
all adversarial data, current-reader checks and assertions are independent.
No historical Python, candidate binary, HTTP or real user database is executed.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.release_info import ReleaseError
from .test_legacy_cross_schema_import import seed, finalize, legacy, setup, CID, QID
from .test_legacy_import import run
from .test_legacy_install import files


OUT = Path(__file__).resolve().parents[2] / "verification/legacy-cross-schema-independent"


def write_receipt(name, **fields):
    parent = Path(__file__).resolve().parents[1] / "app"
    keys = [hashlib.sha256((parent / p).read_bytes()).hexdigest()[:12]
        for p in ("legacy_cross_schema.py", "legacy_fixed_sources.py")]
    folder = OUT / "-".join(keys)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name + ".json")
    if target.exists():
        target = folder / (name + "-" + str(len(list(folder.glob(name + "*.json")))) + ".json")
    target.write_text(json.dumps({"synthetic_only": True, "external_http_calls": 0,
        "candidate_executed": False, **fields}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("fault,table,column,value", [
    ("invalid_json_evidence", "qa_records", "evidence", '{"broken":'),
    ("valid_json_wrong_workspace_kind", "qa_records", "workspace", '[1,2]'),
    ("valid_json_wrong_workspace_item", "qa_records", "workspace", '{"files":["not-a-file-record"]}'),
    ("invalid_datetime_qa", "qa_records", "created_at", "not-a-datetime"),
    ("invalid_datetime_conversation", "conversations", "created_at", "not-a-datetime"),
    ("invalid_json_task_result", "tasks", "result", '{"broken":'),
])
def test_committed_migration_must_be_consumable_by_the_actual_current_reader(
        legacy, monkeypatch, fault, table, column, value):
    s = seed(legacy)
    with closing(sqlite3.connect(s["database"])) as db:
        db.execute('UPDATE "' + table + '" SET "' + column + '"=?', (value,));db.commit()
    finalize(s, monkeypatch)
    before_data, before_program = files(s["data"]), files(s["legacy"])
    rejected = False
    try:
        result = run(s)
    except ReleaseError:
        rejected = True
        assert not s["output"].exists()
    assert files(s["data"]) == before_data and files(s["legacy"]) == before_program
    reader_error = None
    if not rejected:
        assert result["state"] == "committed"
        engine = sa.create_engine("sqlite:///" + (s["output"] / "scholarflow.db").as_posix())
        try:
            with Session(engine) as db:
                try:
                    if table == "tasks":
                        from app.models import TaskRecord
                        db.query(TaskRecord).all()
                    else:
                        from app.api.conversations import detail
                        detail(CID, db=db, limit=None, offset=None, anchor=None)
                except Exception as exc:
                    reader_error = type(exc).__name__
        finally:
            engine.dispose()
    write_receipt(fault, mutation={"table": table, "column": column, "value": value},
        migration_rejected=rejected, output_published=s["output"].exists(),
        current_reader_error=reader_error, originals_unchanged=True)
    assert rejected or reader_error is None, (
        "Importer committed history that the current reader cannot open: " + str(reader_error))
