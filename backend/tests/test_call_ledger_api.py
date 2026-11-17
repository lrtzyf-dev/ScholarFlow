"""Viewing usage never executes work or leaks another conversation's calls."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.call_ledger import router
from app.call_ledger import begin_call, finish_call
from app.models import Conversation, DeletedConversation, ForegroundRun
from app.runtime_models import RunAttempt

app = FastAPI()
app.include_router(router)
client = TestClient(app)
RUN = "chat_ledger_12345678"
CONVERSATION = "ledger_conversation"


def seed(db):
    db.add(Conversation(id=CONVERSATION))
    db.add(Conversation(id="ledger_other_conversation"))
    db.add(ForegroundRun(id=RUN, conversation_id=CONVERSATION, question="example"))
    db.add(RunAttempt(id="ledger_attempt", run_id=RUN, number=1, writer_token="test"))
    db.add(RunAttempt(id="other_attempt", run_id="chat_other_12345678", number=1, writer_token="other"))
    for index, synthetic in enumerate((False, False, True)):
        call = begin_call(provider="test.invalid", service="chat", run_id=RUN,
                          attempt_id="ledger_attempt", model_id="Mixed Case (test)",
                          endpoint="https://test.invalid/v1/chat/completions?api_key=private-query",
                          call_id=f"ledger_call_{index}", synthetic=synthetic, db=db)
        finish_call(call, usage={"total_tokens": 10} if index != 1 else None, db=db)
    db.commit()


def request(**params):
    return client.get(f"/api/chat/runs/{RUN}/calls", params={"conversation_id": CONVERSATION, **params})


def test_read_pagination_and_summary_remain_scoped(db):
    seed(db)
    first = request(limit=1)
    assert first.status_code == 200
    value = first.json()
    assert len(value["items"]) == 1
    assert value["has_more"] and value["next_offset"] == 1
    assert value["summary"]["real_call_count"] == 2
    assert value["summary"]["unknown_usage_calls"] == 1
    assert value["summary"]["amount_complete"] is False
    assert value["summary"]["unknown_amount_calls"] == 2
    second = request(limit=1, offset=1).json()
    assert second["items"][0]["id"] != value["items"][0]["id"]
    assert second["next_offset"] is None
    all_calls = request(include_synthetic=True).json()
    assert all_calls["summary"]["real_call_count"] == 2
    assert all_calls["summary"]["synthetic_call_count"] == 1
    assert all_calls["summary"]["synthetic_usage"] == {"total_tokens": 10}
    assert all_calls["summary"]["actual_usage"] == {"total_tokens": 10}
    assert "private-query" not in json.dumps(all_calls)
    assert request(attempt_id="ledger_attempt").json()["summary"]["real_call_count"] == 2
    assert db.get(ForegroundRun, RUN).started_at is None


def test_cross_conversation_and_attempt_are_not_readable(db):
    seed(db)
    assert request(conversation_id="ledger_other_conversation").status_code == 404
    assert request(attempt_id="other_attempt").status_code == 404
    assert request(attempt_id="missing").status_code == 404
    db.add(DeletedConversation(id=CONVERSATION))
    db.commit()
    assert request().status_code == 410


@pytest.mark.parametrize("params", [{"limit": 201}, {"limit": 0}, {"offset": -1}, {"offset": 1_000_001}])
def test_read_boundaries(params):
    assert request(**params).status_code == 422
