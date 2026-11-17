"""Unmutated controls for independent old20 current-reader adversaries."""
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from .test_legacy_cross_schema_import import seed, finalize, legacy, setup, CID
from .test_legacy_import import run


@pytest.mark.parametrize("reader", ["conversation_detail", "task_model"])
def test_unchanged_author_seed_is_consumable_by_each_real_current_reader(legacy, monkeypatch, reader):
    s = finalize(seed(legacy), monkeypatch)
    assert run(s)["state"] == "committed"
    engine = sa.create_engine("sqlite:///" + (s["output"] / "scholarflow.db").as_posix())
    try:
        with Session(engine) as db:
            if reader == "conversation_detail":
                from app.api.conversations import detail
                result = detail(CID, db=db, limit=None, offset=None, anchor=None)
                assert result["messages"] and result["messages"][0]["answer"]
            else:
                from app.models import TaskRecord
                records = db.query(TaskRecord).all()
                assert records and records[0].status == "cancelled"
    finally:
        engine.dispose()
