"""Bounded citation-principle quality contract, using synthetic verdicts only.

These tests verify dispatch, provenance, single-repair and unknown-paid fences.
They do not establish that a real model recognizes every methodological error.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from app.agent import loop, prompts, source_attribution as source
from app.agent import rewritten_answer_quality as rewritten
from app.agent.checkpoint import AgentState
from app.agent.source_review_journal import cached_source_review
from app.llm import LLMResponse


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "verification/d06-relation-quality-author-current/original-research07-error.json"
REAL_ERROR = json.loads(FIXTURE.read_text(encoding="utf-8"))
QUESTION = REAL_ERROR["question"]
BAD = REAL_ERROR["method_excerpt"]
BAD_SPAN = "1. **A 正文中的引用标记**：如 `[12]`、`(Author, 2020)`、脚注编号，并且要能定位到具体页/段。"
GOOD = (
    "不能仅凭主题相似断言A引用B。引用边需要可核验的引用记录、A到B的方向及目标作品的身份匹配；"
    "可核验参考条目、明确可定位的引用句或可信引用记录是可选择的取证路径，"
    "不必所有格式都同时有正文标记和参考表。DOI可帮助消歧，但不是每份文献都必须有。"
    "正文定位与引用句的科学主张是否被支持另行核验；没有正文定位不能据此否定已有书目引用。"
)


@pytest.fixture(autouse=True)
def external_network_forbidden(monkeypatch):
    connect = socket.socket.connect

    def local_only(sock, address):
        if not isinstance(address, tuple) or not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError("D06 synthetic tests forbid external socket connections")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


def issue(*, confirmed=True, **changes):
    return {"kind": "unresolved_request", "text": BAD_SPAN, "related_text": None,
        "user_constraint": {"receipt_id": "question", "quote": "请说明引用图谱中建立连线还需要什么证据"},
        "reason": "把正文标记与参考条目这一取证路径说成普遍必要条件，未正确回答所问证据原则。",
        "confirmed": confirmed, **changes}


class Reviewer:
    mode = "api"

    def __init__(self, issues=(), *, answer=BAD, quality_only=False):
        self.calls = []
        self.issues = list(issues)
        self.answer = answer
        self.quality_only = quality_only
        self._runtime_config = {"mode": "api", "model_id": "D06-Synthetic-Reviewer"}

    def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), deepcopy(kwargs)))
        data = {"quality_issues": deepcopy(self.issues)}
        if not self.quality_only:
            data.update(source_required=False, results=[{"index": index,
                "kind": "independent", "claims": []}
                for index, _ in enumerate(source._answer_blocks(self.answer))])
        return LLMResponse(json.dumps(data, ensure_ascii=False))


def review(model):
    return source.review_source_attribution(QUESTION, BAD,
        [{"role": "user", "content": QUESTION}], model)


def load_old_source():
    """Compile the frozen original in memory; never create its bytecode cache."""
    path = ROOT / "verification/real-model-20261116/code-current-context-repair/backend/app/agent/source_attribution.py"
    namespace = {"__name__": "app.agent._d06_old_source", "__package__": "app.agent", "__file__": str(path)}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


def test_preserved_real_failure_and_excerpt_bind_original_answer_without_rewriting_it():
    path = ROOT / REAL_ERROR["source_raw"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == REAL_ERROR["source_raw_sha256"]
    actual = json.loads(path.read_text(encoding="utf-8"))["cases"][0]["result"]["answer"]
    assert actual == REAL_ERROR["original_final_answer"] and BAD in actual and BAD_SPAN in BAD
    assert REAL_ERROR["provider_calls_by_this_copy"] == 0


def test_generation_and_both_quality_reviewers_share_the_same_layered_relation_principle():
    from app.agent.relation_evidence_policy import RELATION_EVIDENCE_POLICY
    for instructions in (prompts.SYSTEM_PROMPT, source.quality_instructions(),
                         source.review_instructions(), rewritten.quality_only_instructions()):
        assert RELATION_EVIDENCE_POLICY in instructions
    for phrase in ("引用关系存在及方向", "目标作品身份", "正文定位", "科学主张支持",
                   "可核验参考条目", "明确可定位的引用句", "可信引用记录", "DOI不是必需项"):
        assert phrase in RELATION_EVIDENCE_POLICY
    assert "不能把主题相似改称共被引证据" in prompts.SYSTEM_PROMPT


def test_actual_initial_review_dispatch_includes_method_correctness_without_source_waiver():
    model = Reviewer([issue()])
    result = review(model)
    instructions = model.calls[0][0][0]["content"]
    assert "一种充分证据路径说成普遍必要条件" in instructions
    assert "unresolved_request" in instructions
    assert "independent的claims必须为空" in instructions
    assert result["status"] == "not_applicable" and result["source_required"] is False
    assert result["quality_status"] == "failed" and result["quality_verified"] is True
    got, = result["quality_issues"]
    assert got["kind"] == "unresolved_request" and got["constraint_sha256"] == hashlib.sha256(QUESTION.encode()).hexdigest()
    assert all(BAD[span["start"]:span["end"]] == BAD_SPAN for span in got["answer_spans"])


@pytest.mark.parametrize("confirmed,status", [(True, "failed"), (None, "unverified"), (False, "passed")])
def test_located_method_issue_retains_existing_tristate_semantics(confirmed, status):
    value = review(Reviewer([issue(confirmed=confirmed)]))
    assert value["quality_status"] == status
    assert value["quality_issues"][0]["confirmed"] is confirmed
    assert value["quality_verified"] is (confirmed is not None)


@pytest.mark.parametrize("changes", [
    {"user_constraint": None},
    {"user_constraint": {"receipt_id": "question", "quote": "所有格式都应具有正文标记"}},
    {"user_constraint": {"receipt_id": "system", "quote": "请说明引用图谱"}},
    {"text": "不是当前答案里的方法错误。"},
])
def test_quality_policy_cannot_invent_user_constraint_or_answer_span(changes):
    value = review(Reviewer([issue(**changes)]))
    assert value["quality_status"] == "unverified" and not value["quality_issues"]
    assert value["quality_reason"] == "invalid_quality_protocol"


def test_policy_change_versions_both_result_contracts_but_not_user_repair_identity(monkeypatch):
    old = load_old_source()
    assert old.review_contract_id() != source.review_contract_id()
    assert old.quality_repair_context_key(QUESTION, []) == source.quality_repair_context_key(QUESTION, [])
    assert old.repair_context_key(QUESTION, []) == source.repair_context_key(QUESTION, [])
    new_contract = rewritten.review_contract_id()
    with monkeypatch.context() as patch:
        patch.setattr(source, "quality_instructions", old.quality_instructions)
        assert rewritten.review_contract_id() != new_contract


@pytest.mark.parametrize("old_state", ["pending", "unknown"])
@pytest.mark.parametrize("old_key", ["legacy", "versioned"])
def test_initial_same_context_unknown_policy_review_is_not_rebought(old_state, old_key):
    old = load_old_source()
    family = "4" * 64
    key = family if old_key == "legacy" else "source_review/" + family + "/" + old.review_contract_id()
    original = {"review_dispatch": "pending"} if old_state == "pending" else {"error_code": "dispatch_outcome_unknown"}
    journal = {key: original}
    invoked = []
    for _ in range(3):
        journal = json.loads(json.dumps(journal))
        value = cached_source_review(journal, family, contract_id=source.review_contract_id(),
            review=lambda: invoked.append(True), persist=lambda: None, before_review=lambda: None)
        assert value["error_code"] == "dispatch_outcome_unknown"
        assert set(journal) == {key}
    assert not invoked


@pytest.mark.parametrize("drift", ["policy", "model", "answer", "sources"])
@pytest.mark.parametrize("old_state", ["pending", "unknown"])
def test_postrewrite_unknown_paid_family_survives_policy_model_and_answer_drift(monkeypatch, drift, old_state):
    old = load_old_source()
    history = [{"role": "user", "content": QUESTION}]
    model = Reviewer([issue()], quality_only=True)
    with monkeypatch.context() as patch:
        patch.setattr(source, "quality_instructions", old.quality_instructions)
        old_contract = rewritten.review_contract_id()
    family = rewritten.review_family_key(QUESTION, history)
    key = "source_review/" + family + "/" + old_contract
    journal = {key: {"review_dispatch": "pending"} if old_state == "pending" else
               {"error_code": "dispatch_outcome_unknown"}}
    if drift == "model":
        model._runtime_config["model_id"] = "D06-Changed-Synthetic-Model"
    answer = GOOD if drift == "answer" else BAD
    evidence = {"evidence": [{"revision_id": "changed-version"}]} if drift == "sources" else {}
    for _ in range(3):
        journal = json.loads(json.dumps(journal))
        value = rewritten.review_rewritten_quality(QUESTION, answer, history, model, journal=journal,
            persist=lambda: None, before_review=lambda: None, evidence_context=evidence)
        assert value["error_code"] == "dispatch_outcome_unknown" and value["blocks_completion"]
        assert set(journal) == {key}
    assert not model.calls


def test_known_old_result_rechecks_once_without_mutating_old_proof():
    old = load_old_source()
    family = "5" * 64
    old_key = "source_review/" + family + "/" + old.review_contract_id()
    old_proof = {"quality_status": "passed", "quality_issues": [], "legacy_proof": "unchanged"}
    journal = {old_key: deepcopy(old_proof)}
    called = []
    for _ in range(2):
        value = cached_source_review(journal, family, contract_id=source.review_contract_id(),
            review=lambda: called.append(1) or {"quality_status": "failed", "quality_issues": [issue()]},
            persist=lambda: None, before_review=lambda: None)
        assert value["quality_status"] == "failed"
    assert called == [1] and journal[old_key] == old_proof and len(journal) == 2


@pytest.mark.parametrize("fixed", [True, False])
def test_real_validator_and_executor_use_one_quality_repair_without_reading_or_writing(db, monkeypatch, fixed):
    saved, reviews, generations, tools = [], [], [], []

    class Model:
        mode = "api"

        def chat(self, messages, **kwargs):
            reviews.append(deepcopy(messages))
            request = json.loads(messages[1]["content"])
            text = "\n\n".join(b["text"] for b in request["blocks"])
            issues = [issue()] if BAD_SPAN in text else []
            return LLMResponse(json.dumps({"source_required": False,
                "results": [{"index": b["index"], "kind": "independent", "claims": []}
                    for b in request["blocks"]], "quality_issues": issues}, ensure_ascii=False))

        def stream_chat(self, messages, tools=None):
            generations.append(deepcopy(messages))
            assert any(r.get("repair_claimed") for r in saved[-1]["answer_quality_repairs"].values())
            yield "final", LLMResponse(GOOD if fixed else BAD)

    def forbidden_tool(*args, **kwargs):
        tools.append((args, kwargs))
        raise AssertionError("Explaining a relation principle must not read or write a graph")

    monkeypatch.setattr(loop, "get_llm", Model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop, "execute_tool", forbidden_tool)
    state = AgentState(question=QUESTION, phase="finalize", answer=BAD,
        messages=[{"role": "user", "content": QUESTION}],
        runtime_config={"mode": "api", "model_id": "D06-Synthetic-Executor"}, plan_source="requirements")
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(),
        save_checkpoint=lambda s: saved.append(deepcopy(s))))
    result = next(value for kind, value in events if kind == "answer")
    assert len(generations) == 1 and len(reviews) == 2 and not tools
    assert result["completed"] is fixed and result["answer_quality"]["status"] == ("passed" if fixed else "failed")
    assert sum(record.get("repair_claimed", False) for record in saved[-1]["answer_quality_repairs"].values()) == 1
    assert not saved[-1]["source_attribution_repair_contexts"]
    feedback = generations[0][-1]["content"]
    assert "普遍必要条件" in feedback and "不要直接新增关系" not in issue()["reason"]
    assert "补齐真正缺少的材料依据" not in feedback
    if not fixed:
        assert result["outcome"] == "partial" and result["stop_reason"] == "answer_quality_unresolved"
