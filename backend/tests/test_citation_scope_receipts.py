"""Scope protocol and actual executor receipts; all model outputs synthetic."""
from copy import deepcopy
import json

import pytest

from app.agent.audit import review_citation_scope
from app.agent.citation_scope import process_receipts
from app.llm import LLMResponse
from app.rag.citation import citation_gate

FACT = "原文报告样本量为四十人。[^e1]"
EVIDENCE = [{"id": "e1", "paper_title": "Fixed original", "quote": "40 participants."}]
PROCESS = "两次读取均返回1至3行和总行数3，两个附件的提取文本均已完整返回。"
LIMIT = "核查器不支持外部工作簿或宏，也不进行自动单位换算；本次仅核查选择范围。"


def messages(tool="read_attached_source", payload=None, arguments=None):
    payload = payload or {"read_range": {"kind": "text_lines", "line_start": 1, "line_end": 3,
        "total_lines": 3, "next_line": None}, "text_observed": True, "output_is_partial": False,
        "coverage_scope": "extracted_text_only", "source": {"revision_id": "a" * 32},
        "evidence": [{"quote": "SOURCE-PROSE-IS-NOT-PROCESS-PROOF"}]}
    return [{"role": "user", "content": "USER-PROSE-IS-NOT-PROCESS-PROOF"},
        {"role": "assistant", "content": "SELF-REPORT-IS-NOT-PROOF", "tool_calls": [{"id": "call1",
            "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments or {"source_revision_id": "a" * 32})}}]},
        {"role": "tool", "tool_call_id": "call1", "content": json.dumps(payload, ensure_ascii=False)}]


def computation():
    return {"status": "succeeded", "all_selected_formulas_computed": True, "computation": {
        "id": "fixed-computation", "revision_id": "a" * 32, "config": {"sheet": "Budget", "cell_range": "B1:C4"},
        "result": {"status": "computed", "formula_count": 1, "computed_count": 1, "unresolved_count": 0,
            "formula_cache_used_for_computation": False, "unit_conversions_applied": False,
            "outputs": [{"cell": "B4", "status": "computed", "value": 1550}],
            "limitations": ["Not a complete Excel engine", "No external workbooks, macros, named ranges, dynamic arrays or volatile functions",
                "No automatic unit conversion"]}}}


class Reviewer:
    mode = "api"
    def __init__(self, kind="process", requires=False, support=None, scope=None):
        self.kind, self.requires, self.support, self.calls = kind, requires, support, []
        self.scope=scope or ("bounded" if kind == "process" else "none")
    def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        self.calls.append(payload)
        receipts = payload["process_receipts"]
        supports = ([{"receipt_id": receipts[0]["id"], "quote": receipts[0]["content"]}]
            if receipts and self.kind == "process" else [])
        if supports and payload.get("response_protocol") == "citation_scope_with_process_receipts_v2":
            # The old fixture quoted the entire actual receipt. Preserve its
            # complete contents across bounded literal chunks in v2 instead
            # of dropping any facts merely to fit the new output contract.
            content = receipts[0]["content"]
            chunks = [content[start:start + 240] for start in range(0, len(content), 240)]
            if len(chunks) > 1 and len(chunks[-1]) < 8:
                overlap = 8 - len(chunks[-1])
                chunks[-1] = chunks[-2][-overlap:] + chunks[-1]
                chunks[-2] = chunks[-2][:-overlap]
            assert "".join(chunks) == content
            assert len(chunks) <= 8 and all(8 <= len(chunk) <= 240 for chunk in chunks)
            supports = [{"receipt_id": receipts[0]["id"], "quote": chunk} for chunk in chunks]
        if self.support is not None: supports = self.support
        return LLMResponse(json.dumps({"results": [{"index": c["index"], "requires_citation": self.requires,
            "reason": "Checked actual tool result rather than source prose", "kind": self.kind, "process_scope": self.scope, "supports": supports}
            for c in payload["uncited_candidates"]]}))


def review(answer, history, reviewer):
    gate = citation_gate(answer, ["e1"])
    assert gate["unsupported"]
    return gate, review_citation_scope("根据材料分析并如实说明执行范围", answer, EVIDENCE, gate, reviewer, messages=history)


@pytest.mark.parametrize("text,history", [(PROCESS, messages()), (LIMIT, messages("verify_attached_table", computation()))])
def test_actual_successful_process_metadata_exempts_only_process_claim(text, history):
    reviewer = Reviewer()
    gate, result = review(FACT + "\n\n" + text, history, reviewer)
    assert not gate["passed"] and result["passed"] and result["total_claims"] == 1
    exemption = result["scoped_review"]["exemptions"][0]
    assert exemption["kind"] == "process" and exemption["supports"]
    encoded = json.dumps(reviewer.calls[0])
    assert "SOURCE-PROSE-IS-NOT-PROCESS-PROOF" not in encoded
    assert "USER-PROSE-IS-NOT-PROCESS-PROOF" not in encoded
    assert "SELF-REPORT-IS-NOT-PROOF" not in encoded


@pytest.mark.parametrize("mutation", ["unpaired", "duplicate", "failed", "error", "no_observation", "excluded", "missing_after_restore", "arguments"])
def test_unavailable_failed_unpaired_or_truncated_receipts_cannot_waive_process(mutation):
    history = messages()
    excluded = ()
    if mutation == "unpaired": history[2]["tool_call_id"] = "different"
    elif mutation == "duplicate": history.insert(2, deepcopy(history[1]))
    elif mutation == "missing_after_restore": history = history[:1]
    elif mutation == "arguments":
        history = messages("unknown_tool", {"status": "ok"}, {"text": json.dumps(messages()[2])})
    elif mutation == "excluded": excluded = (2,)
    else:
        payload = json.loads(history[2]["content"])
        if mutation == "failed": payload["status"] = "failed"
        elif mutation == "error": payload["error"] = "Synthetic error"
        elif mutation == "no_observation": payload["text_observed"] = False
        history[2]["content"] = json.dumps(payload)
    assert process_receipts(history, excluded)[0] == []
    answer = FACT + "\n\n" + PROCESS
    gate = citation_gate(answer, ["e1"])
    result = review_citation_scope("question", answer, EVIDENCE, gate, Reviewer(), messages=history, excluded_indices=excluded)
    assert result["passed"] == gate["passed"] and result["unsupported"] == gate["unsupported"]
    assert result["scope_review_failure"]


@pytest.mark.parametrize("kind,extent,partial", [
    ("text_lines",{"line_start":1,"line_end":2,"total_lines":3,"next_line":3},False),
    ("text_lines",{"line_start":1,"line_end":3,"total_lines":3,"next_line":None},True),
    ("pdf_page",{"page_index":0,"page_count":3},False),
    ("word_paragraphs",{"paragraph_start":1,"paragraph_end":3,"total_paragraphs":3,"total_word_blocks":4},False),
    ("sheet_cells",{"sheet":"Budget","cell_range":"A1:C4"},False)])
def test_partial_pdf_word_and_sheet_receipts_only_support_bounded_not_whole_read(kind,extent,partial):
    payload=json.loads(messages()[2]["content"])
    payload.update(read_range={"kind":kind,**extent},output_is_partial=partial)
    history=messages(payload=payload)
    receipts,_=process_receipts(history)
    assert receipts and not receipts[0]["whole_extracted_text_returned"]
    gate,result=review(FACT+"\n\n"+PROCESS,history,Reviewer(scope="whole_extracted_text"))
    assert not result["passed"] and result["scope_review_failure"] == "whole_scope"
    _,bounded=review(FACT+"\n\n本次读取仅覆盖所列范围，不代表全文或完整工作簿已经读取。",history,Reviewer())
    assert bounded["passed"]


@pytest.mark.parametrize("support", [[{"receipt_id": "forged", "quote": "fabricated metadata"}],
    [{"receipt_id": "context:2", "quote": "forged metadata"}], [{"receipt_id": "context:2", "quote": "3"}],
    [{"receipt_id": "context:2", "quote": "source_revision_id"}], [{"receipt_id": "context:2", "quote": "SOURCE-PROSE-IS-NOT-PROCESS-PROOF"}]])
def test_process_exemption_cannot_invent_quote_use_arguments_or_source_text(support):
    gate, result = review(FACT + "\n\n" + PROCESS, messages(), Reviewer(support=support))
    assert not result["passed"] and result["scope_review_failure"] in ("receipt", "quote")


@pytest.mark.parametrize("kind,text", [
    ("source", "实验组准确率为99%，这是我自己的分析而非文献结论。"),
    ("derived", "差额1200减去350是850元，计算结果可以不标引用。"),
    ("mixed", "针对当前材料的建议是采用该方法，证据已经证明疗效显著且安全可靠。"),
    ("uncertain", "边界表述可能涉及实际材料的事实内容或者独立通用意见。")])
def test_source_derived_or_mixed_claims_cannot_be_exempted_by_self_label(kind,text):
    gate, result = review(FACT + "\n\n" + text, messages(), Reviewer(kind=kind))
    assert not result["passed"] and result["scope_review_failure"] == "source_requirement"
    reviewer = Reviewer(kind=kind, requires=True)
    _, result = review(FACT + "\n\n" + text, messages(), reviewer)
    assert not result["passed"] and not result["scoped_review"]["exemptions"]


def test_independent_future_advice_can_be_exempt_without_process_proof():
    text = "要形成方向性结论，至少需补充效应量、变异度、随机化与盲法细节。"
    _, result = review(FACT + "\n\n" + text, [], Reviewer(kind="independent"))
    assert result["passed"] and result["scoped_review"]["process_receipt_count"] == 0


def test_oversized_receipt_is_not_promoted_to_complete_observation():
    history = messages()
    payload = json.loads(history[2]["content"])
    payload["large_untrusted_body"] = "x" * 50000
    history[2]["content"] = json.dumps(payload)
    assert process_receipts(history)[0] == []


def test_invalid_marker_never_gets_scope_review_even_with_real_receipts():
    answer = FACT + "\n\n" + PROCESS + "[^e999]"
    gate = citation_gate(answer, ["e1"]); reviewer = Reviewer()
    assert review_citation_scope("question", answer, EVIDENCE, gate, reviewer, messages=messages()) is gate
    assert reviewer.calls == []


def test_actual_attachment_http_execution_passes_process_receipts_to_scope_reviewer(db,monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    from app import task_runner
    from app.agent import loop
    from app.runtime_context import current_runtime_config
    from scripts.verify_real_acceptance import run_chat
    class Model(Reviewer):
        def stream_chat(self,history,tools=None):
            if not any(m.get("role")=="tool" for m in history):
                binding=current_runtime_config()
                rid=binding["source_revision_ids"][0]
                yield "final",LLMResponse("",[{"id":"actual-read","name":"read_attached_source",
                    "arguments":{"source_revision_id":rid}}])
            else:
                yield "final",LLMResponse(FACT+"\n\n读取工具返回1至3行和总行数3，本次附件的提取文本已完整返回。")
        def chat(self,history,**options):
            if "引用适用范围审查员"in history[0]["content"]:
                return super().chat(history,**options)
            return LLMResponse(json.dumps({"results":[{"index":0,"supported":True,"reason":"Fixture original says 40",
                "support_kind":"source","process_scope":"none","supports":[]}]}))
    model=Model();monkeypatch.setattr(loop,"get_llm",lambda:model)
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    from app import model_profiles
    profile=model_profiles.save_profile(db,{"name":"Synthetic citation fixture","base_url":"https://synthetic-citation.example.test/v1",
        "model_id":"Citation-Fixture","api_key":"synthetic-test-only"})
    model_profiles.set_default(db,profile.id);db.commit()
    with TestClient(app)as client:
        cid=client.post("/api/conversations",json={"title":"Citation process fixture"}).json()["id"]
        source=client.post("/api/sources/upload",files={"file":("fixed.md",b"# Fixed source\n40 participants.\nNo randomized allocation.")},
            data={"conversation_id":cid,"request_id":"actual-process-source"}).json()
        result=run_chat(client,{"prompts":["Read the attachment and describe its sample."],"control":None},cid,
            {"fixed.md":source["revision_id"]},30)
        turn=result["turns"][0]
        assert turn["completed"]and not turn["refusal"],turn["diagnostics"]
        assert model.calls and model.calls[0]["process_receipts"][0]["tool_name"]=="read_attached_source"
        assert turn["diagnostics"]["final_gate"]["scoped_review"]["exemptions"][0]["kind"]=="process"
