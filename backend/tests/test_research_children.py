"""Research children use real SQLite/adapter paths; synthetic HTTP only."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app import research_children as children, research_runs as runs, run_lifecycle, run_store
from app.agent.checkpoint import AgentState
from app.db import SessionLocal
from app.goal_models import Goal
from app.models import Conversation, ForegroundRun
from app.research_models import ResearchChildLink, ResearchChildAttemptLink, ResearchChildRetryGrant, ResearchChildRetryConsumption
from app.runtime_models import RuntimeRun, RunAttempt
from app import source_store as sources
from .test_matrix_runs import seed, api_embeddings, CID, edit


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    runs._stopping.clear()
    yield
    for rid in list(run_store.active_runs()):
        run_store.finish(rid, "interrupted")


def parent(db, *, cid=CID, budget=False, name=None, question="完成当前研究并交付可追溯结果"):
    from app.resource_policy import freeze_budget
    from app.model_profiles import capture_run_config
    if not db.get(Conversation, cid):
        db.add(Conversation(id=cid, title="Research parent")); db.flush()
    ident = name or "fg_" + uuid4().hex
    attempt, _ = run_lifecycle.admit_attempt(db, ident, cid, question, request_id="parent-" + uuid4().hex)
    run = db.get(RuntimeRun, ident)
    binding = {**capture_run_config(db, cid), "approval_mode": "auto",
        "run_id": ident, "conversation_id": cid, "goal_id": run.goal_id, "attempt_id": attempt.id}
    if budget:
        binding["resource_policy"] = {"policy": {"currency": "USD", "amount_limit": "0.1", "request_estimate": "0.1"}}
    binding = freeze_budget(db, run_id=ident, binding=binding)
    state = AgentState(question=question, phase="model", goal_id=run.goal_id,
        conversation_id=cid, runtime_config=binding, attempt_id=attempt.id, writer_token=attempt.writer_token)
    attempt.checkpoint = state.snapshot()
    run_lifecycle.save_checkpoint(db, ident, state.snapshot()); db.commit()
    assert run_store.begin(ident, cid, attempt_id=attempt.id)
    return {**binding, "writer_token": attempt.writer_token}


def arguments(item, request_id="matrix-child-start"):
    return {"kind": "matrix", "request_id": request_id, "inputs": {"matrix_id": item.id,
        "expected_version": item.version, "field_keys": None, "paper_ids": None}}


def pending_checkpoint(db, binding, args, name=children.START):
    attempt = db.get(RunAttempt, binding["attempt_id"], populate_existing=True)
    checkpoint = deepcopy(attempt.checkpoint)
    checkpoint["pending_operation"] = {"key": "parent-tool-stable", "kind": "tool", "name": name, "arguments": args}
    return checkpoint


def newer_parent(db, binding):
    run_store.finish(binding["run_id"], "interrupted", attempt_id=binding["attempt_id"])
    db.expire_all()
    old = db.get(RunAttempt, binding["attempt_id"])
    old_snapshot = deepcopy(old.checkpoint)
    run_lifecycle.finish_attempt(db, binding["run_id"], old.id, old.writer_token, "interrupted")
    run = db.get(RuntimeRun, binding["run_id"])
    new, _ = run_lifecycle.admit_attempt(db, run.id, CID, "继续研究", resume=True,
        request_id="parent-resume-" + uuid4().hex, expected_revision=run.revision, checkpoint=old_snapshot)
    state = AgentState.model_validate(new.checkpoint)
    state.runtime_config = {**state.runtime_config, "attempt_id": new.id}
    run_lifecycle.save_checkpoint(db, run.id, state.snapshot()); db.commit()
    assert run_store.begin(run.id, CID, attempt_id=new.id)
    return {**state.runtime_config, "writer_token": new.writer_token}


def test_parent_writer_is_only_idle_exemption_and_child_creation_is_not_parent_completion(db):
    item, _ = seed(db)
    binding = parent(db)
    args = arguments(item)
    result = children.execute(children.START, args, binding=binding)
    assert result["outcome"] == "completed" and result["delivery_verified"] and result["managed_by_parent"]
    assert result["result"]["applied_version"] == 2
    db.expire_all()
    run = db.get(RuntimeRun, binding["run_id"])
    assert run.outcome == "open" and db.get(Goal, run.goal_id).outcome == "open"
    child = db.get(RuntimeRun, result["run_id"])
    assert child.goal_id != run.goal_id and child.current_attempt_id != binding["attempt_id"]
    attr = db.get(ResearchChildAttemptLink, child.current_attempt_id)
    assert attr.parent_attempt_id == binding["attempt_id"]
    before = deepcopy(db.get(RunAttempt, child.current_attempt_id).checkpoint)
    replay = children.execute(children.START, args, binding=binding)
    assert replay["attempt_id"] == result["attempt_id"] and db.get(RunAttempt, child.current_attempt_id).checkpoint == before
    with pytest.raises(sources.SourceConflict, match="另一组输入"):
        children.execute(children.START, {**args, "inputs": {**args["inputs"], "field_keys": ["method"]}}, binding=binding)


@pytest.mark.parametrize("field", ["writer_token", "attempt_id", "goal_id", "conversation_id"])
def test_forged_or_incomplete_parent_is_rejected_without_child(db, field):
    item, _ = seed(db); binding = parent(db)
    forged = {**binding, field: "forged-identity"}
    with pytest.raises(Exception):
        children.execute(children.START, arguments(item), binding=forged)
    db.expire_all()
    assert list(db.scalars(select(ResearchChildLink))) == []
    missing = deepcopy(binding); missing.pop(field)
    with pytest.raises(Exception): children.execute(children.START, arguments(item), binding=missing)


def test_other_active_writer_cannot_borrow_parent_exemption(db):
    item, _ = seed(db); binding = parent(db)
    other = "fg_" + uuid4().hex
    assert run_store.begin(other, CID)
    with pytest.raises(run_lifecycle.RunConflict): children.execute(children.START, arguments(item), binding=binding)
    run_store.finish(other, "interrupted")
    db.expire_all()
    assert list(db.scalars(select(ResearchChildLink))) == []


def test_known_interruption_new_parent_attempt_resumes_same_child_and_reuses_results(db, monkeypatch):
    item, _ = seed(db); binding = parent(db); args = arguments(item)
    original = runs.Execution.set_payload
    def interrupt(ctx, payload):
        original(ctx, payload)
        if payload.get("progress", {}).get("completed") == 1: raise OSError("Synthetic known interruption")
    monkeypatch.setattr(runs.Execution, "set_payload", interrupt)
    first = children.execute(children.START, args, binding=binding)
    assert first["status"] == "interrupted" and not first["requires_unknown_confirmation"]
    db.expire_all(); old = deepcopy(db.get(RunAttempt, first["attempt_id"]).checkpoint)
    safety = children.resume_safety(db, parent_run_id=binding["run_id"], checkpoint=pending_checkpoint(db, binding, args))
    assert safety["safe"]
    newer = newer_parent(db, binding)
    monkeypatch.setattr(runs.Execution, "set_payload", original)
    with pytest.raises(Exception): children.execute(children.START, args, binding=binding)
    last = children.execute(children.START, args, binding=newer)
    assert last["run_id"] == first["run_id"] and last["attempt_number"] == 2 and last["outcome"] == "completed"
    db.expire_all()
    assert db.get(RunAttempt, first["attempt_id"]).checkpoint == old
    assert db.get(ResearchChildAttemptLink, last["attempt_id"]).parent_attempt_id == newer["attempt_id"]


def test_cancel_after_paid_response_preserves_receipt_and_blocks_next_request(db, monkeypatch):
    from app import cancel_store
    from app.call_ledger import summarize_calls
    item, _ = seed(db); calls = api_embeddings(db, item, monkeypatch); binding = parent(db)
    original = runs.Execution.set_payload
    def cancel_after_one(ctx, payload):
        original(ctx, payload)
        if payload.get("progress", {}).get("completed") == 1: cancel_store.request_cancel(binding["run_id"])
    monkeypatch.setattr(runs.Execution, "set_payload", cancel_after_one)
    value = children.execute(children.START, arguments(item), binding=binding)
    assert len(calls) == 1 and value["status"] == "interrupted" and not value["requires_unknown_confirmation"]
    db.expire_all()
    state = AgentState.model_validate(db.get(RunAttempt, value["attempt_id"]).checkpoint)
    assert len(state.operations) == 1 and value["result"]["progress"]["completed"] == 1
    assert summarize_calls(run_id=binding["run_id"], include_synthetic=True, db=db)["synthetic_call_count"] == 1
    with pytest.raises(Exception): children.execute(children.START, arguments(item), binding=binding)
    assert len(calls) == 1


def test_queued_steering_stops_child_before_second_dispatch_but_next_turn_does_not(db, monkeypatch):
    from app.run_inputs import RunInput
    item, _ = seed(db); calls = api_embeddings(db, item, monkeypatch); binding = parent(db)
    original = runs.Execution.set_payload
    def steer(ctx, payload):
        original(ctx, payload)
        if payload.get("progress", {}).get("completed") == 1:
            with SessionLocal() as writer:
                writer.add(RunInput(input_id="current-steer", run_id=binding["run_id"], conversation_id=CID,
                    kind="steer", content="只分析第一份资料", workspace_revision=0, revision=1, status="queued"))
                writer.commit()
    monkeypatch.setattr(runs.Execution, "set_payload", steer)
    value = children.execute(children.START, arguments(item), binding=binding)
    assert len(calls) == 1 and value["status"] == "interrupted" and not value["requires_unknown_confirmation"]
    db.expire_all(); row = db.get(RunInput, "current-steer"); row.kind = "next_turn"; db.commit()
    monkeypatch.setattr(runs.Execution, "set_payload", original)
    last = children.execute(children.START, arguments(item), binding=binding)
    assert last["outcome"] == "completed" and len(calls) == 2


def test_unknown_paid_call_requires_exact_human_http_grant_and_is_consumed_once(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    item, _ = seed(db); calls = api_embeddings(db, item, monkeypatch, fail_at=1)
    binding = parent(db); args = arguments(item)
    value = children.execute(children.START, args, binding=binding)
    assert value["requires_unknown_confirmation"] and len(calls) == 1
    db.expire_all(); cp = pending_checkpoint(db, binding, args)
    assessment = children.resume_safety(db, parent_run_id=binding["run_id"], checkpoint=cp)
    assert not assessment["safe"] and assessment["reason"] == "unknown_paid_child_requires_human_grant"
    with pytest.raises(sources.SourceError):
        children.execute(children.RESUME, {"child_run_id": value["run_id"], "request_id": "model-bool-retry", "retry_unknown": True}, binding=binding)
    with pytest.raises(sources.SourceConflict): children.execute(children.START, args, binding=binding)
    assert len(calls) == 1
    db.expire_all(); parent_run = db.get(RuntimeRun, binding["run_id"])
    req = {"conversation_id": CID, "parent_run_id": binding["run_id"],
        "expected_parent_revision": parent_run.revision, "expected_parent_attempt_id": binding["attempt_id"],
        "expected_child_attempt_id": value["attempt_id"], "pending_sha256": value["pending_sha256"],
        "request_id": "human-grant-one", "confirm_duplicate_cost": True}
    client = TestClient(app)
    try:
        path = "/api/research/children/" + value["run_id"] + "/retry-grants"
        bad = client.post(path, json={**req, "expected_parent_revision": req["expected_parent_revision"] + 1})
        assert bad.status_code == 409
        missing = {k: v for k, v in req.items() if k != "confirm_duplicate_cost"}
        assert client.post(path, json=missing).status_code == 422
        assert client.post(path, json={**req, "confirm_duplicate_cost": 1}).status_code == 422
        assert client.post(path, json={**req, "confirm_duplicate_cost": False}).status_code == 422
        granted = client.post(path, json=req)
        assert granted.status_code == 200, granted.text
        assert client.post(path, json=req).json()["replayed"]
        assert client.post(path, json={**req, "pending_sha256": "f" * 64}).status_code == 409
        assert len(calls) == 1
    finally: client.close()
    db.expire_all()
    assert children.resume_safety(db, parent_run_id=binding["run_id"], checkpoint=cp)["safe"]
    new_binding = newer_parent(db, binding)
    done = children.execute(children.START, args, binding=new_binding)
    assert done["outcome"] == "completed" and len(calls) == 3
    db.expire_all()
    grants = list(db.scalars(select(ResearchChildRetryGrant)))
    assert len(grants) == 1 and db.get(ResearchChildRetryConsumption, grants[0].id).child_attempt_id == done["attempt_id"]
    assert db.get(RunAttempt, value["attempt_id"]).checkpoint["pending_operation"] is not None


def test_child_budget_and_ledger_are_parent_owned_without_copying_call_records(db, monkeypatch):
    from app.call_ledger import summarize_calls, list_calls
    from app.ledger_models import CallRecord
    from app.resource_models import ResourceBudget
    item, _ = seed(db); calls = api_embeddings(db, item, monkeypatch); binding = parent(db, budget=True)
    result = children.execute(children.START, arguments(item), binding=binding)
    assert len(calls) == 1 and result["status"] == "paused" and not result["requires_unknown_confirmation"]
    db.expire_all()
    link = db.scalar(select(ResearchChildLink))
    child_state = db.get(RunAttempt, result["attempt_id"]).checkpoint
    assert link.resource_budget_id == binding["resource_budget_id"] == child_state["runtime_config"]["resource_budget_id"]
    assert len(list(db.scalars(select(ResourceBudget)))) == 1
    assert len(list(db.scalars(select(CallRecord)))) == 1
    for aid in (None, binding["attempt_id"]):
        summary = summarize_calls(run_id=binding["run_id"], attempt_id=aid, include_synthetic=True, db=db)
        assert summary["synthetic_call_count"] == 1
        records = list_calls(run_id=binding["run_id"], attempt_id=aid, include_synthetic=True, db=db)["items"]
        assert len(records) == 1 and records[0]["run_id"] == result["run_id"]


def test_linked_http_resume_and_apply_cannot_escape_parent_control(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import matrix_run
    item, ids = seed(db); binding = parent(db)
    rank = matrix_run._rank
    changed = False
    def conflict(*a, **k):
        nonlocal changed
        if not changed:
            changed = True
            with SessionLocal() as writer:
                from app import research_store
                edit(writer, research_store.matrix(writer, item.id, CID), ids[0])
        return rank(*a, **k)
    monkeypatch.setattr(matrix_run, "_rank", conflict)
    first = children.execute(children.START, arguments(item), binding=binding)
    assert first["outcome"] == "partial" and first["result"]["apply_conflict"]
    client = TestClient(app)
    try:
        route = "/api/research/matrix-runs/" + first["run_id"]
        assert client.post(route + "/resume", json={"conversation_id": CID, "request_id": "bypass-parent-resume", "expected_revision": first["revision"]}).status_code == 409
        assert client.post(route + "/apply", json={"conversation_id": CID, "request_id": "bypass-parent-apply", "expected_matrix_version": 2}).status_code == 409
    finally: client.close()
    final = children.execute(children.RESUME, {"child_run_id": first["run_id"], "request_id": "parent-merge-one",
        "expected_revision": first["revision"], "expected_matrix_version": 2}, binding=binding)
    assert final["outcome"] == "completed" and final["attempt_number"] == 2


@pytest.mark.parametrize("kind", ["framework", "translation", "quality"])
def test_other_existing_adapters_share_same_bridge_and_preserve_their_real_outcome(db, kind):
    item, ids = seed(db); binding = parent(db)
    inputs = {"framework": {"idea": "Compare methods", "max_sources": 2},
        "translation": {"text": "A research text.", "source": "English", "target": "Chinese", "display_mode": "whole", "source_ref": None},
        "quality": {"paper_id": ids[0]}}[kind]
    value = children.execute(children.START, {"kind": kind, "request_id": "adapter-" + kind, "inputs": inputs}, binding=binding)
    assert value["kind"] == kind and value["parent_run_id"] == binding["run_id"]
    if kind == "translation":
        assert value["outcome"] != "completed" and not value["delivery_verified"]
    elif kind == "framework":
        assert value["outcome"] == "completed" and value["result"]["mode"] == "offline"
        assert value["result"]["semantic_verification"] == "not_performed" and not value["result"]["verified"]
    else:
        assert value["outcome"] == "completed"


def test_verified_delivery_rechecks_identity_hash_fixed_quote_and_namespace(db):
    import json
    from app.agent.loop import _renumber_evidence
    item, _ = seed(db); binding = parent(db)
    start_args = arguments(item)
    value = children.execute(children.START, start_args, binding=binding)
    assert value["evidence"] and value["citation_scope"]["kind"] == "child_run"
    assert all(e["child_citation_id"] == "M1" and not e["child_citation_id_unique"] for e in value["evidence"])
    text_value, counter, evidence = _renumber_evidence(json.dumps(value), 1)
    renamed = json.loads(text_value)
    assert evidence[0]["id"] == "e2" and evidence[0]["child_citation_id"] == "M1"
    assert renamed["result_sha256"] == value["result_sha256"]
    proof = children.verified_delivery(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"], payload=renamed)
    assert proof and proof["completed"] and proof["evidence"][0]["quote"] == evidence[0]["quote"]
    assert children.matches_original_start(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"], arguments=start_args, payload=renamed)
    assert not children.matches_original_start(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"], arguments=arguments(item, "different-start-request"), payload=renamed)
    changed_start = deepcopy(start_args); changed_start["inputs"]["field_keys"] = ["method"]
    assert not children.matches_original_start(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"], arguments=changed_start, payload=renamed)
    for field in ("run_id", "attempt_id", "revision", "result_sha256"):
        bad = {**renamed, field: "forged"}
        assert children.verified_delivery(db, parent_run_id=binding["run_id"], conversation_id=CID,
            child_run_id=value["run_id"], payload=bad) is None
    bad = deepcopy(renamed); bad["evidence"][0]["quote"] += " forged"
    assert children.verified_delivery(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"], payload=bad) is None
    assert children.verified_delivery(db, parent_run_id="other-parent", conversation_id=CID,
        child_run_id=value["run_id"], payload=value) is None


def test_large_result_pages_reassemble_exact_saved_json_without_repeating_inputs(db):
    import json
    item, _ = seed(db); binding = parent(db)
    value = children.execute(children.START, arguments(item), binding=binding)
    offset, chunks = 0, []
    while True:
        page = children.execute(children.READ, {"child_run_id": value["run_id"], "offset": offset, "page_size": 1000}, binding=binding)
        data = page["result"]
        assert len(data["serialized_json_page"]) <= 1000
        chunks.append(data["serialized_json_page"])
        assert children.verified_delivery(db, parent_run_id=binding["run_id"], conversation_id=CID,
            child_run_id=value["run_id"], payload=page)
        if data["next_offset"] is None: break
        offset = data["next_offset"]
    assert json.loads("".join(chunks)) == runs.public(db, value["run_id"], CID)["result"]


def test_matrix_contract_uses_exact_saved_full_coverage_and_rechecks_receipts(db):
    from app import goal_ledger
    item, ids = seed(db); binding = parent(db, question="比较两篇论文并提供研究结果")
    value = children.execute(children.START, arguments(item), binding=binding)
    proof = children.comparison_delivery(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"], payload=value)
    assert proof and proof["source_ids"] == ids and proof["cell_count"] == 4
    assert proof["semantic_support"] == "unverified"
    goal_ledger.record_contract_tool_result(db, binding["goal_id"], children.START, arguments(item), value, "actual-operation")
    snapshot = goal_ledger.evaluate_input_contracts(db, binding["goal_id"], "已经完成结构比较")
    contract = next(r for r in snapshot["requirements"] if (r["tool_arguments"] or {}).get("contract", {}).get("kind") == "compare_source_count")
    assert contract["status"] == "completed"
    evidence = contract["evidence"][0]
    assert evidence["source_ids"] == ids and evidence["research_delivery"]["child_run_id"] == value["run_id"]
    # Changing the user's fixed source scope invalidates the structural proof.
    current = db.get(RunAttempt, binding["attempt_id"])
    changed = deepcopy(current.checkpoint); changed["source_revision_ids"] = [item.document["papers"][0]["revision_id"]]
    current.checkpoint = changed; db.commit()
    assert children.comparison_delivery(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"]) is None
    latest = goal_ledger.evaluate_input_contracts(db, binding["goal_id"], "仍然声称完成")
    assert next(r for r in latest["requirements"] if r["id"] == contract["id"])["status"] == "open"


def test_partial_field_selection_does_not_close_broader_comparison_contract(db):
    from app import goal_ledger
    item, _ = seed(db); binding = parent(db, question="比较两篇论文")
    args = arguments(item); args["inputs"]["field_keys"] = ["method"]
    value = children.execute(children.START, args, binding=binding)
    assert value["outcome"] == "completed"
    assert children.comparison_delivery(db, parent_run_id=binding["run_id"], conversation_id=CID,
        child_run_id=value["run_id"], payload=value) is None
    goal_ledger.record_contract_tool_result(db, binding["goal_id"], children.START, args, value, "partial-selection")
    latest = goal_ledger.evaluate_input_contracts(db, binding["goal_id"], "不能用只提取部分字段代替完整对比")
    assert all(r["status"] != "completed" for r in latest["requirements"] if r["required"])


def test_resume_tool_lost_result_replays_across_parent_attempt_without_stale_revision_failure(db, monkeypatch):
    item, _ = seed(db); binding = parent(db)
    original = runs.Execution.set_payload
    def interrupt(ctx, payload):
        original(ctx, payload)
        if payload.get("progress", {}).get("completed") in (1, 2): raise OSError("Synthetic durable interruption")
    monkeypatch.setattr(runs.Execution, "set_payload", interrupt)
    first = children.execute(children.START, arguments(item), binding=binding)
    args = {"child_run_id": first["run_id"], "request_id": "stable-parent-resume", "expected_revision": first["revision"]}
    second = children.execute(children.RESUME, args, binding=binding)
    assert second["status"] == "interrupted" and second["attempt_number"] == 2
    # A replay in the same parent is idempotent, even after its response was lost.
    replay = children.execute(children.RESUME, args, binding=binding)
    assert replay["attempt_id"] == second["attempt_id"]
    with pytest.raises(sources.SourceConflict, match="固定其他输入"):
        children.execute(children.RESUME, {**args, "expected_revision": second["revision"]}, binding=binding)
    newer = newer_parent(db, binding)
    monkeypatch.setattr(runs.Execution, "set_payload", original)
    last = children.execute(children.RESUME, args, binding=newer)
    assert last["outcome"] == "completed" and last["attempt_number"] == 3
    again = children.execute(children.RESUME, args, binding=newer)
    assert again["attempt_id"] == last["attempt_id"]


def test_run_center_child_routes_to_parent_and_cleanup_removes_only_scoped_links(db):
    from app import run_center, data_cleanup
    from app.models import Paper
    item, ids = seed(db); binding = parent(db)
    value = children.execute(children.START, arguments(item), binding=binding)
    db.expire_all()
    page = run_center.list_runs(db, view="all", limit=100)
    row = next(item for item in page["items"] if item["run_id"] == value["run_id"])
    assert row["parent_run_id"] == binding["run_id"] and row["target_run_id"] == binding["run_id"]
    assert row["target_kind"] == "foreground" and row["research_children_available"]
    preview = data_cleanup.preview(db, CID)
    assert preview["records"]["research_child_links"] == 1 and preview["records"]["research_child_attempt_links"] == 1
    run_store.finish(binding["run_id"], "interrupted", attempt_id=binding["attempt_id"])
    db.rollback()
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    try:
        response = client.delete("/api/conversations/" + CID)
        assert response.status_code == 200, response.text
    finally: client.close()
    db.expire_all()
    assert list(db.scalars(select(ResearchChildLink))) == [] and list(db.scalars(select(ResearchChildAttemptLink))) == []
    assert all(db.get(Paper, pid) is not None for pid in ids)


def grant_for(db, binding, value, request_id="direct-human-grant"):
    db.expire_all()
    run = db.get(RuntimeRun, binding["run_id"])
    grant, _ = children.create_retry_grant(db, child_run_id=value["run_id"], conversation_id=CID,
        parent_run_id=binding["run_id"], expected_parent_revision=run.revision,
        expected_parent_attempt_id=binding["attempt_id"], expected_child_attempt_id=value["attempt_id"],
        pending_sha256=value["pending_sha256"], request_id=request_id)
    grant_id = grant.id; db.commit()
    return grant_id


def test_approved_retry_survives_restart_before_dispatch_without_second_permission(db, monkeypatch):
    item, _ = seed(db); calls = api_embeddings(db, item, monkeypatch, fail_at=1)
    binding = parent(db); args = arguments(item)
    unknown = children.execute(children.START, args, binding=binding)
    grant_id = grant_for(db, binding, unknown)
    execute = children._run
    monkeypatch.setattr(children, "_run", lambda *a: None)
    admitted = children.execute(children.START, args, binding=binding)
    assert len(calls) == 1 and admitted["requires_unknown_confirmation"] and admitted["unknown_retry_approved"]
    assert admitted["attempt_number"] == 2
    with pytest.raises(sources.SourceConflict, match="仍在运行"):
        grant_for(db, binding, admitted, request_id="cannot-approve-queued-dispatch")
    db.rollback()
    runs.recover_startup()
    newer = newer_parent(db, binding)
    monkeypatch.setattr(children, "_run", execute)
    done = children.execute(children.START, args, binding=newer)
    assert done["outcome"] == "completed" and len(calls) == 3 and done["attempt_number"] == 3
    db.expire_all()
    assert len(list(db.scalars(select(ResearchChildRetryConsumption)))) == 1
    assert db.get(ResearchChildRetryConsumption, grant_id).child_attempt_id == admitted["attempt_id"]


def test_new_unknown_after_authorized_retry_cannot_reuse_previous_grant(db, monkeypatch):
    import httpx
    client_class = httpx.Client
    item, _ = seed(db); calls = api_embeddings(db, item, monkeypatch, fail_at=1)
    binding = parent(db); args = arguments(item)
    first = children.execute(children.START, args, binding=binding)
    grant_for(db, binding, first)
    send = client_class.send
    def another_unknown(client, request, *a, **k):
        if len(calls) == 2:
            calls.append(request)
            raise httpx.ReadError("Synthetic later unknown", request=request)
        return send(client, request, *a, **k)
    monkeypatch.setattr(client_class, "send", another_unknown)
    second = children.execute(children.START, args, binding=binding)
    assert len(calls) == 3 and second["requires_unknown_confirmation"] and not second["unknown_retry_approved"]
    assert second["attempt_id"] != first["attempt_id"] and second["pending_sha256"] != first["pending_sha256"]
    with pytest.raises(sources.SourceConflict): children.execute(children.START, args, binding=binding)
    assert len(calls) == 3
    db.expire_all()
    assessment = children.resume_safety(db, parent_run_id=binding["run_id"], checkpoint=pending_checkpoint(db, binding, args))
    assert not assessment["safe"] and assessment["reason"] == "unknown_paid_child_requires_human_grant"


def test_relationships_are_immutable_and_fee_scope_handles_cycles_without_duplicates(db):
    from app.call_ledger import summarize_calls
    item, _ = seed(db); binding = parent(db)
    value = children.execute(children.START, arguments(item), binding=binding)
    db.expire_all()
    link = db.scalar(select(ResearchChildLink))
    link.kind = "quality"
    with pytest.raises(ValueError, match="不可原地修改"): db.flush()
    db.rollback()
    # Corrupted historic relationships cannot create an infinite fee walk.
    db.add(ResearchChildLink(parent_run_id=value["run_id"], child_run_id=binding["run_id"],
        parent_goal_id=value["parent_goal_id"], conversation_id=CID, request_id="synthetic-cycle",
        kind="matrix", input_sha256="f" * 64))
    db.commit()
    scope = children.descendant_call_scope(db, binding["run_id"])
    assert scope == {(binding["run_id"], None), (value["run_id"], None)}
    assert summarize_calls(run_id=binding["run_id"], db=db)["call_count"] == 0


@pytest.mark.parametrize("kind", ["matrix", "quality"])
def test_single_paper_scope_cannot_expand_through_new_research_tools(db, kind):
    item, ids = seed(db); binding = parent(db)
    attempt = db.get(RunAttempt, binding["attempt_id"])
    changed = deepcopy(attempt.checkpoint); changed["paper_id"] = ids[0]
    attempt.checkpoint = changed; db.commit()
    args = arguments(item) if kind == "matrix" else {"kind": "quality", "request_id": "quality-outside-scope", "inputs": {"paper_id": ids[1]}}
    with pytest.raises(Exception, match="单篇|本轮"):
        children.execute(children.START, args, binding=binding)
    db.expire_all()
    assert list(db.scalars(select(ResearchChildLink))) == []


def test_framework_candidates_are_frozen_only_inside_parent_single_paper_scope(db):
    item, ids = seed(db); binding = parent(db)
    attempt = db.get(RunAttempt, binding["attempt_id"])
    changed = deepcopy(attempt.checkpoint); changed["paper_id"] = ids[0]
    attempt.checkpoint = changed; db.commit()
    value = children.execute(children.START, {"kind": "framework", "request_id": "framework-single-scope",
        "inputs": {"idea": "Compare methods", "max_sources": 2}}, binding=binding)
    db.expire_all()
    state = AgentState.model_validate(db.get(RunAttempt, value["attempt_id"]).checkpoint)
    job = runs.descriptor(state)
    assert [row["id"] for row in job["candidate_snapshot"]] == [ids[0]]
    assert {row["id"] for row in job["source_snapshot"]} <= {ids[0]}


def test_text_child_uses_frozen_parent_model_and_production_http_ledger(db, monkeypatch):
    import json
    import httpx
    from app import model_profiles
    from app.llm import APIClient
    from app.runtime_context import current_runtime_config
    from app.translate import translation_run
    from app.call_ledger import summarize_calls
    db.add(Conversation(id=CID, title="Bound text child")); db.commit()
    original = model_profiles.save_profile(db, {"name": "Parent synthetic", "base_url": "https://bridge.synthetic.test/v1",
        "model_id": "Parent-Model-Exact-CASE", "api_key": "synthetic-bridge-parent-key"})
    model_profiles.set_default(db, original.id); db.commit()
    binding = parent(db)
    other = model_profiles.save_profile(db, {"name": "Later default", "base_url": "https://other.synthetic.test/v1",
        "model_id": "Different-Later-Model", "api_key": "synthetic-later-key"})
    model_profiles.set_default(db, other.id); db.commit()
    calls = []
    def transport(request):
        assert str(request.url) == "https://bridge.synthetic.test/v1/chat/completions"
        assert json.loads(request.content)["model"] == "Parent-Model-Exact-CASE"
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Translated unit " + str(len(calls))}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
    client_class = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client_class(*a, **k, transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(translation_run, "get_llm", lambda: APIClient(runtime_config=current_runtime_config(), synthetic=True))
    value = children.execute(children.START, {"kind": "translation", "request_id": "bound-text-child",
        "inputs": {"text": "First paragraph.\n\nSecond paragraph.", "source": "English", "target": "Chinese", "display_mode": "parallel", "source_ref": None}}, binding=binding)
    assert value["outcome"] == "completed" and len(calls) == 2
    assert value["result"]["translation"] == "Translated unit 1\n\nTranslated unit 2"
    assert "text" not in value["inputs"] and value["inputs"]["text_characters"] > 0
    db.expire_all()
    assert summarize_calls(run_id=binding["run_id"], include_synthetic=True, db=db)["synthetic_call_count"] == 2


def test_full_backup_restores_input_links_exact_pending_grant_and_call_identity(db, monkeypatch, tmp_path):
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app import config, data_backup
    from app.db import engine
    from app.ledger_models import CallRecord
    item, _ = seed(db); calls = api_embeddings(db, item, monkeypatch, fail_at=1)
    binding = parent(db); args = arguments(item)
    value = children.execute(children.START, args, binding=binding)
    grant_id = grant_for(db, binding, value)
    db.expire_all(); grant = db.get(ResearchChildRetryGrant, grant_id)
    link_id = grant.link_id
    original_call_id = db.scalar(select(CallRecord.id))
    db.rollback()
    folder = tmp_path / "research-child-source"; folder.mkdir()
    data_backup._database_snapshot(Path(engine.url.database), folder / config.settings.db_name)
    exported = data_backup.create_backup(folder, database=config.settings.db_name, origin_data_dir=config.DATA_DIR)
    target = tmp_path / "research-child-restored"
    staged = data_backup.prepare_restore(target, exported["path"], expected_database=config.settings.db_name)
    data_backup.schedule_restore(target, staged["id"])
    data_backup.apply_pending_restore(target, expected_database=config.settings.db_name)
    restored_engine = create_engine("sqlite:///" + str(target / config.settings.db_name))
    try:
        with Session(restored_engine) as restored:
            actual = children.public(restored, value["run_id"], CID, parent_run_id=binding["run_id"])
            assert actual["pending_sha256"] == value["pending_sha256"] and actual["unknown_retry_approved"]
            assert restored.get(ResearchChildRetryGrant, grant_id).link_id == link_id
            assert restored.get(ResearchChildRetryConsumption, grant_id) is None
            assert restored.get(ResearchChildAttemptLink, value["attempt_id"]).parent_attempt_id == binding["attempt_id"]
            assert restored.get(CallRecord, original_call_id).run_id == value["run_id"]
            assert children.resume_safety(restored, parent_run_id=binding["run_id"],
                checkpoint=pending_checkpoint(restored, binding, args))["safe"]
    finally: restored_engine.dispose()
    assert len(calls) == 1
