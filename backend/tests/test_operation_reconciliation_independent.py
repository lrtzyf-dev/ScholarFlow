"""Root cross-window review of user declarations on the actual persisted API."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from fastapi.testclient import TestClient

from app.main import app
from app.db import SessionLocal
from app.runtime_models import RuntimeRun
from app.operation_receipts import retry_permission, signature
from tests.test_operation_reconciliation import seed, form, read_state, RID, NAME, ARGS


def test_two_windows_cannot_both_reconcile_the_same_revision(db):
    original = seed(db)
    barrier = Barrier(2)
    def submit(index):
        request = {**form(), "request_id": "independent-window-" + str(index),
            "reference": "Independent window " + str(index) + " checked the external request list"}
        barrier.wait(timeout=10)
        return request, TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json=request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (1, 2)))
    assert sorted(response.status_code for _, response in results) == [200, 409]
    winner, response = next(item for item in results if item[1].status_code == 200)
    saved = read_state(db)
    assert len(saved.tool_failures[0]["reconciliations"]) == 1
    assert saved.operations == original.operations and saved.memory_side_effects == original.memory_side_effects
    # A new HTTP client after the response sees one durable declaration. Losing
    # the response and retrying its original request cannot mint another permit.
    replay = TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json=winner)
    assert replay.status_code == 200 and replay.json()["id"] == response.json()["id"]
    assert len(read_state(db).tool_failures[0]["reconciliations"]) == 1


def test_user_executed_declaration_keeps_unknown_fact_without_success_proof(db):
    before = seed(db)
    reply = TestClient(app).post(f"/api/subtasks/runs/{RID}/operations/reconcile", json=form("executed"))
    assert reply.status_code == 200 and reply.json()["provider_verified"] is False
    db.close()
    with SessionLocal() as reopened:
        state = read_state(reopened)
        logical = reopened.get(RuntimeRun, RID)
        assert logical.status == "interrupted" and logical.revision == 6
        assert state.tool_failures[0]["execution_unknown"] and state.tool_failures[0]["status"] == "open"
        assert not state.tool_failures[0].get("resolution_proof")
        assert state.operations == before.operations and not retry_permission(state, signature(NAME, ARGS))
