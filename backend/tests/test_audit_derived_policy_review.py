"""Independent typed policy contracts, not real-model semantic acceptance."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.agent.audit import audit_answer, review_citation_scope
from app.agent.citation_scope import PROCESS_INSTRUCTIONS, validation_failure, verified_source_metadata
from app.llm import LLMResponse
from app.main import app
from app.models import Conversation
from app.rag.citation import citation_gate
from app.runtime_context import bind_runtime_config
from app.source_runtime import freeze_sources
from app.source_tools import execute_source_tool
from scripts.verify_real_acceptance import fixture_bytes


def verdict(index=0, *, kind="derived", supported=True, supports=None, process_scope=None, reason=None):
    return {"index": index, "supported": supported,
        "reason": reason or "Synthetic typed verdict; semantic outcome supplied by this test",
        "support_kind": kind,
        "process_scope": process_scope if process_scope is not None else ("bounded" if kind in ("process", "mixed") else "none"),
        "supports": supports or []}


class AuditModel:
    mode = "api"
    def __init__(self, results, inspect=None):
        self.results, self.inspect, self.prompts = results, inspect, []
    def chat(self, history, **kwargs):
        text = history[1]["content"]
        self.prompts.append(text)
        if self.inspect:
            self.inspect(text)
        return LLMResponse(json.dumps({"results": self.results}, ensure_ascii=False))


@pytest.fixture
def csv_source(db):
    cid = "derived-policy-csv"
    db.add(Conversation(id=cid)); db.commit()
    with TestClient(app) as client:
        response = client.post("/api/sources/upload", files={"file": ("scores.csv", fixture_bytes("scores.csv"))},
            data={"conversation_id": cid, "request_id": "derived-policy-fixture"})
    assert response.status_code == 200, response.text
    rid = response.json()["revision_id"]
    frozen = freeze_sources(db, cid, [rid], {"conversation_id": cid})
    with bind_runtime_config(frozen):
        payload = json.loads(execute_source_tool(db, "read_attached_source", {
            "source_revision_id": rid, "sheet": "CSV", "cell_range": "A1:D4"}, conversation_id=cid))
    assert not payload.get("error"), payload
    evidence = [{**entry, "id": f"e{i + 1}"} for i, entry in enumerate(payload["evidence"])]
    db.commit()
    return cid, evidence


def assert_derived_policy(text):
    # These are the actual instructions sent to the auditor, before any
    # synthetic verdict. The test does not pretend to implement an LLM.
    assert "答案中明确展示的简单计算、公式与统计口径属于derived" in text
    assert "不额外要求计算工具回执" in text
    assert "当前句所引全部输入、缺失状态、单位及所展示计算一致" in text
    assert "声称空单元格含数值、错误公式/算术、遗漏输入或单位不明" in text
    assert "不能把手算说成已运行程序" in text
    assert "其他段的引文不能自动替代当前论断所列引用" in text
    assert "运行脚本/Excel/计算工具" in text


def test_actual_fixed_csv_metadata_keeps_missing_and_units_separate_from_answer_policy(db, csv_source):
    cid, evidence = csv_source
    metadata = verified_source_metadata(db, evidence, cid)
    cells = {cell["cell"]: cell for cell in metadata["e1"]["cells"]}
    assert cells["C3"]["missing"] is True and cells["C3"]["value"] == ""
    assert cells["C2"]["value"] == "8" and cells["C4"]["value"] == "10"
    assert all(cells[f"D{row}"]["value"] == "points" for row in (2, 3, 4))
    assert metadata["e1"]["table_semantics"]["values_coerced"] is False


@pytest.mark.parametrize("claim,kind", [
    ("C3是空白的score输入，缺失1条。", "source"),
    ("本回答把空白C3保留为缺失，不按0参与计算。", "derived"),
    ("D2、D3、D4均为points，本回答汇总保留这些原单位而不作换算。", "derived"),
    ("C2的8和C4的10均为points，C3空白不参与，所引非空输入的算术均值为(8+10)/2=9 points，由本回答计算所得。", "derived"),
])
def test_source_and_shown_derived_policy_need_current_source_inputs_but_no_execution_receipt(db, csv_source, claim, kind):
    cid, evidence = csv_source
    def inspect(text):
        assert_derived_policy(text)
        assert "process_receipts=[]" in text
        assert '"cell": "C3"' in text and '"missing": true' in text
        assert '"cell": "D2"' in text and '"value": "points"' in text
        assert claim in text and "引用证据 e1" in text
    model = AuditModel([verdict(kind=kind)], inspect)
    value = audit_answer(db, claim + "[^e1]", evidence, llm=model, messages=[], conversation_id=cid)
    assert value["passed"] and value["known_failed_count"] == value["unknown_count"] == 0
    assert len(model.prompts) == 1


@pytest.mark.parametrize("claim,kind", [
    ("以下计算只使用C3有数值的输入值，不把C3的空缺当作0参与计算。", "source"),
    ("C3的原始score数值为0。", "source"),
    ("本回答由8和10手算的均值为10 points。", "derived"),
    ("本回答把D4的单位CNY与D2的points直接合并，均值为9 points。", "derived"),
    ("对全部非空score求均值时仅使用C2的8并得到8 points。", "derived"),
])
def test_explicit_negative_source_or_derived_verdict_remains_known_failure_without_process_receipt(db, csv_source, claim, kind):
    cid, evidence = csv_source
    model = AuditModel([verdict(kind=kind, supported=False,
        reason="Synthetic confirmed mismatch with fixed missing/input/unit/arithmetic contract")], assert_derived_policy)
    value = audit_answer(db, claim + "[^e1]", evidence, llm=model, messages=[], conversation_id=cid)
    assert value["status"] == "failed" and value["known_failed_count"] == 1
    assert value["unknown_count"] == 0 and len(value["unsupported"]) == 1
    assert not value["passed"] and not value["unverified"]


@pytest.mark.parametrize("tool,kind", [("Python", "process"), ("Excel", "mixed")])
@pytest.mark.parametrize("supported", [True, False])
def test_claim_of_external_execution_never_gets_a_process_pass_without_receipt(db, tool, kind, supported):
    evidence = [{"id": "e1", "quote": "C2=8 points; C4=10 points; C3 is empty."}]
    claim = f"已经实际运行{tool}对当前材料完成复算，得到均值9 points。"
    model = AuditModel([verdict(kind=kind, supported=supported)], assert_derived_policy)
    value = audit_answer(db, claim + "[^e1]", evidence, llm=model, messages=[])
    assert not value["passed"]
    if supported:
        assert value["status"] == "unverified" and value["unknown_count"] == 1
        assert value["known_failed_count"] == 0
    else:
        assert value["status"] == "failed" and value["known_failed_count"] == 1
        assert value["unknown_count"] == 0


@pytest.mark.parametrize("kind", ["process", "mixed"])
def test_source_quote_cannot_be_used_as_a_fabricated_external_execution_receipt(db, kind):
    quote = "C2=8 points; C4=10 points; the mean is 9 points."
    evidence = [{"id": "e1", "quote": quote}]
    fake_support = [{"receipt_id": "e1", "quote": quote}]
    model = AuditModel([verdict(kind=kind, supports=fake_support)])
    value = audit_answer(db, "已经运行Python完成外部复算。[^e1]", evidence, llm=model, messages=[])
    assert value["status"] == "unverified" and value["unknown_count"] == 1
    assert not value["passed"] and value["known_failed_count"] == 0


@pytest.mark.parametrize("mutation", ["process_scope", "receipt_support"])
def test_derived_verdict_cannot_smuggle_process_support_fields(db, mutation):
    result = verdict()
    if mutation == "process_scope":
        result["process_scope"] = "bounded"
    else:
        result["supports"] = [{"receipt_id": "fabricated", "quote": "fabricated completed execution"}]
    value = audit_answer(db, "明确由8和10计算均值为9 points。[^e1]", [{"id": "e1", "quote": "8 and 10 points"}],
        llm=AuditModel([result]), messages=[])
    assert value["status"] == "unverified" and value["unknown_count"] == 1 and not value["passed"]


@pytest.mark.parametrize("kind", ["source", "derived", "mixed", "uncertain"])
def test_method_wording_cannot_waive_typed_current_source_or_derived_candidate(kind):
    claim = "本回答的计算口径声称C3有数值，均值由8和10算为10 points。"
    evidence = [{"id": "e1", "quote": "C3 is empty; C2=8 points; C4=10 points."}]
    gate = citation_gate(claim, ["e1"])
    class ScopeModel:
        mode = "api"
        def chat(self, history, **kwargs):
            assert "不要求伪造计算工具回执" in history[0]["content"]
            assert "空值被说成有数值、错算及单位冲突也不能豁免来源要求" in history[0]["content"]
            return LLMResponse(json.dumps({"results": [{"index": 0, "requires_citation": False,
                "reason": "Synthetic attempted method waiver", "kind": kind, "process_scope": "none", "supports": []}]}))
    value = review_citation_scope("核实数据", claim, evidence, gate, ScopeModel(), messages=[])
    assert value["scope_review_failure"] == "source_requirement"
    assert value["unsupported"] == [claim] and not value["passed"]


def test_derived_scope_with_no_receipt_still_requires_a_citation_before_semantic_audit():
    result = {"index": 0, "requires_citation": True, "reason": "Synthetic derived input requirement",
        "kind": "derived", "process_scope": "none", "supports": []}
    assert validation_failure(result, []) is None
    assert "须引用全部输入、缺失状态与单位" in PROCESS_INSTRUCTIONS
    claim = "本回答根据8 points和10 points手算均值为9 points。"
    gate = citation_gate(claim, ["e1"])
    class Model:
        mode = "api"
        def chat(self, *args, **kwargs):
            return LLMResponse(json.dumps({"results": [result]}))
    value = review_citation_scope("计算均值", claim, [{"id": "e1", "quote": "8 and 10 points"}], gate, Model(), messages=[])
    assert value["scoped_review"]["verified"] and value["unsupported"] == [claim] and not value["passed"]


@pytest.mark.parametrize("layout", ["paragraphs", "table_rows"])
def test_current_claim_cannot_borrow_another_rows_explicit_unit_citation(db, layout):
    evidence = [{"id": "e1", "quote": "8"}, {"id": "e2", "quote": "10"}, {"id": "e3", "quote": "points"}]
    if layout == "paragraphs":
        answer = "由8和10计算均值为9 points。[^e1][^e2]\n\n原始单位是points。[^e3]"
    else:
        answer = "| 均值为9 points，输入8和10 | [^e1][^e2] |\n| 单位是points | [^e3] |"
    def inspect(text):
        sections = re.split(r"\[论断 \d+\]", text)
        assert len(sections) == 3
        assert "引用证据 e1" in sections[1] and "引用证据 e2" in sections[1]
        assert "引用证据 e3" not in sections[1] and "引用证据 e3" in sections[2]
        assert_derived_policy(text)
    value = audit_answer(db, answer, evidence, llm=AuditModel([
        verdict(0, supported=False, reason="Synthetic rejection: unit belongs to another row, not this claim"),
        verdict(1, kind="source")], inspect), messages=[])
    assert value["status"] == "failed" and value["known_failed_count"] == 1 and value["unknown_count"] == 0
    assert value["claims"][0]["evidence_ids"] == ["e1", "e2"]
    assert value["claims"][1]["evidence_ids"] == ["e3"]


def test_current_answer_presentation_is_independent_and_not_a_claim_about_original_page_numbers(db):
    claim = "上述证据定位字段无页码，因此本回答用段落定位而不另外标注页码。"
    answer = "## 本回答的呈现说明\n\n" + claim + "[^e1]"
    def inspect(text):
        assert "说明本回答未标页码、采用段落定位等呈现方式为independent" in text
        assert "不能误读成原件没有页码" in text and "full_answer=" in text
        assert "本回答的呈现说明" in text
    value = audit_answer(db, answer, [{"id": "e1", "quote": "一段正文，不包含可核查的页眉页脚。"}],
        llm=AuditModel([verdict(kind="independent")], inspect), messages=[])
    assert value["passed"] and value["known_failed_count"] == value["unknown_count"] == 0


@pytest.mark.parametrize("kind,quote", [("source", "原件正文的页脚明确显示第1页。"),
    ("uncertain", "本次证据只有一个正文段落，未返回原件页眉页脚。")])
def test_claim_original_has_no_page_numbers_stays_negative_or_unknown(db, kind, quote):
    value = audit_answer(db, "原件全文任何位置都没有页码。[^e1]", [{"id": "e1", "quote": quote}],
        llm=AuditModel([verdict(kind=kind, supported=False)]), messages=[])
    assert not value["passed"]
    assert value["known_failed_count"] == int(kind == "source")
    assert value["unknown_count"] == int(kind == "uncertain")


def test_unresolved_whole_text_reference_is_unknown_rather_than_automatic_presentation_waiver(db):
    claim = "上述证据定位字段无页码，因此全文未标注页码。"
    model = AuditModel([verdict(kind="uncertain", supported=False, reason="Synthetic unresolved answer/original referent")])
    value = audit_answer(db, claim + "[^e1]", [{"id": "e1", "quote": "一段正文。"}], llm=model, messages=[])
    assert value["status"] == "unverified" and value["unknown_count"] == 1 and not value["passed"]
    assert "歧义无法确认时标uncertain" in model.prompts[0]


@pytest.mark.parametrize("kind,claim", [("independent", "本回答采用段落定位，不额外标注页码。"),
    ("source", "原件任何位置都没有页码。")])
def test_scope_distinguishes_answer_presentation_from_original_absence_contract(kind, claim):
    evidence = [{"id": "e1", "quote": "一段正文，不证明原件是否有页码。"}]
    class Model:
        mode = "api"
        def chat(self, history, **kwargs):
            assert "结合full_answer判断“本文/全文/下文”指当前回答还是原件" in history[0]["content"]
            assert "断言原件没有分页或任何位置都未标页码仍须真实依据" in history[0]["content"]
            return LLMResponse(json.dumps({"results": [{"index": 0, "requires_citation": kind == "source",
                "reason": "Synthetic explicit referent classification", "kind": kind, "process_scope": "none", "supports": []}]}))
    value = review_citation_scope("说明定位方式", claim, evidence, citation_gate(claim, ["e1"]), Model(), messages=[])
    assert value["scoped_review"]["verified"] and value["passed"] == (kind == "independent")
    assert value["unsupported"] == ([] if kind == "independent" else [claim])


def test_process_support_object_missing_receipt_identity_stays_schema_unknown(db):
    result = verdict(kind="process", supports=[{"quote": "Some completed execution metadata"}])
    value = audit_answer(db, "已经实际运行Excel复算。[^e1]", [{"id": "e1", "quote": "8和10。"}],
        llm=AuditModel([result]), messages=[])
    assert value["status"] == "unverified" and value["unknown_count"] == 1 and not value["passed"]
    assert "schema_support" in value["unverified"][0]["reason"]
