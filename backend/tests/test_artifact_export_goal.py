"""Fixed exported bytes, rather than a saved draft, satisfy export criteria."""
import pytest
from uuid import uuid4
from copy import deepcopy
from hashlib import sha256
from sqlalchemy import update, delete
from fastapi import HTTPException

from app import goal_contracts, goal_ledger, goal_receipts
from app.agent import planner


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_export_contract_can_require_a_file_without_unrelated_terms():
    spec = goal_contracts.normalize({"kind": "artifact_export_result", "format": "docx"})
    assert spec["format"] == "docx"
    assert spec["contains"] == []


def test_downloadable_export_does_not_require_a_project_file():
    plan = planner.add_acceptance_requirements([], "请生成 DOCX 文件供下载。")
    assert not any(step.get("acceptance") == "project_file" for step in plan)


def test_export_tool_is_published_as_a_guarded_write():
    from app.artifact_tools import ARTIFACT_TOOLS, ARTIFACT_READ_TOOLS
    from app.agent.tools import SAFE_TOOLS
    assert "export_research_artifact" in ARTIFACT_TOOLS
    assert "export_research_artifact" not in ARTIFACT_READ_TOOLS | SAFE_TOOLS


def setup_goal(db, spec=None, *, question="请说明方法。", cid="export-goal-a", run_id="export-goal-run"):
    from app.models import Conversation
    from app import run_lifecycle
    from app.runtime_models import RuntimeRun
    db.add(Conversation(id=cid, title="导出验收")); db.flush()
    attempt, _ = run_lifecycle.admit_attempt(db, run_id, cid, question)
    run = db.get(RuntimeRun, run_id)
    goal = goal_ledger.snapshot_goal(db, run.goal_id)
    if spec:
        goal = goal_contracts.user_change(db, goal["id"], expected_revision=goal["revision"],
            request_id=uuid4().hex, action="add", title="固定文件交付", contract=spec)
    db.commit()
    return goal, {"conversation_id": cid, "run_id": run_id, "goal_id": goal["id"],
        "attempt_id": attempt.id, "writer_token": attempt.writer_token}


def source(db, cid="export-goal-a", analysis=False):
    from app import artifact_store
    return artifact_store.create(db, request_id=uuid4().hex, conversation_id=cid, title="验收成果",
        kind="analysis" if analysis else "document", content="name,value\nA,1\nB,\nC,3\n" if analysis else "# 方法\n保留局限。\n\n| 组别 | 数值 |\n| --- | --- |\n| A | 1 |",
        evidence=[], analysis={"category_column": "name", "numeric_columns": ["value"],
            "chart_type": "line", "units": {"value": "kg"}} if analysis else {})


def tool_export(db, item, binding, *, format="docx", request_id=None):
    from app.artifact_tools import execute
    from app.runtime_context import bind_runtime_config
    with bind_runtime_config(binding):
        return execute(db, "export_research_artifact", {"artifact_id": item["id"],
            "revision_id": item["current_revision_id"], "request_id": request_id or uuid4().hex,
            "format": format}, conversation_id=binding["conversation_id"])


def requirement(db, goal):
    return next(row for row in goal_ledger.snapshot_goal(db, goal["id"])["requirements"]
        if (row["tool_arguments"] or {}).get("result_contract", {}).get("kind") == "artifact_export_result"
        and row["status"] not in ("cancelled", "superseded"))


def record(db, goal, result, name="export_research_artifact"):
    goal_ledger.record_contract_tool_result(db, goal["id"], name, {}, result, uuid4().hex)


@pytest.mark.parametrize("question,format", [("请生成 DOCX 文件供下载。", "docx"), ("生成DOCX文件。", "docx"),
    ("请导出 XLSX。", "xlsx"), ("生成复算包。", "bundle"), ("Please export SVG.", "svg")])
def test_clear_export_is_registered_with_original_unicode_span(db, question, format):
    goal, _ = setup_goal(db, question=question)
    row = requirement(db, goal)
    assert row["tool_arguments"]["result_contract"]["format"] == format
    span = row["tool_arguments"]["source_span"]
    assert question[span["start"]:span["end"]] == span["quote"]
    assert row["status"] == "open"
    assert not any(s.get("acceptance") == "project_file" for s in planner.add_acceptance_requirements([], question))


@pytest.mark.parametrize("question", ["> 请导出 DOCX。", "“请生成DOCX文件”是什么意思？", "如果批准后生成 DOCX。",
    "请生成 DOCX 吗？", "不要生成 DOCX 文件。", "请写入项目 reports/report.docx。"])
def test_quoted_conditional_negative_or_project_text_does_not_create_download_contract(db, question):
    goal, _ = setup_goal(db, question=question)
    assert not any((r["tool_arguments"] or {}).get("result_contract", {}).get("kind") == "artifact_export_result"
        for r in goal["requirements"])


def test_explicit_project_destination_keeps_existing_obligation():
    plan = planner.add_acceptance_requirements([], "请生成 DOCX 文件并写入项目 reports/report.docx。")
    assert any(s.get("acceptance") == "project_file" for s in plan)


@pytest.mark.parametrize("question", ["请生成DOCX文件，并生成 PDF 文件。", "请生成 DOCX 和 PDF 文件。",
    "请生成 DOCX 文件，同时创建项目文档。"])
def test_download_contract_does_not_erase_other_file_obligations(question):
    assert any(s.get("acceptance") == "project_file" for s in planner.add_acceptance_requirements([], question))


@pytest.mark.parametrize("extra", [{"format": "pdf"}, {"requires_recorded_execution": 1},
    {"requires_recorded_execution": True}, {"revision_id": "a" * 32}, {"passed": True},
    {"artifact_id": "other"}, {"min_fixed_sources": True}, {"origin_goal_id": "forged"}])
def test_invalid_export_contract_is_rejected(extra):
    with pytest.raises(goal_ledger.GoalError):
        goal_contracts.normalize({"kind": "artifact_export_result", "format": "docx", **extra})


def test_saved_draft_and_forged_file_claims_cannot_satisfy_export(db):
    goal, binding = setup_goal(db, {"kind": "artifact_export_result", "format": "docx", "contains": ["局限"]})
    item = source(db)
    for name in ("save_research_artifact", "propose_research_edit", "export_research_artifact"):
        record(db, goal, {"artifact_id": item["id"], "revision_id": item["current_revision_id"],
            "export_id": "a" * 32, "passed": True, "payload_sha256": "f" * 64}, name)
    assert requirement(db, goal)["status"] == "open"
    exported = tool_export(db, item, binding)
    record(db, goal, {"export_id": exported["export_id"], "passed": False, "payload_size": -10})
    row = requirement(db, goal)
    assert row["status"] == "completed"
    assert row["evidence"][-1]["payload_sha256"] == exported["payload_sha256"]
    assert row["evidence"][-1]["semantic_verification"] == "not_assessed"


def test_http_created_or_other_run_export_never_borrows_goal_identity(db, client):
    from app import artifact_exports
    goal, binding = setup_goal(db, {"kind": "artifact_export_result", "format": "docx"})
    item = source(db)
    result = client.post(f"/api/artifacts/{item['id']}/exports", json={"conversation_id": binding["conversation_id"],
        "revision_id": item["current_revision_id"], "request_id": uuid4().hex, "format": "docx"})
    assert result.status_code == 200, result.text
    record(db, goal, result.json())
    assert requirement(db, goal)["status"] == "open"
    other = artifact_exports.create(db, artifact_id=item["id"], conversation_id=binding["conversation_id"],
        revision_id=item["current_revision_id"], request_id=uuid4().hex, format="docx",
        origin_goal_id="other-goal", origin_run_id="other-run")
    record(db, goal, other)
    assert requirement(db, goal)["status"] == "open"


@pytest.mark.parametrize("change", ["tamper", "delete"])
def test_final_recheck_reopens_invalid_file_and_preserves_evidence(db, change):
    from app.artifact_models import ArtifactExport
    goal, binding = setup_goal(db, {"kind": "artifact_export_result", "format": "docx"})
    exported = tool_export(db, source(db), binding)
    record(db, goal, exported)
    before = deepcopy(requirement(db, goal)["evidence"])
    if change == "tamper":
        db.execute(update(ArtifactExport).where(ArtifactExport.id == exported["export_id"]).values(payload=b"forged"))
    else:
        db.execute(delete(ArtifactExport).where(ArtifactExport.id == exported["export_id"]))
    db.flush(); db.expire_all()
    goal_receipts.evaluate(db, goal["id"], "文件已完成")
    row = requirement(db, goal)
    assert row["status"] == "open" and row["evidence"][:len(before)] == before
    assert row["evidence"][-1]["reference_unavailable"]


def test_another_valid_matching_file_survives_one_corrupt_receipt(db):
    from app.artifact_models import ArtifactExport
    goal, binding = setup_goal(db, {"kind": "artifact_export_result", "format": "docx"})
    item = source(db)
    first, second = tool_export(db, item, binding), tool_export(db, item, binding)
    record(db, goal, first); record(db, goal, second)
    db.execute(delete(ArtifactExport).where(ArtifactExport.id == first["export_id"]))
    db.flush(); goal_receipts.evaluate(db, goal["id"], "正文不替代文件")
    assert requirement(db, goal)["status"] == "completed"


def test_requires_real_computation_does_not_accept_bundle_alone(db):
    from app.artifact_compute import recompute
    from app import artifact_store
    goal, binding = setup_goal(db, {"kind": "artifact_export_result", "format": "bundle", "requires_recorded_execution": True})
    item = source(db, analysis=True)
    record(db, goal, tool_export(db, item, binding, format="bundle"))
    assert requirement(db, goal)["status"] == "open"
    row = artifact_store.get(db, item["id"], binding["conversation_id"])
    recompute(db, row, artifact_store.revision(db, row, item["current_revision_id"]))
    record(db, goal, tool_export(db, item, binding, format="bundle"))
    assert requirement(db, goal)["status"] == "completed"


@pytest.mark.parametrize("rule", ["no_file_read", "no_file_write", "no_commands", "no_external_tools"])
def test_export_respects_active_goal_policy(db, rule):
    from app.goal_policy import GoalPolicyChanged
    goal, binding = setup_goal(db)
    item = source(db)
    goal_contracts.user_change(db, goal["id"], expected_revision=goal["revision"], request_id=uuid4().hex,
        action="add", title="持续限制", contract={"kind": "policy", "rule": rule})
    db.commit()
    with pytest.raises(GoalPolicyChanged): tool_export(db, item, binding)


def test_actual_http_file_bytes_are_stable_and_scoped_without_generation_on_get(db, client, monkeypatch):
    from app import artifact_export_formats
    from app.models import Conversation
    goal, binding = setup_goal(db)
    item = source(db)
    exported = tool_export(db, item, binding)
    db.add(Conversation(id="other-export-scope", title="其他会话")); db.commit()
    def forbidden(*args, **kwargs): pytest.fail("GET generated a new file")
    monkeypatch.setattr(artifact_export_formats, "render", forbidden)
    contents = []
    for _ in range(2):
        response = client.get(exported["download_url"])
        assert response.status_code == 200, response.text
        assert response.headers["x-export-sha256"] == sha256(response.content).hexdigest() == exported["payload_sha256"]
        assert int(response.headers["content-length"]) == len(response.content) == exported["payload_size"]
        contents.append(response.content)
    assert contents[0] == contents[1]
    url = f"/api/artifacts/{item['id']}/exports/{exported['export_id']}/download?conversation_id=other-export-scope"
    assert client.get(url).status_code == 404
    assert client.post(f"/api/artifacts/{item['id']}/exports", json={"revision_id": item["current_revision_id"],
        "request_id": uuid4().hex, "format": "docx", "conversation_id": binding["conversation_id"],
        "origin_goal_id": goal["id"]}).status_code == 422


def test_new_export_stays_out_of_plan_history_and_project_bound_tool_sets():
    from app.execution_mode import PLAN_READ_TOOLS
    from app.history_revisions import ANSWER_READ_TOOLS
    from app.agent.tool_availability import PROJECT_BOUND_TOOLS
    assert "export_research_artifact" not in PLAN_READ_TOOLS | ANSWER_READ_TOOLS | PROJECT_BOUND_TOOLS


def test_repeated_finalization_cannot_keep_a_completed_goal_after_export_is_deleted(db):
    from app import artifact_exports
    from app.artifact_models import ArtifactExport
    goal, binding = setup_goal(db, {"kind": "artifact_export_result", "format": "docx"})
    item = source(db)
    exported = artifact_exports.create(db, artifact_id=item["id"], conversation_id=binding["conversation_id"],
        revision_id=item["current_revision_id"], format="docx", request_id=uuid4().hex,
        origin_run_id=binding["run_id"], origin_goal_id=goal["id"])
    record(db, goal, exported)
    assert goal_ledger.finalize_goal(db, goal["id"])["outcome"] == "completed"
    db.execute(delete(ArtifactExport).where(ArtifactExport.id == exported["export_id"]))
    db.flush()
    checked = goal_ledger.finalize_goal(db, goal["id"])
    assert checked["outcome"] == "partial"
    assert requirement(db, goal)["status"] == "open"


@pytest.mark.parametrize("control", ["pause", "no_write", "stop_attempt"])
def test_authorization_changes_during_generation_prevent_committing_export(db, monkeypatch, control):
    from app import artifact_export_formats, run_lifecycle
    from app.artifact_models import ArtifactExport
    from app.db import SessionLocal
    from app.goal_policy import GoalPolicyChanged
    goal, binding = setup_goal(db)
    item = source(db)
    render = artifact_export_formats.render
    def interrupt_after_render(*args, **kwargs):
        payload = render(*args, **kwargs)
        with SessionLocal() as other:
            current = goal_ledger.snapshot_goal(other, goal["id"])
            if control == "pause":
                goal_ledger.revise_goal(other, goal["id"], expected_revision=current["revision"],
                    action="pause", source_message_id="actual-user-pause")
            elif control == "no_write":
                goal_contracts.user_change(other, goal["id"], expected_revision=current["revision"],
                    request_id=uuid4().hex, action="add", title="停止写入", contract={"kind": "policy", "rule": "no_file_write"})
            else:
                run_lifecycle.finish_attempt(other, binding["run_id"], binding["attempt_id"], binding["writer_token"], "cancelled")
            other.commit()
        return payload
    monkeypatch.setattr(artifact_export_formats, "render", interrupt_after_render)
    with pytest.raises((GoalPolicyChanged, run_lifecycle.StaleAttempt)):
        tool_export(db, item, binding)
    assert db.query(ArtifactExport).count() == 0


def test_history_answer_revision_blocks_actual_export_dispatch(db):
    import json
    from app.agent.tools import execute_tool
    from app.runtime_context import bind_runtime_config
    from app.artifact_models import ArtifactExport
    goal, binding = setup_goal(db)
    item = source(db)
    with bind_runtime_config({**binding, "history_revision": {"mode": "answer_only"}}):
        result = json.loads(execute_tool(db, "export_research_artifact", {"artifact_id": item["id"],
            "revision_id": item["current_revision_id"], "request_id": uuid4().hex, "format": "docx"}))
    assert result["blocked_by_history_revision"]
    assert db.query(ArtifactExport).count() == 0


@pytest.mark.parametrize("mode", ["requirement", "whole_goal"])
def test_explicit_human_confirmation_keeps_its_original_authority(db, mode):
    goal, binding = setup_goal(db, {"kind": "artifact_export_result", "format": "docx"})
    if mode == "requirement":
        goal_contracts.user_change(db, goal["id"], expected_revision=goal["revision"], request_id=uuid4().hex,
            action="confirm", requirement_id=requirement(db, goal)["id"], reason="用户已核对实际交付")
    else:
        goal_ledger.revise_goal(db, goal["id"], expected_revision=goal["revision"], action="confirm_complete",
            source_message_id="actual-user-confirm")
    assert goal_ledger.finalize_goal(db, goal["id"])["outcome"] == "completed"
