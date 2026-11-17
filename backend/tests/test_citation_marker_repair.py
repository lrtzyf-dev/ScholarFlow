"""Exact annotation mechanics only; no real model or semantic-pass claims."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.agent import citation_marker_repair as repair
from app.llm import LLMResponse, MockLLM
from app.rag.citation import citation_gate
from tests.test_citation_review_diagnostics import provider  # noqa: F401 -- isolated synthetic HTTP fixture


EVIDENCE = [{"id": "e1", "paper_title": "Fabricated Study A", "page": 1,
             "quote": "Study A evaluated twenty synthetic queries and retrieved twelve relevant items."},
            {"id": "e2", "paper_title": "Fabricated Study B", "page": 2,
             "quote": "Study B reported eighteen synthetic participants without randomization."}]
HEADER = "**Study A 数据（第1页）**"
FACT = "Study A采用二十个合成查询进行评估。"
SECOND = "Study B报告十八名合成受试者。"


class Model:
    mode = "api"

    def __init__(self, edits=(), *, raw=None, error=None, tool_calls=None):
        self.edits, self.raw, self.error = edits, raw, error
        self.tool_calls, self.calls = tool_calls, []

    def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        if self.error:
            raise self.error
        return LLMResponse(self.raw if self.raw is not None else json.dumps({"edits": self.edits}, ensure_ascii=False),
                           tool_calls=self.tool_calls, usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16})


def scoped(answer, *, kinds=None, required=None):
    gate = citation_gate(answer, [e["id"] for e in EVIDENCE])
    candidates = gate["unsupported"]
    kinds = kinds or ["source"] * len(candidates)
    required = required if required is not None else [True] * len(candidates)
    original = deepcopy(gate)
    gate["unsupported"] = [text for i, text in enumerate(candidates) if required[i]]
    gate["scoped_review"] = {"verified": True, "protocol": repair.PROTOCOL,
        "original_total_claims": original["total_claims"], "original_marked_claims": original["marked_claims"],
        "original_unsupported_count": len(candidates), "decisions": [
            {"index": i, "text": text, "kind": kinds[i], "requires_citation": required[i]}
            for i, text in enumerate(candidates)]}
    return gate


def edit(text, index=0, ids=None):
    return {"candidate_index": index, "text": text, "evidence_ids": ids if ids is not None else ["e1"]}


def unchanged(answer, gate, model, **kwargs):
    result = repair.repair_citation_markers(answer, EVIDENCE, gate, model, **kwargs)
    assert result["answer"] == answer and not result["changed"] and not result["edits"], result
    assert result["status"] == "unverified" and result["semantic_verification"] == "not_performed"
    assert "passed" not in result and "verified" not in result
    return result


def test_only_missing_study_heading_gets_marker_without_rewriting_any_text():
    answer = HEADER + "\n\n" + FACT + "[^e1]"
    gate, original = scoped(answer), deepcopy(answer)
    before_gate = deepcopy(gate)
    recorded = []
    model = Model([edit(HEADER)])
    result = repair.repair_citation_markers(answer, EVIDENCE, gate, model, on_usage=recorded.append)
    assert result["answer"] == HEADER + "[^e1]\n\n" + FACT + "[^e1]"
    assert result["changed"] and result["status"] == "applied" and result["annotation_only"]
    assert result["semantic_verification"] == "not_performed" and "passed" not in result
    assert result["missing_before"] == 1 and result["missing_after"] == 0
    assert gate == before_gate and answer == original
    record = result["edits"][0]
    assert record == {"candidate_index": 0, "start": 0, "end": len(HEADER), "insert_at": len(HEADER),
                      "text_sha256": hashlib.sha256(HEADER.encode()).hexdigest(), "evidence_ids": ["e1"],
                      "inserted_markers": "[^e1]"}
    assert recorded == [{"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}]
    payload = json.loads(model.calls[0][0][1]["content"])
    assert payload["full_answer"] == answer and payload["candidates"] == [{"candidate_index": 0, "text_prefix": HEADER}]
    assert model.calls[0][1] == {"temperature": 0}


def test_multiple_proposals_only_insert_markers_at_original_offsets_in_canonical_order():
    answer = FACT + "\n\n" + SECOND
    result = repair.repair_citation_markers(answer, EVIDENCE, scoped(answer, kinds=["derived", "source"]),
        Model([edit(SECOND, 1, ["e2", "e1"]), edit(FACT)]))
    assert result["answer"] == FACT + "[^e1]\n\n" + SECOND + "[^e1][^e2]"
    assert [e["candidate_index"] for e in result["edits"]] == [0, 1]
    assert result["missing_after"] == 0 and result["missing_before"] == 2


def test_one_sentence_inside_same_line_can_be_patched_without_touching_neighbor():
    answer = FACT + SECOND + "[^e2]"
    result = repair.repair_citation_markers(answer, EVIDENCE, scoped(answer), Model([edit(FACT)]))
    assert result["answer"] == FACT + "[^e1]" + SECOND + "[^e2]"


def test_inline_numeric_values_are_preserved_and_match_current_mechanical_prefix_for_exact_annotation():
    answer = "Study A采用`20`个合成查询，检出`12`项结果，复算命中率为`60%`。"
    gate = scoped(answer, kinds=["derived"])
    assert gate["unsupported"] == [answer.replace("`", "")]
    result = repair.repair_citation_markers(answer, EVIDENCE, gate, Model([edit(answer)]))
    assert result["answer"] == answer + "[^e1]" and result["changed"]
    assert result["annotation_only"] and result["semantic_verification"] == "not_performed"
    assert result["missing_after"] == 0
    reused = repair.reuse_citation_marker_annotation(answer, EVIDENCE, gate, result)
    assert reused["answer"] == result["answer"] and reused["edits"] == result["edits"]
    assert reused["changed"] and reused["semantic_verification"] == "not_performed"


def test_code_example_markers_cannot_supply_citations_or_become_annotation_targets():
    answer = "Study A采用`20`个合成查询，示例标记为`[^e1]`。"
    baseline = citation_gate(answer, ["e1", "e2"])
    assert baseline["marked_claims"] == 0 and not baseline["passed"]
    value = unchanged(answer, scoped(answer), Model([edit(answer)]))
    assert value["reason"] == "not_unique_complete_prose"
    answer = "```text\n" + FACT + "[^e1]\n```\n\n" + FACT
    baseline = citation_gate(answer, ["e1", "e2"])
    assert baseline["marked_claims"] == 0 and baseline["unsupported"] == [FACT]
    unchanged(answer, scoped(answer), Model([edit(FACT)]))


def test_bullet_body_and_ordered_item_normalization_use_original_production_prefix():
    for prefix in ("- ", "1. ", "  * "):
        answer = prefix + FACT
        result = repair.repair_citation_markers(answer, EVIDENCE, scoped(answer), Model([edit(answer.strip())]))
        assert result["answer"] == answer + "[^e1]", result


def test_long_prefix_requires_the_entire_unique_original_sentence():
    sentence = "Study A研究对象包括" + "仅供验收的合成查询记录" * 12 + "。"
    gate = scoped(sentence)
    assert len(gate["unsupported"][0]) == 80
    unchanged(sentence, gate, Model([edit(sentence[:80])]))
    result = repair.repair_citation_markers(sentence, EVIDENCE, gate, Model([edit(sentence)]))
    assert result["answer"] == sentence + "[^e1]"


def test_changed_word_whitespace_partial_fragment_and_other_sentence_reject_entire_batch():
    answer = FACT + "\n\n" + SECOND
    for text in (FACT.replace("二十", "三十"), " " + FACT, FACT[:-1], SECOND, FACT + "\n\n" + SECOND, FACT + SECOND):
        unchanged(answer, scoped(answer), Model([edit(text)]))
    unchanged(answer, scoped(answer), Model([edit(FACT), edit(SECOND.replace("十八", "十九"), 1)]))


def test_english_multi_sentence_or_wrapped_sentence_is_conservatively_left_for_rewrite():
    for answer in ("Study A sampled twenty queries. Study B sampled eighteen records.",
                   "Study A研究对象包括二十个\n合成查询并报告检索命中率。"):
        unchanged(answer, scoped(answer), Model([edit(answer)]))


def test_ambiguous_prefixes_repeated_original_or_literal_code_cannot_be_targeted():
    duplicate = FACT + "\n\n" + FACT
    model = Model([edit(FACT)])
    unchanged(duplicate, scoped(duplicate), model)
    assert not model.calls
    answer = "```text\n" + FACT + "\n```\n\n" + FACT
    unchanged(answer, scoped(answer), Model([edit(FACT)]))
    answer = "`" + FACT + "`\n\n" + FACT
    unchanged(answer, scoped(answer), Model([edit(FACT)]))


def test_process_independent_uncertain_and_mixed_candidates_are_never_given_source_markers():
    for kind in ("process", "independent", "uncertain", "mixed"):
        model = Model([edit(FACT)])
        unchanged(FACT, scoped(FACT, kinds=[kind]), model)
        assert not model.calls
    answer = FACT + "\n\n" + SECOND
    gate = scoped(answer, kinds=["source", "process"])
    model = Model([edit(FACT), edit(SECOND, 1)])
    unchanged(answer, gate, model)
    sent = json.loads(model.calls[0][0][1]["content"])
    assert sent["candidates"] == [{"candidate_index": 0, "text_prefix": FACT}]


def test_scope_exemptions_do_not_get_reintroduced_by_a_fake_candidate_index():
    answer = FACT + "\n\n" + SECOND
    gate = scoped(answer, kinds=["independent", "source"], required=[False, True])
    unchanged(answer, gate, Model([edit(FACT)]))
    result = repair.repair_citation_markers(answer, EVIDENCE, gate, Model([edit(SECOND)]))
    assert result["answer"] == FACT + "\n\n" + SECOND + "[^e1]"
    assert result["missing_after"] == 1  # Existing exemption is a separate review contract.


def test_missing_stale_or_forged_scope_record_never_dispatches():
    for mutation in ("missing", "verified", "protocol", "index", "text", "original_count", "remaining"):
        gate = scoped(FACT)
        if mutation == "missing": gate.pop("scoped_review")
        elif mutation == "verified": gate["scoped_review"]["verified"] = 1
        elif mutation == "protocol": gate["scoped_review"]["protocol"] = "other"
        elif mutation == "index": gate["scoped_review"]["decisions"][0]["index"] = True
        elif mutation == "text": gate["scoped_review"]["decisions"][0]["text"] = SECOND
        elif mutation == "original_count": gate["scoped_review"]["original_unsupported_count"] += 1
        else: gate["unsupported"] = [SECOND]
        model = Model([edit(FACT)])
        unchanged(FACT, gate, model)
        assert not model.calls


def test_invalid_or_duplicate_evidence_and_existing_invalid_markers_never_dispatch():
    for evidence in ([{**EVIDENCE[0], "id": "e01"}], [EVIDENCE[0], EVIDENCE[0]],
                     [{**EVIDENCE[0], "quote": ""}], []):
        model = Model([edit(FACT)])
        result = repair.repair_citation_markers(FACT, evidence, scoped(FACT), model)
        assert not result["changed"] and not model.calls
    answer = FACT + "[^e999]"
    model = Model([edit(FACT)])
    unchanged(answer, scoped(answer), model)
    assert not model.calls


def test_strict_proposal_schema_unknown_ids_duplicate_keys_and_boolean_index_reject():
    invalid = [
        "not json", '```json\n{"edits":[]}\n```', '{"edits":[],"extra":NaN}',
        '{"edits":[],"edits":[]}', json.dumps({"edits": [edit(FACT, True)]}),
        json.dumps({"edits": [edit(FACT, 1)]}), json.dumps({"edits": [edit(FACT, ids=["e999"])]}),
        json.dumps({"edits": [edit(FACT, ids=["e1", "e1"])]}),
        json.dumps({"edits": [edit(FACT), edit(FACT)]}),
        json.dumps({"edits": [{**edit(FACT), "kind": "source"}]}),
        json.dumps({"edits": [{"candidate_index": 0, "text": FACT}]}),
        json.dumps({"edits": [None]}), json.dumps({"edits": []}),
        json.dumps({"answer": FACT + "[^e1]"}),
    ]
    for raw in invalid:
        unchanged(FACT, scoped(FACT), Model(raw=raw))


def test_marker_cannot_launder_multiple_unedited_bullets_through_intro_inheritance():
    answer = HEADER + "\n\n- " + FACT
    assert len(scoped(answer)["unsupported"]) == 2
    value = unchanged(answer, scoped(answer), Model([edit(HEADER)]))
    assert value["reason"] == "no_exact_single_reduction"


def test_escaped_insertion_with_no_real_gate_improvement_rejects():
    answer = "Study A材料结尾包含一个保留反斜线\\"
    value = unchanged(answer, scoped(answer), Model([edit(answer)]))
    assert value["reason"] == "no_exact_single_reduction"


def test_usage_kept_for_invalid_json_and_tool_calls_without_private_provider_text():
    for model in (Model(raw="PRIVATE_PROVIDER_BODY"), Model([edit(FACT)], tool_calls=[{"id": "private"}])):
        usage = []
        result = unchanged(FACT, scoped(FACT), model, on_usage=usage.append)
        assert len(usage) == 1 and usage[0]["total_tokens"] == 16
        assert "PRIVATE" not in json.dumps(result)
    result = unchanged(FACT, scoped(FACT), Model(error=RuntimeError("PRIVATE_TOKEN")))
    assert "PRIVATE_TOKEN" not in json.dumps(result)


def test_resource_boundary_is_propagated_instead_of_hidden_as_annotation_failure():
    from app.resource_budget import ResourceBudgetExceeded
    with pytest.raises(ResourceBudgetExceeded):
        repair.repair_citation_markers(FACT, EVIDENCE, scoped(FACT), Model(error=ResourceBudgetExceeded("budget")))


def test_mock_and_unknown_model_modes_never_make_annotation_calls():
    for mode in ("mock", "unknown", None):
        model = Model([edit(FACT)])
        model.mode = mode
        unchanged(FACT, scoped(FACT), model)
        assert not model.calls
    class Pretend(MockLLM):
        mode = "api"
        def chat(self, *args, **kwargs):
            pytest.fail("A mock must not approve source markers")
    unchanged(FACT, scoped(FACT), Pretend())


def test_production_scope_reviewer_result_is_accepted_without_semantic_pass():
    from app.agent.audit import review_citation_scope
    baseline = citation_gate(FACT, [e["id"] for e in EVIDENCE])
    reviewer = Model(raw=json.dumps({"results": [{"index": 0, "requires_citation": True,
        "reason": "This is a source statement.", "kind": "source", "process_scope": "none", "supports": []}]}))
    gate = review_citation_scope("Describe Study A.", FACT, EVIDENCE, baseline, reviewer, messages=[])
    assert gate["scoped_review"]["verified"]
    result = repair.repair_citation_markers(FACT, EVIDENCE, gate, Model([edit(FACT)]))
    assert result["answer"] == FACT + "[^e1]" and result["semantic_verification"] == "not_performed"


def test_real_api_client_uses_one_synthetic_http_and_normal_usage_receipt(db,provider,monkeypatch):
    import httpx
    from app import config,call_ledger
    from app.llm import APIClient
    responses,requests=provider
    monkeypatch.setattr(config.settings,"llm_base_url","https://api.deepseek.com/v1")
    monkeypatch.setattr(config.settings,"llm_model","deepseek-flash")
    monkeypatch.setattr(config.settings,"reasoning_effort","")
    responses.append(httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{
        "content":json.dumps({"edits":[edit(FACT)]}),"reasoning_content":"PRIVATE_REASONING"}}],
        "usage":{"prompt_tokens":20,"completion_tokens":8,"total_tokens":28}}))
    usage=[]
    result=repair.repair_citation_markers(FACT,EVIDENCE,scoped(FACT),APIClient(synthetic=True),on_usage=usage.append)
    assert result["answer"]==FACT+"[^e1]" and len(requests)==1
    body=json.loads(requests[0].content)
    assert body["model"]=="deepseek-flash" and body["reasoning_effort"]=="low" and body["max_tokens"]==8192
    assert usage==[{"prompt_tokens":20,"completion_tokens":8,"total_tokens":28}]
    assert "PRIVATE_REASONING"not in json.dumps(result)
    ledger=call_ledger.summarize_calls(db=db,include_synthetic=True)
    assert ledger["call_count"]==ledger["synthetic_call_count"]==1


def test_cached_annotation_revalidation_is_pure_and_binds_every_original_edit_field(monkeypatch):
    answer=FACT+"\n\n"+SECOND
    gate=scoped(answer)
    result=repair.repair_citation_markers(answer,EVIDENCE,gate,Model([edit(FACT)]))
    assert result["changed"]
    def forbidden(*args,**kwargs):
        pytest.fail("Cached validation made an HTTP/model call")
    monkeypatch.setattr(Model,"chat",forbidden)
    current=deepcopy(gate)
    current["scoped_review"]["decisions"][0]["reason"]="A newly worded source explanation."
    valid=repair.reuse_citation_marker_annotation(answer,EVIDENCE,current,result)
    assert valid["answer"]==result["answer"]and valid["changed"]and valid["annotation_only"]
    for mutation in ("answer","start","end","insert_at","text_sha256","evidence_ids","inserted_markers","candidate_index","semantic"):
        old=deepcopy(result)
        if mutation=="answer":old["answer"]+="Changed original words."
        elif mutation=="start":old["edits"][0]["start"]+=1
        elif mutation=="end":old["edits"][0]["end"]-=1
        elif mutation=="insert_at":old["edits"][0]["insert_at"]=True
        elif mutation=="text_sha256":old["edits"][0]["text_sha256"]="0"*64
        elif mutation=="evidence_ids":old["edits"][0]["evidence_ids"]=["e999"]
        elif mutation=="inserted_markers":old["edits"][0]["inserted_markers"]="[^e2]"
        elif mutation=="candidate_index":old["edits"][0]["candidate_index"]=1
        else:old["semantic_verification"]="passed"
        rejected=repair.reuse_citation_marker_annotation(answer,EVIDENCE,current,old)
        assert not rejected["changed"]and rejected["answer"]==answer,mutation


def test_cached_annotation_never_overrides_current_scope_or_changed_source_or_answer():
    result=repair.repair_citation_markers(FACT,EVIDENCE,scoped(FACT),Model([edit(FACT)]))
    for kind in ("process","independent","mixed","uncertain"):
        rejected=repair.reuse_citation_marker_annotation(FACT,EVIDENCE,scoped(FACT,kinds=[kind]),result)
        assert not rejected["changed"]and rejected["answer"]==FACT
    wrong_ids=[{**EVIDENCE[0],"id":"e7"}]
    assert not repair.reuse_citation_marker_annotation(FACT,wrong_ids,scoped(FACT),result)["changed"]
    changed_quote=deepcopy(EVIDENCE);changed_quote[0]["quote"]="A different version with the same local evidence ID."
    assert not repair.reuse_citation_marker_annotation(FACT,changed_quote,scoped(FACT),result)["changed"]
    changed=FACT.replace("二十","三十")
    assert not repair.reuse_citation_marker_annotation(changed,EVIDENCE,scoped(changed),result)["changed"]
