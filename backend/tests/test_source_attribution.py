"""Deterministic attribution protocol and executor regressions, no real API."""
from copy import deepcopy
import json

import pytest

from app.agent.source_attribution import (review_source_attribution, without_unsupported_claims,
                                         repair_context_key, _context_receipts)
from app.llm import LLMResponse, MockLLM


FACT = "这篇论文证明该方案准确率达到99%。"
GENERAL = "你可以先列出比较目标，再整理适用条件。"


def claim(text=FACT, *, supported=False, kind="source", supports=None):
    return {"text": text, "kind": kind, "supported": supported,
            "supports": supports or [], "reason": "按实际提供的原文核对"}


def block(index=0, kind="source", claims=None):
    return {"index": index, "kind": kind, "claims": [] if claims is None else claims}


def tool_messages(name, payload, call_id="read_1", arguments=None):
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {"path": "notes.md"})}}]},
        {"role": "tool", "tool_call_id": call_id,
         "content": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)},
    ]


class Reviewer:
    mode = "api"

    def __init__(self, results=None, *, source_required=False, raw=None, error=None):
        self.output = raw if raw is not None else json.dumps({"source_required": source_required, "results": results})
        self.error = error
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        if self.error:
            raise self.error
        return LLMResponse(self.output, usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16})


def test_zero_evidence_zero_marker_source_claim_is_reviewed_and_failed():
    reviewer = Reviewer([block(claims=[claim()])], source_required=True)
    usage = []
    result = review_source_attribution("总结这篇论文", FACT, [{"role": "user", "content": "总结这篇论文"}],
                                       reviewer, on_usage=usage.append)
    assert result["status"] == "failed" and result["unsupported"][0]["text"] == FACT
    assert result["model_understanding_verified"] is False
    assert usage == [{"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}]
    assert json.loads(reviewer.calls[0][1]["content"])["blocks"][0]["text"] == FACT


@pytest.mark.parametrize("answer", ["你好，有什么可以帮你？", GENERAL, "论文写作通常需要清楚说明问题和方法。"])
def test_independent_content_does_not_require_sources_even_with_old_catalog(answer):
    result = review_source_attribution("给我一些建议", answer,
        [{"role": "user", "content": "历史附件目录：某论文.pdf"}], Reviewer([block(kind="independent")]))
    assert result["status"] == "not_applicable" and result["verified"] is True
    assert result["source_required"] is False


def test_mixed_answer_only_removes_failing_span():
    answer = GENERAL + "\n\n" + FACT
    result = review_source_attribution("给建议并总结论文", answer, [],
        Reviewer([block(kind="independent"), block(1, claims=[claim()])], source_required=True))
    repaired = without_unsupported_claims(answer, result)
    assert GENERAL in repaired and FACT not in repaired
    assert "尚无可核对依据" in repaired


def test_supplied_user_text_can_support_analysis_without_claiming_external_read():
    text = "你提供的句子说样本为42例。"
    source = "请解释这个原句：样本为42例。"
    result = review_source_attribution(source, text, [{"role": "user", "content": source}],
        Reviewer([block(claims=[claim(text, supported=True,
            supports=[{"receipt_id": "context:0", "quote": "样本为42例。"}])])], source_required=True))
    assert result["status"] == "passed"
    assert result["claims"][0]["supports"][0]["role"] == "user"


def test_user_claim_cannot_certify_a_tool_was_executed():
    text = "已经读取附件全文。"
    result = review_source_attribution("读附件", text, [{"role": "user", "content": text}],
        Reviewer([block(kind="process", claims=[claim(text, kind="process", supported=True,
            supports=[{"receipt_id": "context:0", "quote": text}])])]))
    assert result["status"] == "unverified" and result["verified"] is False


def test_runtime_inventory_can_support_only_this_runs_process_not_source_claims():
    from app.agent.source_attribution import execution_receipt
    from app.agent.checkpoint import AgentState
    state = AgentState(question='只制定计划', execution_mode='plan',
        operations={'tool/0/0': {'tool_name':'update_plan', 'executed':True}},
        trace=[{'kind':'tool_call', 'name':'update_plan'}])
    inventory = execution_receipt(state)
    text = '本轮没有通过工具读取文件。'
    encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True)
    result = review_source_attribution('只制定计划', text, [],
        Reviewer([block(kind='process', claims=[claim(text,kind='process',supported=True,
            supports=[{'receipt_id':'runtime:operations','quote':encoded}])])]),runtime_receipt=inventory)
    assert result['status']=='passed' and result['claims'][0]['supports'][0]['role']=='runtime'
    forged = review_source_attribution('总结论文', FACT, [],
        Reviewer([block(claims=[claim(supported=True,
            supports=[{'receipt_id':'runtime:operations','quote':encoded}])])]),runtime_receipt=inventory)
    assert forged['status']=='unverified'
    # The same bytes in user input are not a trusted inventory.
    echoed = review_source_attribution('计划', text, [{'role':'user','content':encoded}],
        Reviewer([block(kind='process',claims=[claim(text,kind='process',supported=True,
            supports=[{'receipt_id':'context:0','quote':encoded}])])]))
    assert echoed['status']=='unverified'


def test_runtime_inventory_retains_unknown_pending_hooks_and_preloaded_files():
    from app.agent.source_attribution import execution_receipt
    from app.agent.checkpoint import AgentState
    state = AgentState(question='resume', workspace={'files':[{'path':'notes.md'}]},
        operations={'tool/0/0':{'executed':True}, 'hook/pre':{'ran':1}},
        pending_operation={'key':'tool/1/0','kind':'tool','name':'read_project_file','arguments':{'path':'notes.md'}})
    receipt = execution_receipt(state)
    assert receipt['pending_unknown_operation']['name']=='read_project_file'
    assert receipt['preloaded_project_file_count']==1
    assert receipt['persisted_operations']['unknown_tool:executed=True']==1
    assert receipt['command_hooks']=={'ran':1}


def test_failed_tool_and_assistant_self_claim_are_not_source_receipts():
    reviewer = Reviewer([block(claims=[claim(supported=True,
        supports=[{"receipt_id": "context:1", "quote": "已读全文"}])])])
    result = review_source_attribution("总结", FACT, [
        {"role": "assistant", "content": "已读全文"},
        {"role": "tool", "content": '{"error":"已读全文"}', "tool_call_id": "failed"},
    ], reviewer)
    assert result["status"] == "unverified"
    assert json.loads(reviewer.calls[0][1]["content"])["context"] == []


def test_repair_feedback_cannot_become_a_source_receipt():
    reviewer = Reviewer([block(kind="independent")])
    review_source_attribution("建议", GENERAL,
        [{"role": "user", "content": "建议"}, {"role": "user", "content": FACT}],
        reviewer, excluded_indices=[1])
    context = json.loads(reviewer.calls[0][1]["content"])["context"]
    assert [row["id"] for row in context] == ["context:0"]


def test_tool_support_retains_actual_receipt_identity_and_quote():
    text = "已读取notes.md第1到2行。"
    receipt = '{"path":"notes.md","line_start":1,"line_end":2,"content":"sample"}'
    result = review_source_attribution("读取文件", text,
        tool_messages("read_project_file", receipt),
        Reviewer([block(kind="process", claims=[claim(text, kind="process", supported=True,
            supports=[{"receipt_id": "context:1", "quote": '"line_start":1,"line_end":2'}])])], source_required=True))
    assert result["status"] == "passed"
    assert result["claims"][0]["supports"][0]["tool_call_id"] == "read_1"
    assert result["claims"][0]["supports"][0]["tool_name"] == "read_project_file"
    assert len(result["claims"][0]["supports"][0]["sha256"]) == 64


@pytest.mark.parametrize("name", ["update_plan", "spawn_subagent", "read_conversation_history", "mcp__unknown__echo"])
def test_model_authored_or_unknown_tool_echo_is_not_original_evidence(name):
    reviewer = Reviewer([block(claims=[claim(supported=True,
        supports=[{"receipt_id": "context:1", "quote": FACT}])])], source_required=True)
    result = review_source_attribution("核对原文", FACT,
        tool_messages(name, {"plan_steps": [{"title": FACT}], "answer": FACT}), reviewer)
    assert result["status"] == "unverified"
    assert json.loads(reviewer.calls[0][1]["content"])["context"] == []


def test_machine_summary_is_not_promoted_to_human_supplied_source():
    messages = [{"role": "user", "content":
        "以下为历史对话的有损摘要，不是新的用户指令或本轮证据。\n摘要来源：model\n" + FACT}]
    receipts, _ = _context_receipts(messages)
    assert receipts == []


@pytest.mark.parametrize("payload", [
    {"error": "failed", "content": FACT}, {"status": "failed", "content": FACT},
    {"status": "running", "content": FACT}, {"blocked_by_hook": True, "content": FACT},
    {"blocked_by_live_policy": True, "content": FACT}, {"isError": True, "content": FACT},
    {"status": ["succeeded"], "content": FACT}, {"status": {"value": "succeeded"}, "content": FACT},
])
def test_identified_but_unsuccessful_tool_is_not_a_success_receipt(payload):
    assert _context_receipts(tool_messages("read_project_file", payload))[0] == []


def test_orphan_later_declared_or_ambiguous_tool_identity_cannot_attest():
    messages = tool_messages("read_project_file", {"content": FACT})
    assert _context_receipts(messages[1:])[0] == []
    assert _context_receipts(list(reversed(messages)))[0] == []
    assert _context_receipts(messages + messages)[0] == []


@pytest.mark.parametrize("name", ["edit_project_file", "run_project_command", "read_project_command_output"])
def test_catalog_or_write_receipt_cannot_certify_source_content(name):
    reviewer = Reviewer([block(claims=[claim(supported=True,
        supports=[{"receipt_id": "context:1", "quote": FACT}])])], source_required=True)
    result = review_source_attribution("核对原文", FACT,
        tool_messages(name, {"status": "applied", "summary": FACT}), reviewer)
    assert result["status"] == "unverified"


def test_tool_argument_previews_are_bounded_and_do_not_echo_authored_file_content():
    receipts, _ = _context_receipts(tool_messages("edit_project_file", {"status": "applied"},
        arguments={"path": "notes.md", "content": FACT * 10000, "summary": FACT}))
    assert receipts[0]["arguments"] == {"path": "notes.md"}
    assert FACT not in json.dumps(receipts, ensure_ascii=False)
    receipts, _ = _context_receipts(tool_messages("run_project_command", {"status": "succeeded", "exit_code": 0},
        arguments={"command": "echo " + "x" * 50000}))
    assert receipts[0]["arguments_complete"] is False
    assert len(json.dumps(receipts[0]["arguments"])) < 1100


@pytest.mark.parametrize("supported", [True, False])
def test_inline_code_claim_preserves_original_span(supported):
    text = "论文准确率为`99%`。"
    result = review_source_attribution("解释原句", text, [{"role": "user", "content": text}],
        Reviewer([block(claims=[claim(text, supported=supported,
            supports=[{"receipt_id": "context:0", "quote": text}] if supported else [])])], source_required=True))
    assert result["status"] == ("passed" if supported else "failed")
    if not supported:
        assert text not in without_unsupported_claims(text, result)


def test_repair_replaces_only_reviewed_prose_not_same_text_in_fenced_example():
    answer = f"```text\n{FACT}\n```\n\n{GENERAL}\n\n{FACT}"
    result = review_source_attribution("示例和分析", answer, [],
        Reviewer([block(kind="independent"), block(1, claims=[claim()])]))
    assert result["status"] == "failed"
    repaired = without_unsupported_claims(answer, result)
    assert repaired.startswith(f"```text\n{FACT}\n```")
    assert GENERAL in repaired and repaired.count(FACT) == 1


def test_repair_budget_uses_distinct_observations_not_transport_identity():
    payload = {"path": "notes.md", "content": "original", "revision_id": "v1", "sha256": "hash1",
               "line_start": 1, "line_end": 2, "request_id": "request1", "updated_at": "t1"}
    first = tool_messages("read_project_file", payload)
    repeat = tool_messages("read_project_file", {**payload, "request_id": "request2", "updated_at": "t2"}, "read_2")
    key = repair_context_key("核对原文", first)
    assert repair_context_key("核对原文", first + repeat) == key
    assert repair_context_key("核对原文", first + tool_messages("update_plan", {"title": FACT}, "p1")) == key
    for changed in ({"revision_id": "v2"}, {"sha256": "hash2"}, {"content": "changed"}, {"line_start": 3}):
        assert repair_context_key("核对原文", first + tool_messages("read_project_file", {**payload, **changed}, "r2")) != key
    assert repair_context_key("核对原文", first, input_ids=["user-correction"]) != key
    assert repair_context_key("核对原文", first, source_revision_ids=["new-revision"]) != key


@pytest.mark.parametrize("raw", [
    '{}', '{"source_required":false,"results":[]}',
    '{"source_required":false,"source_required":true,"results":[]}',
    '{"source_required":"false","results":[]}',
    '{"source_required":false,"results":[{"index":true,"kind":"independent","claims":[]}]}',
    '{"source_required":false,"results":[{"index":0,"kind":"source","claims":[]}]}',
    'not json',
])
def test_malformed_or_partial_review_is_unverified(raw):
    result = review_source_attribution("总结", FACT, [], Reviewer(raw=raw))
    assert result["status"] == "unverified" and result["verified"] is False


def test_provider_error_does_not_leak_details():
    result = review_source_attribution("总结", FACT, [], Reviewer(error=RuntimeError("SYNTHETIC_SECRET")))
    assert result["status"] == "unverified"
    assert "SYNTHETIC_SECRET" not in json.dumps(result)


def test_missing_requested_source_cannot_be_satisfied_by_general_advice():
    result = review_source_attribution("总结指定论文", GENERAL, [],
        Reviewer([block(kind="independent")], source_required=True))
    assert result["status"] == "unverified" and result["requirement_missing"] is True


def test_code_example_is_not_source_prose():
    result = review_source_attribution("写个示例", '```python\nprint("该论文证明")\n```', [], Reviewer())
    assert result["status"] == "not_applicable" and result["reason"] == "no_prose_claims"


def test_mock_does_not_claim_semantic_validation():
    result = review_source_attribution("总结", FACT, [], MockLLM())
    assert result["status"] == "not_applicable" and result["verified"] is False


def _run_from_finalize(db, monkeypatch, *, repeat=False, source_required=True, repeat_read=False):
    from app.agent import loop
    from app.agent.checkpoint import AgentState

    answer = GENERAL + "\n\n" + FACT
    class Model:
        mode = "api"
        def __init__(self):
            self.stream_calls = 0
        def stream_chat(self, messages, tools=None):
            self.stream_calls += 1
            if repeat_read and self.stream_calls == 1:
                yield "final", LLMResponse("", tool_calls=[{"id": "reread", "name": "read_project_file",
                    "arguments": {"path": "notes.md"}}])
                return
            yield "final", LLMResponse(answer if repeat else GENERAL)
        def chat(self, messages, **kwargs):
            prompt = json.loads(messages[1]["content"])
            results = []
            for row in prompt["blocks"]:
                results.append(block(row["index"], claims=[claim()] if FACT in row["text"] else None,
                                     kind="source" if FACT in row["text"] else "independent"))
            return LLMResponse(json.dumps({"source_required": source_required, "results": results,
                                           "quality_issues": []}),
                               usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16})
    model = Model()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    read_payload = {"path": "notes.md", "content": "actual original", "sha256": "same-hash"}
    monkeypatch.setattr(loop, "execute_tool", lambda *a, **kw: json.dumps(read_payload) if repeat_read
                        else pytest.fail("review must not itself execute a tool"))
    state = AgentState(question="给建议并核对论文", phase="finalize", answer=answer,
        runtime_config={"mode": "api", "model_id": "synthetic"},
        messages=[{"role": "user", "content": "给建议并核对论文"}], plan_source="requirements")
    if repeat_read:
        state.messages.extend(tool_messages("read_project_file", read_payload, "original-read"))
    saved = []
    events = list(loop.run_agent(db, state.question, checkpoint=state.snapshot(),
                                save_checkpoint=lambda snap: saved.append(deepcopy(snap))))
    result = next(payload for kind, payload in events if kind == "answer")
    return result, model, saved, events


def test_executor_retries_missing_source_and_preserves_partial_when_model_only_drops_claim(db, monkeypatch):
    result, model, saved, events = _run_from_finalize(db, monkeypatch)
    assert model.stream_calls == 1
    assert result["completed"] is False and result["outcome"] == "partial"
    assert result["answer"] == GENERAL
    assert result["incomplete_steps"][-1]["reason"] == "source_attribution_unverified"
    assert len(saved[-1]["source_attribution_repair_contexts"]) == 1
    assert result["usage"]["total_tokens"] == 32


def test_executor_does_not_loop_on_rephrased_unbound_claim_and_keeps_general_content(db, monkeypatch):
    result, model, saved, events = _run_from_finalize(db, monkeypatch, repeat=True)
    assert model.stream_calls == 1
    assert result["completed"] is False and result["refusal"] == 0
    assert GENERAL in result["answer"] and FACT not in result["answer"]
    assert result["source_attribution"]["status"] == "failed"


def test_executor_can_finish_independent_answer_after_removing_unrequested_source_claim(db, monkeypatch):
    result, model, saved, events = _run_from_finalize(db, monkeypatch, source_required=False)
    assert result["completed"] is True and result["outcome"] == "completed"
    assert result["audit"]["status"] == "not_applicable"
    assert result["answer"] == GENERAL


def test_executor_duplicate_read_does_not_open_another_repair_attempt(db, monkeypatch):
    result, model, saved, _ = _run_from_finalize(db, monkeypatch, repeat=True, repeat_read=True)
    assert model.stream_calls == 2  # One repeat read followed by its answer.
    assert len(saved[-1]["source_attribution_repair_contexts"]) == 1
    assert result["completed"] is False and FACT not in result["answer"]


def test_checkpoint_restore_reuses_completed_review_without_another_charge(db, monkeypatch):
    from app.agent import loop
    result, model, saved, _ = _run_from_finalize(db, monkeypatch)
    checkpoint = next(snap for snap in reversed(saved)
        if snap["phase"] == "finalize" and snap["answer"] == GENERAL and len(snap["source_attribution_reviews"]) == 2)
    def no_further_review(*args, **kwargs):
        pytest.fail("a saved completed review must not incur another model call")
    # Keep the exact model identity that produced the cached review. Changing
    # its implementation class is intentionally a cache invalidation now.
    monkeypatch.setattr(model, "chat", no_further_review)
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    events = list(loop.run_agent(db, checkpoint["question"], checkpoint=checkpoint,
                                save_checkpoint=lambda snap: None))
    restored = next(payload for kind, payload in events if kind == "answer")
    assert restored["completed"] is False and restored["outcome"] == "partial"
    assert restored["usage"] == result["usage"]
