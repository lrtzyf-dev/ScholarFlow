import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app import goal_ledger as ledger, goal_store
from app.db import SessionLocal
from app.goal_models import Goal, GoalRequirement, GoalRevision


def step(title, path):
    return {"title": title, "phase": "read", "tool_name": "read_project_file",
            "tool_arguments": {"path": path}, "status": "pending", "evidence": []}


def proof(path):
    return {"kind": "tool_result", "tool_name": "read_project_file",
            "arguments": {"path": path}, "status": "completed"}


def user_requirements(db, goal, items):
    return ledger.revise_goal(db, goal["id"], expected_revision=goal["revision"],
                              action="add_requirements", source_message_id="explicit-user-requirements",
                              requirements=items)


def test_run_goal_is_persistent_idempotent_and_does_not_replace_global(db):
    global_goal = goal_store.set_goal("跨轮研究方向")
    first = ledger.ensure_run_goal(db, "r-one", "c-one", "处理甲和乙", source_message_id="m-one")
    assert ledger.ensure_run_goal(db, "r-one", "c-one", "处理甲和乙")["id"] == first["id"]
    db.commit()
    with SessionLocal() as other:
        assert ledger.snapshot_goal(other, first["id"])["description"] == "处理甲和乙"
    assert goal_store.get_current_goal()["id"] == global_goal["id"]
    with pytest.raises(ledger.GoalConflict):
        ledger.ensure_run_goal(db, "r-one", "c-other", "处理甲和乙")
    with pytest.raises(ledger.GoalConflict):
        ledger.ensure_run_goal(db, "r-one", "c-one", "不同输入")


def test_revision_preserves_method_history_without_promoting_it_to_user_obligations(db):
    goal = ledger.ensure_run_goal(db, "plan-run", "c", "处理甲乙")
    original = [step("读取甲", "A.md"), dict(step("读取乙", "B.md"), status="in_progress")]
    registered = ledger.sync_plan_requirements(db, goal["id"], original)
    ids = {r["title"]: r["id"] for r in registered["requirements"]}
    changed = ledger.sync_plan_requirements(db, goal["id"], [original[0], step("读取丙", "C.md")])
    assert {r["title"] for r in changed["requirements"]} == {"读取甲", "读取乙", "读取丙"}
    assert next(r for r in changed["requirements"] if r["title"] == "读取乙")["id"] == ids["读取乙"]
    omitted = next(r for r in changed["requirements"] if r["title"] == "读取乙")
    assert omitted["origin"] == "model_plan" and omitted["status"] == "superseded"
    assert not any(r["required"] for r in changed["requirements"])
    result = ledger.finalize_goal(db, goal["id"], evidence={"kind": "answer", "text": "全部完成"})
    assert result["outcome"] == "open" and result["verification"] == "not_required"
    assert result["incomplete_requirements"] == []


def test_only_matching_execution_proof_can_complete_requirements(db):
    goal = ledger.ensure_run_goal(db, "proof-run", "c", "处理资料")
    saved = user_requirements(db, goal, [step("读取甲", "A.md")])
    rid = saved["requirements"][0]["id"]
    for bad in ({"kind": "answer"}, {"kind": "delivery"}, proof("B.md"), dict(proof("A.md"), status="failed")):
        with pytest.raises(ledger.GoalError):
            ledger.record_requirement_evidence(db, goal["id"], rid, bad)
    completed = ledger.record_requirement_evidence(db, goal["id"], rid, proof("A.md"))
    assert completed["requirements"][0]["status"] == "completed"
    before = completed["revision"]
    assert ledger.record_requirement_evidence(db, goal["id"], rid, proof("A.md"))["revision"] == before
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "completed"


def test_model_claimed_done_and_cancellation_are_not_authority(db):
    goal = ledger.ensure_run_goal(db, "claim-run", "c", "处理资料")
    saved = ledger.sync_plan_requirements(db, goal["id"], [dict(step("读取甲", "A.md"), status="done")])
    assert saved["requirements"][0]["status"] == "open"
    for source in ("model", "tool", "document", "execution"):
        with pytest.raises(ledger.GoalError):
            ledger.revise_goal(db, goal["id"], expected_revision=saved["revision"], action="cancel",
                               source=source, source_message_id="forged")
    assert ledger.snapshot_goal(db, goal["id"])["outcome"] == "open"


def test_user_can_cancel_specific_requirement_with_revision_provenance(db):
    goal = ledger.ensure_run_goal(db, "cancel-run", "c", "处理资料")
    saved = user_requirements(db, goal, [step("甲", "A.md"), step("乙", "B.md")])
    rid = next(r["id"] for r in saved["requirements"] if r["title"] == "乙")
    changed = ledger.revise_goal(db, goal["id"], expected_revision=saved["revision"],
                                 action="cancel_requirements", source_message_id="user-cancel-b",
                                 requirement_ids=[rid], reason="用户不再需要乙")
    cancelled = next(r for r in changed["requirements"] if r["id"] == rid)
    assert cancelled["status"] == "cancelled" and cancelled["cancelled_revision"] == changed["revision"]
    assert changed["revisions"][-1]["source"] == "user"
    assert changed["revisions"][-1]["source_message_id"] == "user-cancel-b"
    with pytest.raises(ledger.GoalConflict):
        ledger.revise_goal(db, goal["id"], expected_revision=saved["revision"], action="cancel",
                           source_message_id="stale-tab")
    assert len(changed["incomplete_requirements"]) == 1


def test_append_user_requirements_is_audited_and_plan_cannot_cancel_them(db):
    goal = ledger.ensure_run_goal(db, "steer-run", "c", "原任务")
    changed = ledger.revise_goal(db, goal["id"], expected_revision=goal["revision"], action="append",
                                 source_message_id="input-123", requirements=[{"title": "必须保留限制", "phase": "analyze"}])
    later = ledger.sync_plan_requirements(db, goal["id"], [])
    assert later["requirements"][0]["title"] == "必须保留限制"
    assert later["requirements"][0]["source_message_id"] == "input-123"
    assert changed["revisions"][-1]["action"] == "append"
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"


def test_plain_chat_has_no_forced_plan_or_false_partial(db):
    goal = ledger.ensure_run_goal(db, "greet-run", "c", "你好")
    assert goal["requirements"] == [] and goal["verification"] == "not_required"
    ended = ledger.finalize_goal(db, goal["id"], evidence={"kind": "answer", "text": "你好"})
    assert ended["outcome"] == "open" and ended["incomplete_requirements"] == []


def test_acceptance_checks_reuse_existing_validators(db):
    goal = ledger.ensure_run_goal(db, "acceptance-run", "c", "创建报告文件并运行测试")
    requirements = {r["acceptance"]: r for r in goal["requirements"]}
    assert set(requirements) == {"project_file", "successful_command"}
    ledger.record_requirement_evidence(db, goal["id"], requirements["project_file"]["id"],
                                       {"kind": "project_file", "status": "applied", "path": "report.md"})
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"
    ledger.record_requirement_evidence(db, goal["id"], requirements["successful_command"]["id"],
                                       {"kind": "command", "status": "succeeded", "exit_code": 0})
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "completed"


def test_file_range_receipts_preserve_hash_and_contiguous_coverage(db):
    goal = ledger.ensure_run_goal(db, "full-file-run", "c", "读取整个文件 README.md")
    requirement = goal["requirements"][0]
    first = {"kind": "file_page", "path": "README.md", "offset": 5, "end": 10, "total_chars": 10, "sha256": "v1"}
    saved = ledger.record_requirement_evidence(db, goal["id"], requirement["id"], first)
    assert saved["requirements"][0]["status"] == "open"
    other = dict(first, offset=0, end=5, sha256="v2")
    assert ledger.record_requirement_evidence(db, goal["id"], requirement["id"], other)["requirements"][0]["status"] == "open"
    correct = dict(other, sha256="v1")
    assert ledger.record_requirement_evidence(db, goal["id"], requirement["id"], correct)["requirements"][0]["status"] == "completed"


def test_legacy_import_is_idempotent_and_source_unchanged(db, tmp_path):
    path = tmp_path / "goals.json"
    data = [{"id": "g-old", "description": "旧目标", "status": "paused", "note": "原备注",
             "created_at": "2026-11-01T00:00:00+00:00", "updated_at": "2026-11-02T00:00:00+00:00"}]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    assert ledger.import_legacy_goals(db, path) == 1
    assert ledger.import_legacy_goals(db, path) == 0
    snapshot = ledger.snapshot_goal(db, "g-old")
    assert snapshot["control_state"] == "paused" and snapshot["note"] == "原备注"
    assert snapshot["revisions"][0]["source"] == "legacy_import"
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", ['{"not":"a list"}', '[{"id":"g1","description":"x","status":"wrong"}]', 'not json'])
def test_invalid_legacy_import_preserves_file_and_database(db, tmp_path, raw):
    path = tmp_path / "goals.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ledger.GoalMigrationError):
        ledger.import_legacy_goals(db, path)
    assert path.read_text(encoding="utf-8") == raw
    assert db.scalars(select(Goal)).all() == []


def test_entire_legacy_file_is_validated_before_any_import(db, tmp_path):
    path = tmp_path / "goals.json"
    raw = json.dumps([{"id": "g1", "description": "valid", "status": "active"}, {"id": "g2", "description": "bad", "status": "broken"}])
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ledger.GoalMigrationError):
        ledger.import_legacy_goals(db, path)
    assert db.scalars(select(Goal)).all() == [] and path.read_text() == raw


def test_legacy_import_conflict_rolls_back_partial_list(db, tmp_path):
    existing = ledger.ensure_run_goal(db, "existing", "c", "existing")
    db.commit()
    path = tmp_path / "goals.json"
    raw = json.dumps([{"id": "g-first", "description": "first", "status": "active"},
                      {"id": existing["id"], "description": "conflict", "status": "active"}])
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ledger.GoalMigrationError):
        ledger.import_legacy_goals(db, path)
    assert db.get(Goal, "g-first") is None
    assert db.get(Goal, existing["id"]).scope == "run" and path.read_text() == raw


def test_two_sessions_cannot_overwrite_user_revision(db):
    goal = ledger.ensure_run_goal(db, "cas-run", "c", "同一目标")
    db.commit()
    with SessionLocal() as other:
        assert ledger.snapshot_goal(other, goal["id"])["revision"] == goal["revision"]
        ledger.revise_goal(db, goal["id"], expected_revision=goal["revision"], action="pause", source_message_id="tab-a")
        db.commit()
        with pytest.raises(ledger.GoalConflict):
            ledger.revise_goal(other, goal["id"], expected_revision=goal["revision"], action="cancel", source_message_id="tab-b")
        other.rollback()
    assert ledger.snapshot_goal(db, goal["id"])["control_state"] == "paused"


@pytest.mark.parametrize("change", [
    {"end": 100}, {"offset": -1}, {"end": 0}, {"offset": True}, {"sha256": ""}, {"path": "OTHER.md"},
])
def test_invalid_file_receipts_cannot_manufacture_coverage(db, change):
    goal = ledger.ensure_run_goal(db, "invalid-page-run", "c", "读取整个文件 README.md")
    row = goal["requirements"][0]
    page = {"kind": "file_page", "path": "README.md", "offset": 0, "end": 10, "total_chars": 10, "sha256": "v1"}
    with pytest.raises(ledger.GoalError):
        ledger.record_requirement_evidence(db, goal["id"], row["id"], {**page, **change})
    assert ledger.snapshot_goal(db, goal["id"])["requirements"][0]["status"] == "open"


def test_global_goal_migration_failure_is_visible_not_empty_history(db, tmp_path, monkeypatch):
    path = tmp_path / "goals.json"
    path.write_text("broken history", encoding="utf-8")
    monkeypatch.setattr(goal_store, "GOAL_PATH", path)
    with pytest.raises(ledger.GoalMigrationError):
        goal_store.load_goals()
    assert path.read_text() == "broken history"


def test_goal_projection_uses_proven_status_and_user_cancellation_without_inventing_plan(db):
    goal = ledger.ensure_run_goal(db, "projection-run", "c", "甲和乙")
    plan = [step("甲", "A.md"), step("乙", "B.md")]
    saved = ledger.sync_plan_requirements(db, goal["id"], plan)
    assert all(s.get("requirement_id") for s in plan)
    a, b = plan[0]["requirement_id"], plan[1]["requirement_id"]
    saved = ledger.record_requirement_evidence(db, goal["id"], a, proof("A.md"))
    saved = ledger.revise_goal(db, goal["id"], expected_revision=saved["revision"],
                               action="cancel_requirements", source_message_id="user-skip-b", requirement_ids=[b])
    projected = ledger.apply_goal_to_plan(saved, plan)
    assert len(projected) == 1 and projected[0]["status"] == "done"
    assert projected[0]["evidence"] == [proof("A.md")]
    assert ledger.apply_goal_to_plan(saved, []) == []
    assert plan[1]["status"] == "pending", "projection must not mutate checkpoint input"


def test_goal_projection_does_not_accept_forged_id_or_model_done(db):
    goal = ledger.ensure_run_goal(db, "forged-projection-run", "c", "甲")
    saved = ledger.sync_plan_requirements(db, goal["id"], [step("甲", "A.md")])
    row = saved["requirements"][0]
    original = dict(step("甲", "A.md"), status="done", evidence=[{"kind": "answer"}])
    projected = ledger.apply_goal_to_plan(saved, [original])
    assert projected[0]["status"] == "pending" and projected[0]["evidence"] == []
    saved = ledger.record_requirement_evidence(db, goal["id"], row["id"], proof("A.md"))
    forged = dict(step("丙", "C.md"), requirement_id=row["id"])
    assert ledger.apply_goal_to_plan(saved, [forged])[0]["status"] == "pending"


def test_replacing_model_method_keeps_actual_user_requirement_and_validates_each_binding(db):
    from app.agent.planner import merge_model_plan

    goal = ledger.ensure_run_goal(db, "replace-method-run", "c", "分析资料")
    required = user_requirements(db, goal, [step("核实指定材料", "FINAL.md")])
    rid = required["requirements"][0]["id"]
    method_a = dict(step("先尝试甲方法", "A.md"), requirement_ids=[rid])
    ledger.sync_plan_requirements(db, goal["id"], [method_a])
    assert method_a["requirement_required"] is False
    replacement = dict(step("改用丙方法", "C.md"), requirement_ids=[rid])
    active = merge_model_plan([method_a], [replacement])
    assert [s["title"] for s in active] == ["改用丙方法"]
    active[0]["evidence"] = [proof("C.md")]
    updated = ledger.sync_plan_requirements(db, goal["id"], active)
    assert active[0]["requirement_ids"] == [rid]
    assert next(r for r in updated["requirements"] if r["id"] == rid)["status"] == "open"
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"
    actual = dict(step("正确核实资料", "FINAL.md"), requirement_ids=[rid], evidence=[proof("FINAL.md")])
    updated = ledger.sync_plan_requirements(db, goal["id"], [actual])
    assert next(r for r in updated["requirements"] if r["id"] == rid)["status"] == "completed"
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "completed"


def test_model_cannot_create_required_acceptance_or_bind_a_different_goal(db):
    own = ledger.ensure_run_goal(db, "own-run", "c", "分析资料")
    other = ledger.ensure_run_goal(db, "other-run", "other", "分析资料")
    other = user_requirements(db, other, [step("他会话要求", "OTHER.md")])
    foreign_id = other["requirements"][0]["id"]
    proposed = dict(step("模型提出的检查", "A.md"), acceptance="project_file", required=True,
                    requirement_ids=[foreign_id, "forged"])
    saved = ledger.sync_plan_requirements(db, own["id"], [proposed])
    assert saved["requirements"][0]["origin"] == "model_plan"
    assert saved["requirements"][0]["required"] is False
    assert proposed["requirement_ids"] == []
    assert ledger.finalize_goal(db, own["id"])["outcome"] == "open"


@pytest.mark.parametrize("content", ["请用中文回答", "不要删除任何原文件", "先解释原因，再给结论", "  保留原始换行\n\n以及空白  "])
def test_user_correction_is_kept_verbatim_without_creating_false_tool_obligations(db, content):
    goal = ledger.ensure_run_goal(db, "input-run", "c", "原任务")
    saved = ledger.record_user_input(db, goal["id"], expected_revision=goal["revision"],
                                     source_message_id="input-one", content=content)
    assert saved["requirements"] == []
    assert saved["revisions"][-1]["changes"] == {"content": content, "input_kind": "steer"}
    assert saved["revisions"][-1]["source"] == "user"
    duplicate = ledger.record_user_input(db, goal["id"], expected_revision=goal["revision"],
                                         source_message_id="input-one", content=content)
    assert duplicate["revision"] == saved["revision"]
    with pytest.raises(ledger.GoalConflict):
        ledger.record_user_input(db, goal["id"], expected_revision=saved["revision"],
                                 source_message_id="input-one", content="不同内容")
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "open"


def test_raw_user_correction_does_not_silently_cancel_existing_acceptance(db):
    goal = ledger.ensure_run_goal(db, "constraint-run", "c", "创建报告文件")
    saved = ledger.record_user_input(db, goal["id"], expected_revision=goal["revision"],
                                     source_message_id="keep-original", content="只改语言，改成中文")
    assert saved["requirements"][0]["required"] is True
    assert saved["requirements"][0]["status"] == "open"
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"


def test_actual_answer_can_finish_a_model_writing_method_without_claiming_user_acceptance(db):
    goal = ledger.ensure_run_goal(db, "writing-method-run", "c", "解释一个概念")
    plan = [{"title": "组织说明", "phase": "write", "status": "done",
             "evidence": [{"kind": "answer", "text": "实际输出的解释"}]}]
    saved = ledger.sync_plan_requirements(db, goal["id"], plan)
    projected = ledger.apply_goal_to_plan(saved, plan)
    assert projected[0]["status"] == "done"
    assert saved["requirements"][0]["required"] is False
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "open"


def test_only_explicit_user_addition_can_promote_an_existing_model_method(db):
    goal = ledger.ensure_run_goal(db, "promote-method-run", "c", "分析资料")
    method = step("读取材料", "A.md")
    saved = ledger.sync_plan_requirements(db, goal["id"], [method])
    original_id = saved["requirements"][0]["id"]
    promoted = user_requirements(db, saved, [step("读取材料", "A.md")])
    assert promoted["requirements"][0]["id"] == original_id
    assert promoted["requirements"][0]["origin"] == "user"
    assert promoted["requirements"][0]["required"] is True
    assert promoted["requirements"][0]["source_message_id"] == "explicit-user-requirements"
    omitted = ledger.sync_plan_requirements(db, goal["id"], [])
    assert omitted["requirements"][0]["status"] == "open"
    assert ledger.finalize_goal(db, goal["id"])["outcome"] == "partial"
