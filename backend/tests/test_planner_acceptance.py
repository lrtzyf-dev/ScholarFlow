import pytest

from app.agent import planner
from app.llm import LLMResponse
from app.agent.planner import (
    add_acceptance_requirements,
    advance,
    complete_one,
    complete_requirement,
    finalize,
    record_file_read,
)


def test_advancing_phase_does_not_mark_unverified_prior_steps_done():
    plan = [
        {"title": "检索", "phase": "search", "status": "pending", "tool_name": "retrieve_evidence",
         "tool_arguments": {"query": "target"}},
        {"title": "阅读", "phase": "read", "status": "pending"},
        {"title": "交付", "phase": "final", "status": "pending"},
    ]

    advance(plan, "read")

    assert [step["status"] for step in plan] == ["pending", "in_progress", "pending"]
    assert complete_one(plan, "search", {"kind": "tool_result", "tool_name": "retrieve_evidence",
                                         "arguments": {"query": "target"}, "status": "completed"})
    assert plan[0]["status"] == "done"


def test_final_delivery_requires_every_step_to_have_evidence():
    plan = [
        {"title": "阅读", "phase": "read", "status": "pending"},
        {"title": "交付", "phase": "final", "status": "pending"},
    ]

    result = finalize(plan, {"kind": "delivery"})

    assert result["completed"] is False
    assert result["incomplete_steps"][0]["title"] == "阅读"
    assert plan[-1]["status"] == "pending"


def test_requested_file_and_validation_are_explicit_acceptance_items():
    plan = add_acceptance_requirements([], "生成一份报告文件并运行测试")

    assert {step["acceptance"] for step in plan} == {"project_file", "successful_command"}
    assert not complete_requirement(plan, "project_file", {"status": "proposed"})
    assert complete_requirement(plan, "project_file", {"status": "applied", "path": "report.md"})
    assert not finalize(plan, {"kind": "delivery"})["completed"]
    assert complete_requirement(plan, "successful_command", {"status": "succeeded", "exit_code": 0})
    assert finalize(plan, {"kind": "delivery"})["completed"]


def test_entire_named_file_requires_contiguous_pages_with_same_hash():
    plan = add_acceptance_requirements([], "读取整个文件 README.md")
    acceptance = next(step["acceptance"] for step in plan if step.get("acceptance"))
    assert acceptance == "full_file:readme.md"

    assert not record_file_read(plan, "README.md", {
        "offset": 8, "next_offset": None, "total_chars": 12, "sha256": "same",
    })
    assert record_file_read(plan, "README.md", {
        "offset": 0, "next_offset": 8, "total_chars": 12, "sha256": "same",
    })
    assert next(step for step in plan if step.get("acceptance"))["status"] == "done"


def test_all_sources_remain_unverified_without_coverage_evidence():
    plan = add_acceptance_requirements([], "阅读文件夹中的全部文件")

    assert any(step.get("acceptance") == "source_coverage" for step in plan)
    assert finalize(plan, {"kind": "delivery"})["completed"] is False

@pytest.mark.parametrize("response", ["not valid JSON", '{"steps": []}', None])
def test_explicit_legacy_planner_failure_does_not_invent_fallback_steps(response):
    class Model:
        def chat(self, *args, **kwargs):
            if response is None:
                raise RuntimeError("synthetic unavailable planner")
            return LLMResponse(response)

    assert planner.make_plan(None, Model(), "任意请求") == []


def test_model_plan_keeps_only_model_proposed_steps_without_synthetic_delivery():
    proposed = [{"title": "读取指定内容", "phase": "read"}]
    result = planner.merge_model_plan([], proposed)
    assert [(step["title"], step["phase"]) for step in result] == [("读取指定内容", "read")]
    assert result[0]["status"] == "pending" and result[0]["evidence"] == []


@pytest.mark.parametrize(("tool", "arguments", "status"), [
    ("load_skill", {"name": "readme"}, "completed"),
    ("read_project_file", {"path": "OTHER.md"}, "completed"),
    ("read_project_file", {"path": "README.md"}, "failed"),
    ("read_project_file", {}, "completed"),
])
def test_same_phase_tool_cannot_complete_a_different_target(tool, arguments, status):
    plan = planner.merge_model_plan([], [{"title": "读取README", "phase": "read",
                                          "tool_name": "read_project_file", "tool_arguments": {"path": "README.md"}}])
    assert not complete_one(plan, "read", {"kind": "tool_result", "tool_name": tool,
                                           "arguments": arguments, "status": status})
    assert plan[0]["status"] == "pending"


def test_tool_target_arguments_are_checked_and_cannot_be_satisfied_by_an_answer():
    plan = planner.merge_model_plan([], [{"title": "查找指定主题", "phase": "search",
                                          "tool_name": "retrieve_evidence", "tool_arguments": {"query": "target"}}])
    assert not complete_one(plan, "search", {"kind": "tool_result", "tool_name": "retrieve_evidence",
                                             "arguments": {"query": "unrelated"}, "status": "completed"})
    assert complete_one(plan, "search", {"kind": "tool_result", "tool_name": "retrieve_evidence",
                                         "arguments": {"query": "target", "k": 5}, "status": "completed"})
    write = planner.merge_model_plan([], [{"title": "写入文件", "phase": "write", "tool_name": "edit_project_file",
                                           "tool_arguments": {"path": "report.md", "content": "target"}}])
    assert not complete_one(write, "write", {"kind": "answer"})


def test_revising_the_target_does_not_inherit_previous_completion_proof():
    previous = {"title": "读取资料", "phase": "read", "tool_name": "read_project_file",
                "tool_arguments": {"path": "BEFORE.md"}, "status": "done", "evidence": [{"kind": "tool_result"}]}
    revised = planner.merge_model_plan([previous], [{"title": "读取资料", "phase": "read",
                                                     "tool_name": "read_project_file", "tool_arguments": {"path": "AFTER.md"}}])
    target = next(step for step in revised if step["tool_arguments"]["path"] == "AFTER.md")
    assert target["status"] == "pending" and target["evidence"] == []
    assert previous in revised


def test_unbound_legacy_tool_plan_does_not_accept_phase_only_proof():
    plan = [{"title": "读取资料", "phase": "read", "status": "pending"}]
    assert not complete_one(plan, "read", {"kind": "tool_result", "tool_name": "load_skill",
                                           "arguments": {"name": "unrelated"}, "status": "completed"})
    assert finalize(plan, {"kind": "delivery"})["completed"] is False


@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_model_revision_keeps_ordinary_unfinished_obligation(status):
    previous = {"title": "未完成乙", "phase": "read", "status": status,
                "tool_name": "read_project_file", "tool_arguments": {"path": "B.md"}, "evidence": []}
    result = planner.merge_model_plan([previous], [{"title": "新方法", "phase": "analyze"}])
    assert previous in result and previous["status"] == status
    assert finalize(result, {"kind": "delivery"})["completed"] is False


def test_normalization_keeps_requirement_reference_without_accepting_status_claim():
    result = planner.merge_model_plan([], [{"title": "甲", "phase": "read", "requirement_ids": ["req-a"],
                                            "status": "done", "evidence": [{"kind": "answer"}]}])
    assert result[0]["requirement_ids"] == ["req-a"]
    assert result[0]["status"] == "pending" and result[0]["evidence"] == []


def test_known_model_method_can_be_replaced_without_retaining_a_false_obligation():
    previous = {"title": "旧方法", "phase": "read", "status": "in_progress", "evidence": [],
                "requirement_origin": "model_plan", "requirement_required": False}
    revised = planner.merge_model_plan([previous], [{"title": "新方法", "phase": "analyze"}])
    assert [s["title"] for s in revised] == ["新方法"]


def test_model_acceptance_claim_is_replaceable_but_user_acceptance_is_not():
    method = {"title": "模型自定检查", "phase": "audit", "status": "pending", "evidence": [],
              "acceptance": "successful_command", "requirement_origin": "model_plan", "requirement_required": False}
    user_check = {"title": "用户要求检查", "phase": "write", "status": "pending", "evidence": [],
                  "acceptance": "project_file", "requirement_origin": "user_acceptance", "requirement_required": True}
    revised = planner.merge_model_plan([method, user_check], [])
    assert revised == [user_check]
