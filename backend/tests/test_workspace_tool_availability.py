"""Run capability availability is independent of natural-language keywords."""
from copy import deepcopy

import pytest

from app.agent.tool_availability import PROJECT_BOUND_TOOLS, available_workspace_tools


@pytest.mark.parametrize("workspace", [None, {}, {"project_id": None}, {"project_id": ""}])
def test_unlinked_workspace_removes_only_explicit_project_capabilities(workspace):
    names = sorted(PROJECT_BOUND_TOOLS) + ["read_attached_source", "read_research_artifact",
        "list_research_artifact_versions", "retrieve_evidence", "mcp__custom__project_search"]
    specs = [{"name": name, "parameters": {"type": "object"}} for name in names]
    original = deepcopy(specs)
    actual = available_workspace_tools(specs, workspace)
    assert [s["name"] for s in actual] == names[len(PROJECT_BOUND_TOOLS):]
    assert specs == original


def test_linked_workspace_preserves_all_specs_without_granting_more_capabilities():
    specs = [{"name": "list_project_files"}, {"name": "read_research_artifact"}]
    assert available_workspace_tools(specs, {"project_id": "bound-project"}) == specs
    assert available_workspace_tools([], {"project_id": "bound-project"}) == []


def test_actual_executor_never_dispatches_hallucinated_unlinked_project_tool(db, monkeypatch):
    from app.agent import loop
    from app.llm import LLMResponse
    from app.models import Conversation
    db.add(Conversation(id="unlinked-artifact-review")); db.commit()
    monkeypatch.setattr(loop.settings, "memory_recall", False)
    offered, calls = [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            offered.append(deepcopy(tools))
            if len(offered) == 1:
                assert "当前会话没有关联项目文件夹" in messages[0]["content"]
                yield "final", LLMResponse("", tool_calls=[{
                    "id": "injected-project", "name": "list_project_files", "arguments": {}}])
            else:
                yield "final", LLMResponse("请先关联目标项目，或提供要查看的文件。")
    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **kw: calls.append(a) or "{}")
    events = list(loop.run_agent(db, "查看项目文件", conversation_id="unlinked-artifact-review"))
    assert offered and all(not (PROJECT_BOUND_TOOLS & {s["name"] for s in specs}) for specs in offered)
    assert not calls
    assert any(kind == "answer" for kind, _ in events)
