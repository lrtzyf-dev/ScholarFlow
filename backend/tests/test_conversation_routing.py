"""The model chooses each action; execution preserves evidence and recovery."""
from copy import deepcopy
import json

import pytest

from app import cancel_store
from app.agent import execution, loop, planner, tools as agent_tools
from app.agent.checkpoint import AgentState
from app.execution_mode import PlanConflict, claim_reviewed_plan
from app.llm import LLMResponse, MockLLM
from app.models import QARecord
from app.rag.citation import citation_gate


def unexpected(*args, **kwargs):
    pytest.fail("This conversation path must not perform this operation")


def call(name, arguments=None, call_id="fixture-call"):
    return LLMResponse("", [{"id": call_id, "name": name, "arguments": arguments or {}}])


class ScriptedModel:
    def __init__(self, replies=None, *, cancel_run=None):
        self.replies = replies or [LLMResponse("这是模型根据本轮输入给出的回答。")]
        self.cancel_run = cancel_run
        self.calls = []
        self.closed = 0

    def chat(self, *args, **kwargs):
        unexpected()

    def stream_chat(self, messages, tools=None):
        index = len(self.calls)
        self.calls.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        try:
            if self.cancel_run:
                cancel_store.request_cancel(self.cancel_run)
                yield "delta", "answer", "must not be delivered"
                return
            assert index < len(self.replies), "Unexpected additional model call"
            reply = self.replies[index]
            if reply.content:
                yield "delta", "answer", reply.content
            yield "final", reply
        finally:
            self.closed += 1


@pytest.fixture(autouse=True)
def isolated_conversation(monkeypatch):
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "llm_model", "Different-Global-Model")
    monkeypatch.setattr(loop.settings, "approval_mode", "auto")
    monkeypatch.setattr(loop.settings, "memory_capture", True)
    monkeypatch.setattr(loop.settings, "work_rules", "")
    monkeypatch.setattr(loop, "make_plan", unexpected)
    monkeypatch.setattr(loop, "execute_tool", unexpected)
    monkeypatch.setattr(loop, "citation_gate", unexpected)
    monkeypatch.setattr(loop, "audit_answer", unexpected)
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *args: "")
    monkeypatch.setattr(loop.goal_store, "get_current_goal", lambda: None)
    monkeypatch.setattr(loop, "_load_history", lambda *args, **kwargs: ([], {"total": 0}))
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *args, **kwargs: {
        "ran": 0, "blocked": False, "feedback": "",
    })
    specs = [spec for spec in agent_tools.TOOL_SPECS if spec["name"] in {
        "update_plan", "list_papers", "retrieve_evidence",
    }]
    monkeypatch.setattr(loop, "get_tool_specs", lambda: deepcopy(specs))
    model = ScriptedModel()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    return model


def drive(db, question, **kwargs):
    return loop.run_agent(db, question, conversation_id="c_routing", **kwargs)


def answer_of(events):
    answers = [event for kind, event in events if kind == "answer"]
    assert len(answers) == 1
    return answers[0]


def checkpoint_writer(db, snapshots):
    def persist(snapshot):
        snapshots.append(deepcopy(snapshot))
        db.commit()
    return persist


@pytest.mark.parametrize("question", [
    "你好", "HELLO!", "谢谢", "两个加两个等于几？",
    "你好，请总结这篇论文", "谢谢，请继续读取附件", "Hi, review the project",
])
def test_model_can_answer_any_text_without_forced_plan_or_retrieval(db, isolated_conversation, question):
    events = list(drive(db, question))
    answer = answer_of(events)
    assert answer["answer"] == isolated_conversation.replies[0].content
    assert len(isolated_conversation.calls) == isolated_conversation.closed == 1
    assert isolated_conversation.calls[0]["messages"][-1]["content"] == question
    assert answer["refusal"] == 0 and answer["plan_steps"] == []
    assert answer["evidence"] == []
    assert answer["gate"]["status"] == answer["audit"]["status"] == "not_applicable"
    assert answer["audit"]["verified"] is False
    assert not any(kind in {"plan", "tool_call", "critic", "audit"} for kind, _ in events)
    assert sum(kind == "done" for kind, _ in events) == 1
    record = db.query(QARecord).one()
    assert record.question == question and record.answer == answer["answer"]
    assert record.refusal == 0 and record.evidence == []


@pytest.mark.parametrize("selected", ["image", "skill"])
def test_selected_input_is_sent_to_model_without_triggering_a_plan(
        db, monkeypatch, isolated_conversation, selected):
    from app import skill_store

    kwargs = {}
    if selected == "image":
        kwargs.update(images=[{"media_type": "image/png", "data": "AA=="}],
                      runtime_config={"mode": "api", "model_id": "Fixture-Vision-Model"})
    else:
        monkeypatch.setattr(skill_store, "load_skill", lambda *args, **kwargs: {
            "name": "Fixture Skill", "content": "Use the selected synthetic skill instruction.",
        })
        kwargs["selected_skills"] = ["fixture-skill"]
    events = list(drive(db, "你好", **kwargs))
    assert len(isolated_conversation.calls) == 1
    assert not any(kind in {"plan", "tool_call"} for kind, _ in events)
    assert db.query(QARecord).count() == 1
    messages = isolated_conversation.calls[0]["messages"]
    if selected == "image":
        assert isinstance(messages[-1]["content"], list)
        assert any(part.get("type") == "image_url" for part in messages[-1]["content"])
    else:
        assert any(kind == "skill_attach" for kind, _ in events)
        assert "selected synthetic skill instruction" in messages[0]["content"]


@pytest.mark.parametrize(("mode", "model_id"), [
    ("api", "Org/MiXeD-Model-V4"), ("litellm", "Provider/EXACT-Id"), ("mock", "Fixture-Mock-Model"),
])
def test_model_receives_exact_public_binding_without_private_reference(
        db, isolated_conversation, mode, model_id):
    config = {"mode": mode, "model_id": model_id, "approval_mode": "auto",
              "credential_ref": "Private-Credential-Reference"}
    events = list(drive(db, "你现在用的是什么模型？", runtime_config=config))
    assert len(isolated_conversation.calls) == 1
    system_text = isolated_conversation.calls[0]["messages"][0]["content"]
    assert model_id in system_text and f'"mode": "{mode}"' in system_text
    assert "Different-Global-Model" not in system_text
    assert "Private-Credential-Reference" not in system_text
    assert "credential_ref" not in system_text
    assert answer_of(events)["answer"] == isolated_conversation.replies[0].content
    public = json.dumps({"events": events, "trace": db.query(QARecord).one().trace}, ensure_ascii=False)
    assert "credential_ref" not in public and "Private-Credential-Reference" not in public


@pytest.mark.parametrize("question", ["你好", "总结这篇论文"])
def test_cancel_before_execution_does_not_deliver_or_persist_an_answer(db, isolated_conversation, question):
    cancel_store.request_cancel("route-before-cancel")
    events = list(drive(db, question, run_id="route-before-cancel"))
    assert any(kind == "cancelled" for kind, _ in events)
    assert not any(kind in {"answer", "done", "tool_call"} for kind, _ in events)
    assert db.query(QARecord).count() == 0 and isolated_conversation.calls == []


def test_cancellation_during_model_answer_closes_stream_without_saving_qa(db, monkeypatch):
    model = ScriptedModel(cancel_run="route-stream-cancel")
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(drive(db, "你好", run_id="route-stream-cancel"))
    assert any(kind == "cancelled" for kind, _ in events)
    assert not any(kind in {"answer", "done", "answer_delta"} for kind, _ in events)
    assert model.closed == 1 and db.query(QARecord).count() == 0


def test_cancellation_after_fragment_prevents_final_delivery(db):
    source = drive(db, "你好", run_id="route-fragment-cancel")
    events = []
    for event in source:
        events.append(event)
        if event[0] == "answer_delta":
            cancel_store.request_cancel("route-fragment-cancel")
    assert any(kind == "cancelled" for kind, _ in events)
    assert not any(kind in {"answer", "done"} for kind, _ in events)
    assert db.query(QARecord).count() == 0


@pytest.mark.parametrize("question", ["你好", "能帮我确认目前有哪些内容吗？"])
def test_model_chosen_tools_do_not_invent_a_plan_and_resume_without_replay(db, monkeypatch, question):
    model = ScriptedModel([
        LLMResponse("", [
            {"id": "one", "name": "list_papers", "arguments": {"page": 1}},
            {"id": "two", "name": "list_papers", "arguments": {"page": 2}},
        ]), LLMResponse("目录中没有其他条目。"),
    ])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    completed, snapshots = [], []

    def tool(db, name, arguments, **kwargs):
        completed.append((name, deepcopy(arguments)))
        return json.dumps({"papers": [], "page": arguments["page"]})

    monkeypatch.setattr(loop, "execute_tool", tool)
    persist = checkpoint_writer(db, snapshots)
    source = drive(db, question, save_checkpoint=persist)
    first_events = []
    for event in source:
        first_events.append(event)
        if event[0] == "tool_result":
            break
    source.close()
    checkpoint = snapshots[-1]
    assert checkpoint["planning_started"] is True and checkpoint["todo"] == []
    assert checkpoint["plan_source"] == "requirements"
    assert completed == [("list_papers", {"page": 1})]
    assert not any(kind in {"plan", "todo_update"} for kind, _ in first_events)
    resumed = list(drive(db, question, checkpoint=checkpoint, save_checkpoint=persist))
    assert not any(kind in {"plan", "todo_update"} for kind, _ in resumed)
    assert not any(kind in {"thinking", "thinking_delta"} for kind, _ in first_events + resumed)
    assert any(kind == "status" and event.get("name") == "list_papers" for kind, event in first_events)
    assert completed == [("list_papers", {"page": 1}), ("list_papers", {"page": 2})]
    assert len(model.calls) == 2 and answer_of(resumed)["refusal"] == 0
    assert answer_of(resumed)["plan_steps"] == []
    assert snapshots[-1]["phase"] == "completed"
    assert AgentState.model_validate(snapshots[-1]).planning_started is True
    assert db.query(QARecord).count() == 1


@pytest.mark.parametrize("provider_output", ["reasoning", "preamble", "both"])
def test_real_model_reasoning_and_tool_preamble_are_preserved_without_synthetic_thoughts(
        db, monkeypatch, provider_output):
    reasoning, progress = "MODEL_PROVIDED_REASONING", "MODEL_PROVIDED_TOOL_PREAMBLE"
    model = ScriptedModel([
        LLMResponse(content=progress if provider_output != "reasoning" else "",
                    tool_calls=[{"id": "real-tool-call", "name": "list_papers", "arguments": {}}],
                    reasoning_content=reasoning if provider_output != "preamble" else ""),
        LLMResponse("当前目录为空。"),
    ])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "execute_tool", lambda *args, **kwargs: json.dumps({"papers": []}))
    events = list(drive(db, "列出当前条目"))
    thoughts = [event for kind, event in events if kind == "thinking"]
    progress_events = [event for kind, event in events if kind == "commentary"]
    assert [event["text"] for event in thoughts] == ([] if provider_output == "preamble" else [reasoning])
    assert not any(event.get("preamble") for event in thoughts)
    assert [event["text"] for event in progress_events] == ([] if provider_output == "reasoning" else [progress])
    if progress_events:
        assert progress_events[0]["source"] == "model" and progress_events[0]["draft_to_commentary"] is True
        assert next(i for i, (kind, _) in enumerate(events) if kind == "commentary") < next(
            i for i, (kind, _) in enumerate(events) if kind == "tool_call")
    assert any(kind == "status" and event.get("name") == "list_papers" for kind, event in events)
    assert answer_of(events)["answer"] == "当前目录为空。" and len(model.calls) == 2
    assert [event["text"] for event in db.query(QARecord).one().trace if event["kind"] == "commentary"] == [
        event["text"] for event in progress_events]


def test_private_acceptance_checks_survive_tool_resume_without_a_visible_plan(db, monkeypatch):
    question = "创建报告文件并运行测试"
    model = ScriptedModel([
        call("list_papers", {}, "check-input"),
        LLMResponse("已写完报告并通过测试。"),
        LLMResponse("实际尚未创建报告文件，也未运行测试，暂时无法完成。"),
    ])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "execute_tool", lambda *args, **kwargs: json.dumps({"papers": []}))
    snapshots = []
    persist = checkpoint_writer(db, snapshots)
    source = drive(db, question, save_checkpoint=persist)
    before = []
    for event in source:
        before.append(event)
        if event[0] == "tool_result":
            break
    source.close()
    saved = snapshots[-1]
    assert saved["plan_source"] == "requirements"
    assert {step["acceptance"] for step in saved["todo"]} == {"project_file", "successful_command"}
    resumed = list(drive(db, question, checkpoint=saved, save_checkpoint=persist))
    result = answer_of(resumed)
    assert result["completed"] is False and result["outcome"] == "partial"
    assert result["plan_steps"] == [] and len(result["incomplete_steps"]) == 2
    assert not any(kind in {"plan", "todo_update"} for kind, _ in before + resumed)
    assert len(model.calls) == 3, "missing requirements must still be checked after a tool-only resume"
    assert all(step["status"] != "done" for step in snapshots[-1]["todo"])
    record = db.query(QARecord).one()
    saved_outcome = next(event for event in record.trace if event["kind"] == "task_outcome")
    assert saved_outcome["outcome"] == "partial" and saved_outcome["completed"] is False
    assert saved_outcome["incomplete_steps"] == result["incomplete_steps"]


def test_resume_after_delivered_answer_keeps_one_qa_and_does_not_recall_model(db, isolated_conversation):
    snapshots = []
    persist = checkpoint_writer(db, snapshots)
    source = drive(db, "你好", save_checkpoint=persist)
    for kind, _ in source:
        if kind == "answer":
            break
    source.close()
    assert snapshots[-1]["phase"] == "deliver"
    resumed = list(drive(db, "你好", checkpoint=snapshots[-1], save_checkpoint=persist))
    assert answer_of(resumed)["refusal"] == 0
    assert sum(kind == "done" for kind, _ in resumed) == 1
    assert db.query(QARecord).count() == 1 and len(isolated_conversation.calls) == 1


def test_model_can_explain_missing_literature_without_forced_research_work(db, monkeypatch):
    text = "当前没有提供该论文原文，我无法确认实验结论。请提供相关资料。"
    model = ScriptedModel([LLMResponse(text)])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(drive(db, "这篇论文有哪些关键结论？"))
    answer = answer_of(events)
    assert answer["answer"] == text and answer["refusal"] == 0
    assert answer["gate"]["passed"] is False and answer["audit"]["verified"] is False
    assert answer["evidence"] == []
    assert not any(kind in {"plan", "tool_call", "critic", "audit"} for kind, _ in events)


@pytest.mark.parametrize("text", [
    "该结论已被原文证实。[^e99]",
    "引用示例：`[^e1]`。真正的正文引用是这个结论。[^e99]",
    "```markdown\n示例文字[^e1]\n```\n正文声称结果已得到证实。[^e99]",
    r"正文中双反斜杠不是转义引用：\\[^e99]",
])
def test_unknown_citation_is_rejected_without_running_a_semantic_audit(db, monkeypatch, text):
    model = ScriptedModel([LLMResponse(text)])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "citation_gate", citation_gate)
    answer = answer_of(list(drive(db, "解释这个结论")))
    assert answer["refusal"] == 1 and answer["completed"] is False
    assert answer["gate"]["status"] == "insufficient_evidence"
    assert answer["audit"]["verified"] is False
    assert answer["gate"]["invalid_markers"] == ["e99"]
    assert text != answer["answer"]


@pytest.mark.parametrize("text", [
    "引用标记的写法是 `[^e1]`，这只是语法示例。",
    "需要显示反引号时可用双反引号：``示例 `[^e1]` 文本``。",
    "```markdown\n这是一段示例引用[^e1]\n```\n以上展示了脚注语法。",
    "~~~markdown\n这是一段示例引用[^e1]\n~~~\n以上展示了脚注语法。",
    "````markdown\n```\n嵌套示例[^e1]\n```\n````\n以上是四反引号代码块。",
    "说明：\n```markdown\n尚未闭合的代码示例[^e1]",
    r"普通文字中也可转义引用语法：\[^e1]。",
])
def test_markdown_citation_examples_are_delivered_without_source_refusal(db, monkeypatch, text):
    model = ScriptedModel([LLMResponse(text)])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(drive(db, "展示 Markdown 引用语法"))
    answer = answer_of(events)
    assert answer["answer"] == text and answer["refusal"] == 0
    assert answer["gate"]["status"] == "not_applicable"
    assert answer["audit"]["verified"] is False
    assert not any(kind in {"critic", "audit", "refuse"} for kind, _ in events)
    assert db.query(QARecord).one().answer == text


def test_empty_retrieval_does_not_become_verified_evidence(db, monkeypatch):
    model = ScriptedModel([
        call("retrieve_evidence", {"query": "fixture"}),
        LLMResponse("本次检索没有返回原文证据，因此无法确认该论文结论。"),
    ])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "execute_tool", lambda *args, **kwargs: json.dumps({"evidence": []}))
    events = list(drive(db, "分析相关结果"))
    answer = answer_of(events)
    assert answer["evidence"] == []
    assert answer["gate"]["passed"] is False and answer["audit"]["verified"] is False
    assert not any(kind in {"critic", "audit"} for kind, _ in events)


def test_native_model_plan_and_revision_persist_actual_completion_proofs(db, monkeypatch):
    initial_steps = [
        {"title": "检查已有条目", "phase": "search", "tool_name": "list_papers", "tool_arguments": {}},
        {"title": "整理结果", "phase": "write"},
    ]
    model = ScriptedModel([
        call("update_plan", {"steps": initial_steps}, "initial-plan"),
        call("list_papers", {}, "list-inventory"),
        call("update_plan", {"steps": [{"title": "整理结果", "phase": "write"}]}, "revise-plan"),
        LLMResponse("文献库当前为空，没有核实任何论文结论。"),
    ])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    native_calls, snapshots = [], []

    def native_tool(db, name, arguments, **kwargs):
        native_calls.append(name)
        return agent_tools.execute_tool(db, name, arguments, **kwargs)

    monkeypatch.setattr(loop, "execute_tool", native_tool)
    persist = checkpoint_writer(db, snapshots)
    source = drive(db, "按当前问题处理资料", save_checkpoint=persist)
    for kind, _ in source:
        if kind == "tool_result":
            break
    source.close()
    checkpoint = snapshots[-1]
    assert native_calls == ["update_plan"]
    assert checkpoint["planning_started"] is True
    assert checkpoint["plan_source"] == "model"
    assert [(step["title"], step["phase"]) for step in checkpoint["todo"]] == [
        (step["title"], step["phase"]) for step in initial_steps]
    assert all(step["status"] == "pending" for step in checkpoint["todo"])
    assert not any(step.get("evidence") for step in checkpoint["todo"])
    events = list(drive(db, "按当前问题处理资料", checkpoint=checkpoint, save_checkpoint=persist))
    assert native_calls == ["update_plan", "list_papers", "update_plan"]
    final_steps = snapshots[-1]["todo"]
    retained = next(step for step in final_steps if step["title"] == "检查已有条目")
    assert retained["status"] == "done"
    assert any(proof["kind"] == "tool_result" for proof in retained["evidence"])
    assert answer_of(events)["refusal"] == 0 and db.query(QARecord).count() == 1
    assert len(model.calls) == 4


@pytest.mark.parametrize("steps", [[], "invalid", [{"title": "", "phase": "read"}], [{"title": "Read", "phase": "invalid"}]])
def test_native_update_plan_rejects_invalid_plan_data(db, steps):
    result = json.loads(agent_tools.execute_tool(db, "update_plan", {"steps": steps}))
    assert result.get("error") and "plan_steps" not in result


@pytest.mark.parametrize(("name", "arguments"), [
    ("read_project_file", {}), ("retrieve_evidence", {}),
    ("update_plan", {}), ("missing_tool", {}), ("read_project_file", {"path": 123}),
])
def test_plan_tool_binding_requires_available_tool_and_required_target_identity(db, name, arguments):
    result = json.loads(agent_tools.execute_tool(db, "update_plan", {"steps": [
        {"title": "处理指定目标", "phase": "read", "tool_name": name, "tool_arguments": arguments}]}))
    assert result.get("error") and "plan_steps" not in result


@pytest.mark.parametrize(("name", "arguments"), [
    ("edit_project_file", {"path": "report.md"}),
    ("run_project_command", {"command": "python -m pytest"}),
    ("read_project_file", {"path": "README.md"}),
    ("retrieve_evidence", {"query": "target subject"}),
    ("git_project_write", {"action": "stage", "paths": ["report.md"]}),
    ("git_project_write", {"action": "commit", "message": "requested change"}),
])
def test_plan_binding_uses_stable_target_without_future_content_or_descriptions(db, name, arguments):
    result = json.loads(agent_tools.execute_tool(db, "update_plan", {"steps": [
        {"title": "处理目标", "phase": "write", "tool_name": name, "tool_arguments": arguments}]}))
    assert result["plan_steps"][0]["tool_arguments"] == arguments


@pytest.mark.parametrize(("name", "arguments"), [
    ("edit_project_file", {"content": "not a target"}),
    ("run_project_command", {"summary": "not a target"}),
    ("run_project_command", {"command": "python -m pytest", "summary": 123}),
    ("git_project_write", {"action": "stage"}),
    ("git_project_write", {"action": "commit"}),
])
def test_target_binding_rejects_missing_identity_and_invalid_optional_values(db, name, arguments):
    result = json.loads(agent_tools.execute_tool(db, "update_plan", {"steps": [
        {"title": "处理目标", "phase": "write", "tool_name": name, "tool_arguments": arguments}]}))
    assert result.get("error") and "plan_steps" not in result


@pytest.mark.parametrize("arguments", [{}, {"target": "wrong", "count": "bad-type"}])
def test_unknown_tool_binding_keeps_its_published_required_parameters(db, monkeypatch, arguments):
    monkeypatch.setattr(agent_tools, "get_tool_specs", lambda: [{"name": "mcp__fixture__read", "parameters": {
        "type": "object", "properties": {"target": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["target"]}}])
    result = json.loads(agent_tools.execute_tool(db, "update_plan", {"steps": [
        {"title": "读取指定目标", "phase": "read", "tool_name": "mcp__fixture__read", "tool_arguments": arguments}]}))
    assert result.get("error") and "plan_steps" not in result


@pytest.mark.parametrize(("name", "payload", "completed"), [
    ("edit_project_file", {"status": "proposed"}, False),
    ("edit_project_file", {"status": "conflict"}, False),
    ("edit_project_file", {"status": "applied"}, True),
    ("run_project_command", {"status": "succeeded", "exit_code": 0}, True),
    ("run_project_command", {"status": "succeeded", "exit_code": 1}, False),
    ("run_project_command", {"status": "proposed", "exit_code": 0}, False),
    ("run_project_command", {"status": "interrupted", "exit_code": None}, False),
    ("run_project_command", {"status": "succeeded", "exit_code": 0, "output_is_partial": True}, False),
])
def test_bound_write_and_command_need_actual_applied_or_successful_zero_exit_result(
        db, monkeypatch, name, payload, completed):
    bound = {"path": "report.md"} if name == "edit_project_file" else {"command": "synthetic-check"}
    actual = {"path": "report.md", "operation": "create", "summary": "generated file description",
              "content": "GENERATED_AFTER_PLANNING"} if name == "edit_project_file" else {
        "command": "synthetic-check", "summary": "generated description"}
    phase = "write" if name == "edit_project_file" else "audit"
    model = ScriptedModel([
        call("update_plan", {"steps": [{"title": "实际执行指定操作", "phase": phase,
                                         "tool_name": name, "tool_arguments": bound}]}, "bound-plan"),
        call(name, actual, "real-operation"),
        LLMResponse("本轮操作已处理。"), LLMResponse("尚不能确认操作已成功。"),
    ])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [spec for spec in agent_tools.TOOL_SPECS
                                                         if spec["name"] in {"update_plan", name}])
    monkeypatch.setattr(loop, "tool_risk", lambda _name: "safe")
    monkeypatch.setattr(loop, "execute_tool", lambda db, tool, args, **kwargs: agent_tools.execute_tool(db, tool, args))
    if name == "edit_project_file":
        monkeypatch.setattr(execution.project_changes, "prepare", lambda *args, **kwargs: {
            "id": "synthetic-change", "path": "report.md", "operation": "create", "summary": "write", "status": "proposed"})
        monkeypatch.setattr(execution.project_changes, "apply", lambda *args, **kwargs: {
            "id": "synthetic-change", "path": "report.md", **payload})
    else:
        monkeypatch.setattr(execution.project_commands, "prepare", lambda *args, **kwargs: {
            "id": "synthetic-command", "command": "synthetic-check", "cwd": ".", "summary": "check",
            "timeout_seconds": 5, "attempt": 1, "status": "proposed"})
        monkeypatch.setattr(execution.project_commands, "run", lambda *args, **kwargs: dict(payload))
    events = list(drive(db, "处理指定操作"))
    result = answer_of(events)
    assert result["completed"] is completed and len(model.calls) == (3 if completed else 4)
    assert (result["plan_steps"][0]["status"] == "done") is completed


@pytest.mark.parametrize(("name", "arguments", "payload", "completed"), [
    ("load_skill", {"name": "fixture"}, {"name": "fixture", "content": "guide"}, False),
    ("read_project_file", {"path": "OTHER.md"}, {"path": "OTHER.md", "text": "other"}, False),
    ("read_project_file", {"path": "README.md"}, {"path": "README.md", "next_offset": 100, "text": "page"}, False),
    ("read_project_file", {"path": "README.md"}, {"path": "README.md", "limited": True, "text": "cut"}, False),
    ("read_project_file", {"path": "README.md"}, {"error": "read failed"}, False),
    ("read_project_file", {"path": "README.md"}, {"path": "README.md", "status": "proposed"}, False),
    ("read_project_file", {"path": "README.md"}, {"path": "README.md", "text": "actual target"}, True),
])
def test_only_a_matching_successful_complete_tool_result_finishes_bound_step(
        db, monkeypatch, name, arguments, payload, completed):
    steps = [{"title": "读取指定README", "phase": "read", "tool_name": "read_project_file",
              "tool_arguments": {"path": "README.md"}}, {"title": "整理结果", "phase": "write"}]
    model = ScriptedModel([call("update_plan", {"steps": steps}, "bound-plan"),
                           call(name, arguments, "actual-work"),
                           LLMResponse("已得到本轮结果。"), LLMResponse("当前仍无法完成指定目标。")])
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    selected = {"update_plan", "read_project_file", "load_skill"}
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [spec for spec in agent_tools.TOOL_SPECS if spec["name"] in selected])
    monkeypatch.setattr(loop, "tool_risk", lambda _name: "safe")

    def actual_tool(db, actual_name, actual_arguments, **kwargs):
        if actual_name == "update_plan":
            return agent_tools.execute_tool(db, actual_name, actual_arguments)
        return json.dumps(payload)

    monkeypatch.setattr(loop, "execute_tool", actual_tool)
    snapshots = []
    events = list(drive(db, "按请求处理指定目标", save_checkpoint=checkpoint_writer(db, snapshots)))
    result = answer_of(events)
    assert result["completed"] is completed
    target = next(step for step in result["plan_steps"] if step["title"] == "读取指定README")
    assert (target["status"] == "done") is completed
    assert len(model.calls) == (3 if completed else 4)
    if completed:
        assert target["evidence"][0]["arguments"] == {"path": "README.md"}
    else:
        assert any(kind == "answer_reset" for kind, _ in events)
    restored = AgentState.model_validate(snapshots[-1])
    assert restored.todo[0]["tool_arguments"] == {"path": "README.md"}


def test_model_plan_revision_keeps_completion_proof_and_explicit_acceptance():
    completed = {"title": "读取资料", "phase": "read", "status": "done",
                 "evidence": [{"kind": "tool_result", "operation_key": "tool/0/0"}]}
    acceptance = {"title": "生成报告文件", "phase": "write", "status": "pending", "acceptance": "project_file"}
    result = planner.merge_model_plan([completed, acceptance], [{"title": "整理回答", "phase": "write"}])
    assert completed in result and acceptance in result
    assert next(step for step in result if step["title"] == "整理回答")["status"] == "pending"


def test_actual_mock_plan_registers_durable_reviewable_steps_before_text(db, monkeypatch):
    model, native_calls = MockLLM(), []
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setenv("MOCK_STREAM_DELAY", "0")

    def native_tool(db, name, arguments, **kwargs):
        native_calls.append(name)
        return agent_tools.execute_tool(db, name, arguments, **kwargs)

    monkeypatch.setattr(loop, "execute_tool", native_tool)
    events = list(drive(db, "请为下一步资料整理制定可执行计划", execution_mode="plan"))
    answer = answer_of(events)
    assert native_calls == ["update_plan"]
    assert answer["execution_mode"] == "plan" and answer["outcome"] == "planned"
    assert answer["completed"] is False and answer["refusal"] == 0
    assert answer["plan_steps"] and all(step["status"] == "pending" for step in answer["plan_steps"])
    record = db.query(QARecord).one()
    assert record.execution_mode == "plan" and record.plan_steps == answer["plan_steps"]
    first_tool = next(i for i, (kind, _) in enumerate(events) if kind == "tool_call")
    first_text = next(i for i, (kind, _) in enumerate(events) if kind == "answer_delta")
    assert first_tool < first_text
    assert not any(kind == "answer_reset" for kind, _ in events)
    assert claim_reviewed_plan(db, record.id, "c_routing", "chat-reviewed-plan") == record.answer
    db.commit()
    db.refresh(record)
    assert record.plan_run_id == "chat-reviewed-plan"


def test_actual_mock_greeting_in_plan_mode_remains_an_ordinary_response(db, monkeypatch):
    monkeypatch.setattr(loop, "get_llm", lambda: MockLLM())
    monkeypatch.setenv("MOCK_STREAM_DELAY", "0")
    events = list(drive(db, "你好", execution_mode="plan"))
    answer = answer_of(events)
    assert answer["refusal"] == 0 and answer["plan_steps"] == []
    assert answer["outcome"] == "completed"
    assert not any(kind in {"plan", "tool_call", "critic", "audit"} for kind, _ in events)
    record = db.query(QARecord).one()
    assert not record.plan_steps
    with pytest.raises(PlanConflict):
        claim_reviewed_plan(db, record.id, "c_routing", "chat-no-plan")

