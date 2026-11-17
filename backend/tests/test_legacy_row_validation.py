"""Semantic old20 copy checks on synthetic values and isolated archives only."""
from contextlib import closing
import json
import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import legacy_cross_schema as cross
from app.legacy_row_validation import validate_row
from app.legacy_schema_contracts import OLD20_COLUMNS
from app.release_info import ReleaseError
from .test_legacy_cross_schema_import import seed, finalize, legacy, setup, CID, ANSWER
from .test_legacy_import import run, review, recover
from .test_legacy_install import files


@pytest.fixture(scope="module")
def declared_types(tmp_path_factory):
    path = tmp_path_factory.mktemp("static-old-row-contract") / "target.db"
    cross.create_target(path)
    with closing(sqlite3.connect(path)) as db:
        return {table: {row[1]: row[2].upper() for row in
                db.execute('PRAGMA table_info("' + table + '")')}
                for table in OLD20_COLUMNS}


def test_all_retained_declared_json_and_datetime_columns_reject_unreadable_values(declared_types):
    checked = {"JSON": 0, "DATETIME": 0}
    for table, columns in OLD20_COLUMNS.items():
        for column in columns:
            kind = declared_types[table][column]
            if kind not in checked:
                continue
            checked[kind] += 1
            for value in (('{"broken":', 9) if kind == "JSON" else ("not-a-date", b"2026-11-01")):
                with pytest.raises(ReleaseError, match=table + r"\." + column):
                    validate_row(table, (column,), (value,), declared_types[table])
    assert checked["JSON"] >= 30 and checked["DATETIME"] >= 30


def test_nonfinite_json_numbers_cannot_reach_current_api_responses(declared_types):
    for value in ('{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}', '{"value":[1e999]}'):
        with pytest.raises(ReleaseError, match="非有限JSON"):
            validate_row("tasks", ("result",), (value,), declared_types["tasks"])


def test_json_strings_and_keys_must_be_utf8_response_encodable(declared_types):
    for value in ('{"answer":"\\ud800"}', '{"answer":"\\udfff"}',
                  '{"\\ud800":"value"}', '{"answer":["\\udfff"]}'):
        with pytest.raises(ReleaseError, match="JSON字符"):
            validate_row("tasks", ("result",), (value,), declared_types["tasks"])
    for value in ('{"answer":"中文 😀"}', '{"answer":"\\ud83d\\ude00"}',
                  '{"中文":"原样保留"}'):
        assert validate_row("tasks", ("result",), (value,), declared_types["tasks"]) is None


def test_current_message_workspace_and_task_shapes_are_checked_without_value_disclosure(declared_types):
    cases = [
        ("qa_records", "workspace", value) for value in (
            "[]", "false", "1", '"private-synthetic-value"', '{"files":null}',
            '{"files":{}}', '{"files":"private-synthetic-value"}', '{"files":[{}]}',
            '{"files":[{"path":null}]}', '{"files":[{"path":9}]}',
            '{"files":["private-synthetic-value"]}')]
    cases += [("foreground_runs", "workspace", '{"files":[null]}')]
    cases += [("qa_records", col, '{"private-synthetic-value":true}')
              for col in ("plan_steps", "plan_images", "claims", "evidence", "trace", "skills")]
    cases += [("qa_records", "usage", '["private-synthetic-value"]')]
    cases += [("tasks", col, '["private-synthetic-value"]') for col in ("payload", "result", "checkpoint")]
    cases += [("tasks", col, '{"private-synthetic-value":true}') for col in ("plan", "events")]
    for table, column, value in cases:
        with pytest.raises(ReleaseError) as error:
            validate_row(table, (column,), (value,), declared_types[table])
        assert "private-synthetic-value" not in str(error.value)


def test_accepted_values_are_not_normalized_or_rewritten(declared_types):
    cases = [
        ("qa_records", "workspace", value) for value in (None, "null", "{}", '{"files":[]}',
            '{ "files" : [ {"path":"Z:/never-open-this-file","extra":"retained"} ] }')]
    cases += [("qa_records", col, value) for col in ("evidence", "claims", "trace", "skills")
              for value in (None, "null", "[]")]
    cases += [("tasks", col, value) for col in ("payload", "result", "checkpoint")
              for value in (None, "null", "{}")]
    cases += [("tasks", col, value) for col in ("plan", "events") for value in (None, "null", "[]")]
    # Historical journal shapes are not recast into today's preferred shapes.
    cases += [("git_operations", "paths", "{}"), ("foreground_runs", "skills", "{}"),
              ("recommendations", "reasons", "{}"), ("qa_records", "read_at", None),
              ("qa_records", "created_at", "2026-11-01T12:00:00.123456+00:00")]
    for table, column, value in cases:
        row = (value,)
        assert validate_row(table, (column,), row, declared_types[table]) is None
        assert row == (value,)


@pytest.mark.parametrize("table,column,value", [
    ("qa_records", "workspace", '{"files":[{}]}'),
    ("qa_records", "workspace", '{"files":null}'),
    ("conversations", "pinned_at", "invalid-optional-date"),
    ("tasks", "payload", '["not-an-object"]'),
    ("tasks", "result", '{"answer":1e999}'),
    ("desktop_state", "value", '{"broken":'),
    ("foreground_runs", "workspace", '{"files":[{"path":null}]}'),
])
def test_unreadable_rows_never_publish_and_recovery_cannot_bypass_validation(
        legacy, monkeypatch, table, column, value):
    s = seed(legacy)
    with closing(sqlite3.connect(s["database"])) as db:
        db.execute('UPDATE "' + table + '" SET "' + column + '"=?', (value,))
        db.commit()
    finalize(s, monkeypatch)
    before_data, before_program = files(s["data"]), files(s["legacy"])
    plan = review(s)
    for operation in (lambda: run(s), lambda: recover(s, plan)):
        with pytest.raises(ReleaseError, match=table + r"\." + column):
            operation()
        assert not s["output"].exists() and not s["new_target"].exists()
        assert files(s["data"]) == before_data and files(s["legacy"]) == before_program


@pytest.mark.parametrize("has_fts", [True, False], ids=["fts", "no-fts"])
def test_valid_history_keeps_original_values_and_loads_actual_current_readers(legacy, monkeypatch, has_fts):
    s = seed(legacy, has_fts)
    workspace = '{ "files" : [{"path":"Z:/never-open-this-file","text":"retained"}], "project_id":"historical" }'
    created = "2026-11-01T12:00:00.123456+00:00"
    result = '{ "answer": "historical result", "completed": true }'
    with closing(sqlite3.connect(s["database"])) as db:
        db.execute("UPDATE qa_records SET workspace=?,created_at=?,read_at=NULL,plan_steps='[]',usage='null'", (workspace, created))
        db.execute("UPDATE tasks SET payload='{}',result=?,events='[]',\"plan\"='null'", (result,))
        db.commit()
    finalize(s, monkeypatch, has_fts)
    before_data, before_program = files(s["data"]), files(s["legacy"])
    assert run(s)["state"] == "committed"
    target = s["output"] / "scholarflow.db"
    with closing(sqlite3.connect(target)) as db:
        assert db.execute("SELECT workspace,created_at,read_at,usage FROM qa_records").fetchone() == (workspace, created, None, "null")
        assert db.execute("SELECT result FROM tasks").fetchone() == (result,)
    engine = sa.create_engine("sqlite:///" + target.as_posix())
    try:
        with Session(engine) as db:
            from app.api.conversations import detail
            from app.models import TaskRecord
            from app.task_runner import task_to_dict
            message = detail(CID, db=db, limit=None, offset=None, anchor=None)["messages"][0]
            assert message["answer"] == ANSWER
            assert message["file_paths"] == ["Z:/never-open-this-file"]
            assert message["created_at"] == created and message["usage"] is None
            task = task_to_dict(db.query(TaskRecord).one())
            assert task["result"] == json.loads(result) and task["status"] == "cancelled"
            assert task["outcome"] == "completed" and not task["executing"]
            json.dumps({"message": message, "task": task}, allow_nan=False)
    finally:
        engine.dispose()
    assert files(s["data"]) == before_data and files(s["legacy"]) == before_program


@pytest.mark.parametrize("value", [float("inf"), -float("inf")], ids=["positive", "negative"])
def test_nonfinite_task_progress_cannot_publish_an_unserializable_response(legacy, monkeypatch, value):
    from fastapi.encoders import jsonable_encoder
    from starlette.responses import JSONResponse
    s = seed(legacy)
    with closing(sqlite3.connect(s["database"])) as db:
        db.execute("UPDATE tasks SET progress=?", (value,))
        db.commit()
    finalize(s, monkeypatch)
    before_data, before_program = files(s["data"]), files(s["legacy"])
    rejected = False
    try:
        run(s)
    except (ReleaseError, ValueError):
        # Non-finite native FLOAT values already fail closed in the earlier
        # immutable-row canonical digest; no new row transform is required.
        rejected = True
        assert not s["output"].exists()
    if not rejected:
        engine = sa.create_engine("sqlite:///" + (s["output"] / "scholarflow.db").as_posix())
        try:
            with Session(engine) as db:
                from app.models import TaskRecord
                from app.task_runner import task_to_dict
                JSONResponse(jsonable_encoder(task_to_dict(db.query(TaskRecord).one())))
        finally:
            engine.dispose()
    assert files(s["data"]) == before_data and files(s["legacy"]) == before_program
