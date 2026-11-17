"""Independent audit probes: synthetic model behavior, actual executor/gate/history.

These tests document the exact boundary being exercised. A scripted response
can prove what the executor does, but cannot estimate how a paid model behaves.
No user credentials or external model providers are used.
"""
from copy import deepcopy
import json

import pytest

from app import cancel_store, run_store
from app.agent import loop
from app.agent import tools as agent_tools
from app.agent.paper_scope import PaperScopeError, describe
from fastapi.testclient import TestClient
from app.main import app
from app.llm import LLMResponse
from app.models import Conversation, ForegroundRun, Paper, QARecord
from app.rag.citation import citation_gate, parse_claims


SOURCE_SENTENCE = "Transformer采用多头注意力并行计算不同位置之间的关系。"
GENERAL_SENTENCE = "Python中的列表是可以按索引访问的有序可变序列。"
EVIDENCE = {
    "id": "original", "paper_id": "fixture_paper", "paper_title": "合成测试论文",
    "page": 1, "quote": SOURCE_SENTENCE, "chunk_id": "fixture_chunk",
}
MARKDOWN_CITATION_EXAMPLES = [
    "示例：`[^e99]`。",
    "`a_long_function_name(argument_one, argument_two, '[^e99]')`",
    "示例：``文本 `[^e99]` 引用``。",
    "```markdown\n示例文字[^e99]\n```",
    "~~~markdown\n示例文字[^e99]\n~~~",
    "````markdown\n```\n示例文字[^e99]\n```\n````",
    r"示例：\[^e99]。",
]


class ScriptedModel:
    mode = "api"

    def __init__(self, answer, *, retrieve=False):
        self.answer = answer
        self.retrieve = retrieve
        self.calls = []
        self.rewrites = []
        self.scope_checks = []
        self.attribution_checks = []

    def stream_chat(self, messages, tools=None):
        self.calls.append(deepcopy(messages))
        if self.retrieve and not any(message["role"] == "tool" for message in messages):
            yield "final", LLMResponse("", [{
                "id": "fixture_call", "name": "retrieve_evidence",
                "arguments": {"query": "fixture"},
            }])
        else:
            yield "delta", "answer", self.answer
            yield "final", LLMResponse(self.answer)

    def chat(self, messages, tools=None, **kwargs):
        try:
            payload = json.loads(messages[-1]["content"])
        except (ValueError, TypeError):
            payload = None
        # Dispatch this synthetic reviewer by the actual typed input contract,
        # not an obsolete natural-language system-prompt title.
        if isinstance(payload, dict) and {"question", "blocks", "context", "context_truncated"} <= set(payload):
            self.attribution_checks.append(deepcopy(messages))
            unsupported_source = self.answer.startswith("《合成测试论文》证明")
            return LLMResponse(json.dumps({"source_required": unsupported_source, "results": [
                {"index": index, "kind": "source" if unsupported_source else "independent",
                 "claims": [{"text": block["text"], "kind": "source", "supported": False, "supports": [],
                    "reason": "该合成夹具没有实际来源正文"}] if unsupported_source else []}
                for index, block in enumerate(payload["blocks"])
            ], "quality_issues": []}, ensure_ascii=False))
        if isinstance(payload, dict) and {"question", "full_answer", "uncited_candidates", "evidence"} <= set(payload):
            self.scope_checks.append(deepcopy(messages))
            # These exact, finite literals are the requested Markdown examples
            # in this fixture. No meta prefix or unknown source claim gets a
            # blanket exemption merely because it appears in an example.
            known_example = any(self.answer == SOURCE_SENTENCE + "[^e1]\n\n" + example
                                for example in MARKDOWN_CITATION_EXAMPLES)
            def independent(candidate):
                return candidate["text_prefix"] == GENERAL_SENTENCE or (
                    known_example and SOURCE_SENTENCE not in candidate["text_prefix"])
            return LLMResponse(json.dumps({"results": [
                {"index": candidate["index"],
                 "requires_citation": not independent(candidate),
                 "reason": ("该有限夹具明确为独立常识或用户要求的Markdown字面示例"
                            if independent(candidate) else "测试夹具中的论文来源事实仍需引用"),
                 **({"kind": "independent" if independent(candidate) else "source", "supports": [], "process_scope": "none"}
                    if "process_receipts" in payload else {})}
                for candidate in payload["uncited_candidates"]
            ]}, ensure_ascii=False))
        self.rewrites.append(deepcopy(messages))
        # Preserve the legitimate model output so the backend decision is visible.
        return LLMResponse(self.answer)


@pytest.fixture(autouse=True)
def isolated_audit(monkeypatch):
    monkeypatch.setattr(cancel_store, "_flags", {})
    for name in ("_active", "_finished", "_early_cancels"):
        monkeypatch.setattr(run_store, name, {})
    monkeypatch.setattr(loop.settings, "memory_recall", True)
    monkeypatch.setattr(loop.settings, "memory_capture", True)
    monkeypatch.setattr(loop.settings, "llm_api_key", "")
    monkeypatch.setattr(loop.settings, "work_rules", "")
    monkeypatch.setattr(loop.agents_md, "load_merged_rules", lambda *args: "")
    monkeypatch.setattr(loop.goal_store, "get_current_goal", lambda: None)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *args, **kwargs: {
        "ran": 0, "blocked": False, "feedback": "",
    })
    monkeypatch.setattr(loop, "get_tool_specs", lambda: [
        {"name": "retrieve_evidence", "parameters": {"type": "object", "properties": {}}},
    ])
    monkeypatch.setattr(loop, "execute_tool", lambda *args, **kwargs: json.dumps({"evidence": [EVIDENCE]}))
    monkeypatch.setattr(loop, "citation_gate", citation_gate)
    monkeypatch.setattr(loop, "audit_answer", lambda *args, **kwargs: {
        "mode": "fixture", "passed": True, "verified": True, "status": "passed",
        "unsupported": [], "unsupported_ratio": 0.0,
    })


def drive(db, monkeypatch, answer, *, question="合成审计问题", retrieve=False, **kwargs):
    model = ScriptedModel(answer, retrieve=retrieve)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(loop.run_agent(
        db, question, conversation_id="c_daily_audit",
        runtime_config={"mode": "api", "model_id": "Synthetic-Model", "approval_mode": "auto"},
        **kwargs,
    ))
    return model, events, next(event for kind, event in events if kind == "answer")


def test_audit_mixed_general_and_cited_answer_preserves_both_requested_parts(db, monkeypatch):
    text = SOURCE_SENTENCE + "[^e1]\n\n" + GENERAL_SENTENCE
    model, _, result = drive(
        db, monkeypatch, text, retrieve=True,
        question="请总结论文的注意力机制，再解释Python的列表是什么。",
    )
    assert result["refusal"] == 0 and result["answer"] == text
    assert model.rewrites == []
    assert len(model.scope_checks) == 1
    assert result["gate"]["scoped_review"]["scope"] == "citation_requirement_only"


@pytest.mark.parametrize("cited_count", [1, 4])
def test_audit_mixed_scope_review_does_not_exempt_uncited_paper_facts(db, monkeypatch, cited_count):
    text = "\n\n".join([SOURCE_SENTENCE + "[^e1]"] * cited_count)
    text += "\n\n" + GENERAL_SENTENCE + "\n\n论文的实验准确率达到百分之九十九。"
    model, _, result = drive(
        db, monkeypatch, text, retrieve=True,
        question="请总结论文的注意力机制和实验准确率，再解释Python的列表。",
    )
    assert result["refusal"] == 1
    assert result["gate"]["invalid_markers"] == []
    assert any("论文的实验准确率" in sentence for sentence in result["gate"]["unsupported"])


@pytest.mark.parametrize("example", MARKDOWN_CITATION_EXAMPLES)
def test_audit_code_citation_examples_preserve_real_evidence_answer(db, monkeypatch, example):
    text = SOURCE_SENTENCE + "[^e1]\n\n" + example
    _, _, result = drive(
        db, monkeypatch, text, retrieve=True,
        question="请总结这篇论文，再演示Markdown引用标记的写法。",
    )
    assert result["refusal"] == 0 and result["answer"] == text
    assert result["gate"]["invalid_markers"] == []
    assert result["gate"]["passed"] is True
    assert all("e99" not in claim["evidence_ids"] for claim in result["claims"])


def test_audit_inline_numeric_claim_is_still_sent_to_semantic_audit():
    text = "该模型在测试中的准确率为 `99.8%`，使用 `Transformer` 架构。[^e1]"
    claims = parse_claims(text, ["e1"])
    assert len(claims) == 1
    assert "99.8%" in claims[0]["claim"] and "Transformer" in claims[0]["claim"]
    assert claims[0]["evidence_ids"] == ["e1"]


def test_audit_literal_marker_cannot_supply_a_real_citation():
    text = SOURCE_SENTENCE + "`[^e1]`"
    gate = citation_gate(text, ["e1"])
    assert gate["passed"] is False and gate["marked_claims"] == 0
    assert parse_claims(text, ["e1"]) == []


def test_audit_code_example_does_not_hide_an_invalid_prose_marker():
    text = SOURCE_SENTENCE + "[^e1]\n\n示例：`[^e99]`。正文仍然给出了未知来源的事实。[^e98]"
    gate = citation_gate(text, ["e1"])
    assert gate["passed"] is False and gate["invalid_markers"] == ["e98"]


def test_audit_source_claim_without_tool_or_marker_is_not_accepted_without_source_proof(db, monkeypatch):
    text = "《合成测试论文》证明该方法在全部数据集上的准确率达到99.8%。"
    model, events, result = drive(db, monkeypatch, text, question="这篇论文报告的准确率是多少？")
    assert any(kind == "audit" for kind, _ in events)
    assert not any(kind == "tool_call" for kind, _ in events)
    assert result["completed"] is False
    assert result["evidence"] == []
    assert result["source_attribution"]["status"] == "failed"
    assert model.attribution_checks
    assert "99.8%" not in result["answer"]
    assert db.query(QARecord).one().answer == result["answer"]


def test_audit_research_history_and_topic_switch_are_forwarded_without_forced_tools(db, monkeypatch):
    db.add(Conversation(id="c_daily_audit", title="旧研究"))
    db.add(QARecord(
        id="qa_old_research", conversation_id="c_daily_audit", question="请总结注意力论文。",
        answer=SOURCE_SENTENCE + "[^e1]", evidence=[dict(EVIDENCE, id="e1")], in_memory=1,
    ))
    db.commit()
    question = "先不讨论论文，把你好翻译成英文。"
    model, events, result = drive(db, monkeypatch, "Hello.", question=question)
    messages = model.calls[0]
    assert {k: messages[-1][k] for k in ("role", "content")} == {"role": "user", "content": question}
    history_answer = next(message["content"] for message in messages if message["role"] == "assistant")
    assert "[历史证据:qa_old_research:e1]" in history_answer
    assert "[^e1]" not in history_answer
    assert not any(kind in {"plan", "tool_call", "critic"} for kind, _ in events)
    assert result["source_attribution"]["status"] == "not_applicable"
    assert result["source_attribution"]["quality_status"] == "passed" and model.attribution_checks
    assert result["answer"] == "Hello." and result["refusal"] == 0
    assert db.query(QARecord).count() == 2


@pytest.mark.parametrize("status", ["pending", "parsing", "needs_ocr", "parsed"])
def test_audit_nonready_selected_source_preserves_ordinary_conversation(db, monkeypatch, status):
    paper = Paper(id="p_not_ready", title="正在解析的论文", status=status, pdf_path="fixture.pdf")
    db.add(paper)
    db.commit()
    model, events, result = drive(db, monkeypatch, "你好！", question="你好", paper_id=paper.id)
    assert len(model.calls) == 1 and result["answer"] == "你好！" and result["refusal"] == 0
    scope = next(event for kind, event in events if kind == "paper_scope")
    assert scope["paper_id"] == paper.id and scope["retrieval_ready"] is False
    assert scope["unavailable_reason"]
    assert "当前不可检索原文" in model.calls[0][0]["content"]
    assert not any(kind == "tool_call" for kind, _ in events)
    assert db.query(QARecord).one().paper_id == paper.id


def test_audit_missing_selected_source_is_not_silently_dropped(db):
    with pytest.raises(PaperScopeError) as failure:
        describe(db, "missing-paper")
    assert failure.value.status_code == 404


@pytest.mark.parametrize("status", ["pending", "needs_ocr", "parsed"])
def test_audit_nonready_retrieval_returns_model_visible_error_without_library_fallback(db, monkeypatch, status):
    paper = Paper(id="p_not_ready", title="正在解析的论文", status=status)
    db.add(paper)
    db.commit()
    monkeypatch.setattr(loop, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(agent_tools, "retrieve", lambda *args, **kwargs: pytest.fail("must not query full library"))
    text = "所选论文当前没有可用原文，我不能确认它的结论。"
    model, _, result = drive(db, monkeypatch, text, retrieve=True, paper_id=paper.id)
    tool_message = next(message for message in model.calls[-1] if message["role"] == "tool")
    denial = json.loads(tool_message["content"])
    assert denial.get("error") and not denial.get("evidence")
    # The authorization fence can reject before the underlying tool's scoped
    # error response. Both public paths deny retrieval; neither queries fallback.
    assert denial.get("code") in (None, "paper_scope_invalid")
    assert result["answer"] == text and result["refusal"] == 0 and result["evidence"] == []


@pytest.mark.parametrize("status", ["pending", "needs_ocr", "parsed"])
def test_audit_chat_http_accepts_nonready_selected_source_for_general_question(db, monkeypatch, status):
    paper = Paper(id="p_http_not_ready", title="未就绪来源", status=status)
    db.add(paper)
    db.commit()
    model = ScriptedModel("你好！我可以正常对话。")
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    response = TestClient(app).post("/api/chat/stream", json={
        "question": "你好", "paper_id": paper.id, "conversation_id": "c_daily_http",
    })
    assert response.status_code == 200, response.text
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    result = next(frame for frame in frames if frame.get("kind") == "answer")
    assert result["answer"] == model.answer and result["refusal"] == 0
    assert len(model.calls) == 1
    db.expire_all()
    record = db.query(QARecord).one()
    assert record.paper_id == paper.id and record.answer == model.answer


def test_audit_background_admission_accepts_nonready_source_without_launching_live_work(db, monkeypatch):
    from app import task_runner
    import threading
    # This test intentionally does not enter the app lifespan. An earlier
    # TestClient shutdown must not leak its shutdown flag into this admission.
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    paper = Paper(id="p_task_not_ready", title="未就绪后台来源", status="needs_ocr")
    db.add(paper)
    db.commit()
    admitted = []

    def launch(session, task):
        admitted.append(task)
        return task_runner.task_to_dict(task)

    monkeypatch.setattr(task_runner, "_launch", launch)
    response = TestClient(app).post("/api/tasks/start", json={
        "question": "你好", "paper_id": paper.id, "conversation_id": "c_daily_task",
    })
    assert response.status_code == 200
    assert len(admitted) == 1 and response.json()["paper_id"] == paper.id
    assert admitted[0].payload["paper_id"] == paper.id


def test_audit_foreground_missing_source_fails_before_workspace_or_run_side_effects(db, monkeypatch):
    from app.api import foreground_runs

    def forbidden(*args, **kwargs):
        pytest.fail("missing source must fail before capturing workspace or credentials")

    monkeypatch.setattr(foreground_runs.workspace, "capture", forbidden)
    monkeypatch.setattr(foreground_runs, "capture_run_config", forbidden)
    response = TestClient(app).post("/api/foreground-runs", json={
        "id": "chat_missing_source_0001", "conversation_id": "c_missing_source",
        "question": "你好", "paper_id": "missing-paper",
    })
    assert response.status_code == 404
    assert db.query(ForegroundRun).count() == 0
    assert db.query(Conversation).count() == 0


@pytest.mark.parametrize("status", ["pending", "parsing", "needs_ocr", "parsed"])
def test_audit_foreground_admitted_nonready_source_can_complete_ordinary_http_chat(db, monkeypatch, status):
    paper = Paper(id="p_foreground_not_ready", title="前台未就绪来源", status=status)
    db.add(paper)
    db.commit()
    model = ScriptedModel("你好！当前仍可以正常交谈。")
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    client = TestClient(app)
    run_id = "chat_nonready_source_0001"
    payload = {"question": "你好", "paper_id": paper.id, "conversation_id": "c_foreground_source"}
    admitted = client.post("/api/foreground-runs", json={"id": run_id, **payload})
    assert admitted.status_code == 200 and admitted.json()["status"] == "running"
    response = client.post("/api/chat/stream", json={"run_id": run_id, **payload})
    assert response.status_code == 200
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    result = next(frame for frame in frames if frame.get("kind") == "answer")
    assert result["answer"] == model.answer and result["refusal"] == 0
    db.expire_all()
    checkpoint = db.get(ForegroundRun, run_id)
    assert checkpoint.paper_id == paper.id and checkpoint.status == "completed"
    record = db.query(QARecord).one()
    assert record.paper_id == paper.id and record.answer == model.answer

