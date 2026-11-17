"""Selected-paper scope must remain truthful from request to evidence."""
import json

import pytest
from fastapi.testclient import TestClient

from app.agent import loop
from app.agent.paper_scope import PaperScopeError, describe, resolve, system_block
from app.agent.tools import execute_tool
from app.llm import LLMResponse
from app.main import app


def test_selected_paper_is_injected_and_tool_cannot_switch_scope(db, seeded_paper, monkeypatch):
    class Model:
        def __init__(self):
            self.messages = []

        def chat(self, *args, **kwargs):
            raise RuntimeError("planner fallback")

        def stream_chat(self, messages, tools=None):
            self.messages.append(messages)
            yield ("final", LLMResponse(content="已完成"))

    model = Model()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(loop.run_agent(db, "这篇论文讲了什么？", paper_id=seeded_paper.id))

    system = model.messages[0][0]["content"]
    assert seeded_paper.id in system
    assert seeded_paper.title in system
    scope_event = next(event for kind, event in events if kind == "paper_scope")
    assert scope_event["paper_id"] == seeded_paper.id

    conflict = json.loads(execute_tool(
        db, "retrieve_evidence", {"query": "attention", "paper_id": "another-paper"},
        scope_paper_id=seeded_paper.id,
    ))
    assert conflict["code"] == "paper_scope_conflict"
    evidence = json.loads(execute_tool(
        db, "retrieve_evidence", {"query": "attention"}, scope_paper_id=seeded_paper.id,
    ))
    assert evidence["evidence"]
    assert {item["paper_id"] for item in evidence["evidence"]} == {seeded_paper.id}


def test_invalid_selected_paper_is_rejected_at_desktop_entrypoints():
    client = TestClient(app)
    for endpoint, payload in (
        ("/api/chat/stream", {"question": "q", "paper_id": "missing-paper"}),
        ("/api/tasks/start", {"question": "q", "paper_id": "missing-paper"}),
    ):
        response = client.post(endpoint, json=payload)
        assert response.status_code == 404, (endpoint, response.text)


@pytest.mark.parametrize("status", ["pending", "parsing", "needs_ocr", "parsed"])
def test_nonready_selected_paper_can_be_described_but_never_becomes_full_library(db, status):
    from app.models import Paper

    paper = Paper(title="等待解析", status=status)
    db.add(paper)
    db.commit()
    scope = describe(db, paper.id)
    assert scope.paper_id == paper.id and scope.status == status
    assert scope.retrieval_ready is False and scope.unavailable_reason
    assert scope.as_dict()["retrieval_ready"] is False
    assert "当前不可检索原文" in system_block(scope)
    assert "普通问答" in system_block(scope)
    with pytest.raises(PaperScopeError) as failure:
        resolve(db, paper.id)
    assert failure.value.status_code == 409
    for tool in ("retrieve_evidence", "list_papers"):
        result = json.loads(execute_tool(db, tool, {"query": "attention"}, scope_paper_id=paper.id))
        assert result["code"] == "paper_scope_invalid"
        assert "evidence" not in result and "papers" not in result
