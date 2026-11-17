from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app import resource_policy, resource_budget, model_profiles
from app.models import Conversation, DeletedConversation
from app.resource_models import ResourceBudget
from app.runtime_models import RuntimeRun


def test_defaults_are_opt_in_and_freeze_is_same_session_idempotent(db):
    assert resource_policy.snapshot_policy(db, "c")["policy"]["amount_limit"] is None
    saved = resource_policy.update_policy(db, "c", {"amount_limit": "2", "request_estimate": ".1", "active_seconds_limit": 300}, 0)
    # Existing write transaction: freeze must not open another writer Session.
    binding = {"resource_policy": deepcopy(saved)}
    first = resource_policy.freeze_budget(db, run_id="run", binding=binding)
    second = resource_policy.freeze_budget(db, run_id="run", binding=binding)
    assert first["resource_budget_id"] == second["resource_budget_id"]
    db.commit()
    resource_policy.update_policy(db, "c", {"amount_limit": "5", "request_estimate": ".2"}, 1)
    db.commit()
    still = resource_policy.freeze_budget(db, run_id="run", binding=first)
    assert resource_budget.budget_snapshot(still["resource_budget_id"])["amount_limit"] == "2"
    assert len(db.scalars(select(ResourceBudget)).all()) == 1


def test_frozen_budget_cannot_be_borrowed_from_another_run(db):
    identity = resource_budget.create_budget(owner_run_id="owner", active_seconds_limit=30)
    with pytest.raises(resource_budget.ResourceBudgetError, match="不属于"):
        resource_policy.freeze_budget(db, run_id="other", binding={"resource_budget_id": identity})


def test_capture_freezes_policy_without_opening_nested_budget_transaction(db):
    resource_policy.update_policy(db, "c", {"active_seconds_limit": 60}, 0)
    config = model_profiles.capture_run_config(db, "c")
    assert config["resource_policy"]["policy"]["active_seconds_limit"] == 60
    assert "resource_budget_id" not in config
    assert not db.scalars(select(ResourceBudget)).all()


def test_api_versions_validation_and_conversation_ownership(db):
    db.add_all([Conversation(id="c1", title="one"), Conversation(id="c2", title="two"),
                RuntimeRun(id="run1", kind="foreground", conversation_id="c1")])
    db.commit()
    identity = resource_budget.create_budget(owner_run_id="run1", amount_limit="1", request_estimate=".1", price_version="estimate")
    client = TestClient(app)
    response = client.put("/api/conversation-resources/c1", json={"expected_revision": 0, "active_seconds_limit": 60})
    assert response.status_code == 200
    assert client.put("/api/conversation-resources/c1", json={"expected_revision": 0}).status_code == 409
    assert client.put("/api/conversation-resources/c1", json={"expected_revision": 1, "amount_limit": "1"}).status_code == 422
    first = client.get("/api/conversation-resources/c1").json()
    second = client.get("/api/conversation-resources/c2").json()
    assert first["budgets"][0]["id"] == identity and second["budgets"] == []
    update = {"expected_revision": first["budgets"][0]["revision"], "amount_limit": "2"}
    assert client.put(f"/api/conversation-resources/c2/budgets/{identity}", json=update).status_code == 404
    assert client.put(f"/api/conversation-resources/c1/budgets/{identity}", json=update).status_code == 200
    assert Decimal(resource_budget.budget_snapshot(identity)["amount_limit"]) == 2


def test_deleted_conversation_cannot_access_saved_resources(db):
    db.add(DeletedConversation(id="deleted"))
    db.commit()
    response = TestClient(app).get("/api/conversation-resources/deleted")
    assert response.status_code == 410


@pytest.mark.parametrize("exception_name", ["budget", "context", "cancel"])
def test_planner_audit_attribution_and_memory_do_not_swallow_resource_boundaries(db, exception_name):
    from app.agent import planner, audit, source_attribution
    from app import conversation_memory
    from app.agent.active_context import ContextCapacityExceeded
    from app.request_resources import ResourceRequestCancelled
    from app.runtime_context import bind_runtime_config
    kind = {"budget": resource_budget.ResourceBudgetExceeded, "context": ContextCapacityExceeded, "cancel": ResourceRequestCancelled}[exception_name]
    class Model:
        mode = "api"
        def chat(self, *args, **kwargs):
            raise kind("synthetic boundary")
    model = Model()
    evidence = [{"id": "e1", "quote": "The sample size is 100.", "paper_title": "Study", "page": 1}]
    operations = [lambda: planner.make_plan(db, model, "compare"),
        lambda: audit.audit_answer(db, "The sample size is 100[^e1].", evidence, model),
        lambda: audit.review_citation_scope("q", "The sample size is 100.", evidence,
            {"unsupported": ["The sample size is 100."], "total_claims": 1, "marked_claims": 0}, model),
        lambda: source_attribution.review_source_attribution("q", "The file says the sample size is 100.",
            [{"role": "user", "content": "Use the uploaded file."}], model),
        lambda: conversation_memory._summarize(db.get_bind(), "c", "", [("record", "history")], model, "run")]
    with bind_runtime_config({"mode": "api"}):
        for operation in operations:
            with pytest.raises(kind):
                operation()
