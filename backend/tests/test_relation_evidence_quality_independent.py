"""Independent D06 mechanism checks; all reviewer decisions are synthetic.

The preserved pre-change .txt sources are compiled only in memory. No frozen
evaluation copy or author test helper is imported. This cannot establish real
model understanding, graph-science semantics, or commercial acceptance.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
from types import ModuleType

import pytest

from app import source_store
from app.agent import loop, prompts as live_prompts, source_attribution as live_source
from app.agent import rewritten_answer_quality as live_rewritten
from app.agent.checkpoint import AgentState
from app.agent.paid_review_family import FIELD, family_identity
from app.agent.relation_evidence_policy import RELATION_EVIDENCE_POLICY
from app.agent.source_review_journal import cached_source_review
from app.api.graph import _compute_full_graph
from app.citation_contexts import locate_context
from app.llm import LLMResponse
from app.models import Conversation, Paper, Reference
from app.research_models import RelationEvidence
from app.source_models import SourceLibraryBinding, SourceLibraryImport


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "verification/d06-relation-quality-independent-current"
QUESTION = "只解释从A指向B建立引用连线需要哪些证据，不要新增关系，也不要检索外部索引。"
BAD_SPAN = "必须同时找到A正文引用标记与参考文献表条目"
BAD = "建立引用连线" + BAD_SPAN + "，且每份文献都必须提供DOI；缺少正文位置就不能确认任何引用关系。"
GOOD = ("引用取证要区分存在和方向、目标作品身份、正文定位、科学主张支持。"
    "经核验的参考条目并完成目标精确匹配是一种取证路径，明确引用句或可信引用记录也是可选路径。"
    "DOI可帮助消歧但不是必需项；正文未定位应如实说明，不据此否定已有书目引用。"
    "这些只解释原则，未声称已核查本轮文件、导入外部索引或写入任何连线。")


def _old_module(name):
    path = EVIDENCE / "before-source" / (name + ".py.txt")
    module = ModuleType("app.agent._d06_independent_old_" + name)
    module.__package__ = "app.agent"
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


OLD_SOURCE = _old_module("source_attribution")
OLD_PROMPTS = _old_module("prompts")
OLD_REWRITTEN = _old_module("rewritten_answer_quality")
OLD_REWRITTEN.source = OLD_SOURCE
BEFORE = os.environ.get("D06_INDEPENDENT_PHASE") == "before"
source = OLD_SOURCE if BEFORE else live_source
prompts = OLD_PROMPTS if BEFORE else live_prompts
rewritten = OLD_REWRITTEN if BEFORE else live_rewritten


@pytest.fixture(autouse=True)
def no_external_socket(monkeypatch):
    for name in ("connect", "connect_ex"):
        original = getattr(socket.socket, name)

        def connect(sock, address, _original=original):
            if isinstance(address, tuple) and address:
                try:
                    if ipaddress.ip_address(address[0]).is_loopback:
                        return _original(sock, address)
                except ValueError:
                    pass
            raise AssertionError("D06 independent synthetic audit forbids external sockets")

        monkeypatch.setattr(socket.socket, name, connect)


def _issue(confirmed=True, **changes):
    return {"kind": "unresolved_request", "text": BAD_SPAN, "related_text": None,
        "user_constraint": {"receipt_id": "question", "quote": "建立引用连线需要哪些证据"},
        "reason": "把一种取证路径列成普遍必要条件，未正确解释用户询问的引用取证依据。",
        "confirmed": confirmed, **changes}


class Oracle:
    mode = "api"

    def __init__(self, issues=(), *, quality_only=False, model_id="D06-independent-model-A", claim=None):
        self.issues, self.quality_only, self.claim = list(issues), quality_only, claim
        self.calls = []
        self._runtime_config = {"mode": "api", "model_id": model_id}

    def chat(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        request = json.loads(messages[1]["content"])
        data = {"quality_issues": deepcopy(self.issues)}
        if not self.quality_only:
            data.update(source_required=self.claim is not None, results=[{
                "index": block["index"], "kind": "source" if self.claim is not None else "independent",
                "claims": [deepcopy(self.claim)] if self.claim is not None else []}
                for block in request["blocks"]])
        return LLMResponse(json.dumps(data, ensure_ascii=False))


def _initial(model, *, answer=BAD, question=QUESTION, messages=None):
    return source.review_source_attribution(question, answer,
        messages if messages is not None else [{"role": "user", "content": question}], model)


def _post(model, journal=None, *, answer=BAD, policy_module=None, **changes):
    target = rewritten if policy_module is None else policy_module
    return target.review_rewritten_quality(QUESTION, answer, [{"role": "user", "content": QUESTION}],
        model, journal={} if journal is None else journal, persist=lambda: None,
        before_review=lambda: None, **changes)


def test_canary_generation_initial_and_postrewrite_share_layered_policy():
    for text in (prompts.SYSTEM_PROMPT, source.quality_instructions(), source.review_instructions(),
                 rewritten.quality_only_instructions()):
        assert RELATION_EVIDENCE_POLICY in text


def test_canary_actual_initial_dispatch_explains_method_error_contract():
    model = Oracle([_issue()])
    result = _initial(model)
    assert "一种充分证据路径说成普遍必要条件" in model.calls[0][0]["content"]
    assert result["status"] == "not_applicable" and result["source_required"] is False
    assert result["quality_status"] == "failed" and result["quality_verified"] is True


def test_canary_policy_versions_result_identities_without_new_repair_allowance():
    assert source.review_contract_id() != OLD_SOURCE.review_contract_id()
    assert rewritten.review_contract_id() != OLD_REWRITTEN.review_contract_id()
    history = [{"role": "user", "content": QUESTION}]
    assert source.quality_repair_context_key(QUESTION, history) == OLD_SOURCE.quality_repair_context_key(QUESTION, history)
    assert source.repair_context_key(QUESTION, history) == OLD_SOURCE.repair_context_key(QUESTION, history)
    assert source.REVIEW_PROTOCOL == OLD_SOURCE.REVIEW_PROTOCOL
    assert source.SOURCE_REVIEW_CONTEXT_PROTOCOL == OLD_SOURCE.SOURCE_REVIEW_CONTEXT_PROTOCOL


def test_canary_old_known_pass_is_not_relabelled_as_new_policy_proof():
    family = "a" * 64
    old_key = "source_review/" + family + "/" + OLD_SOURCE.review_contract_id()
    old = {"quality_status": "passed", "quality_issues": [], "old_proof": "must remain unchanged"}
    journal, calls = {old_key: deepcopy(old)}, []
    for _ in range(2):
        value = cached_source_review(journal, family, contract_id=source.review_contract_id(),
            review=lambda: calls.append(1) or {"quality_status": "failed", "quality_issues": [_issue()]},
            persist=lambda: None, before_review=lambda: None)
        assert value["quality_status"] == "failed"
    assert calls == [1] and journal[old_key] == old


@pytest.mark.parametrize("match,body", [("arxiv", False), ("doi", False), ("title", False), ("arxiv", True)])
def test_real_graph_keeps_identity_location_and_semantic_layers_separate(db, match, body):
    target_title = "Independent Laboratory Target Reference Study"
    identifier = " arXiv:2411.01234" if match == "arxiv" else " doi:10.5555/d06-independent" if match == "doi" else ""
    reference_text = "[1] Doe, J. " + target_title + ". 2024." + identifier
    raw = (("A comparison is cited here [1].\n" if body else "") + "References\n" + reference_text).encode()
    citing = Paper(title="Independent citing document", status="parsed", content_hash=hashlib.sha256(raw).hexdigest())
    target = Paper(title=target_title + (identifier if match == "doi" else ""), status="parsed",
        arxiv_id="2411.01234" if match == "arxiv" else None, content_hash="b" * 64)
    db.add_all([citing, target, Conversation(id="d06-independent-reader")])
    db.flush()
    scope = source_store.SourceScope("library", citing.id)
    record = source_store.capture_source(db, scope=scope,
        authorization=source_store.SourceAuthorization(scope, "d06-independent-fabricated-library", "library"),
        kind="library", title="D06 fabricated reference.txt", raw_bytes=raw, media_type="text/plain")
    db.add(SourceLibraryBinding(paper_id=citing.id, revision_id=record["revision_id"]))
    db.add(SourceLibraryImport(paper_id=citing.id, revision_id=record["revision_id"], raw_sha256=record["raw_sha256"],
        request_id="d06-independent-library", chunk_locators={}))
    db.add(Reference(paper_id=citing.id, ref_index=1, raw_text=reference_text, title=target_title))
    db.commit()
    graph = _compute_full_graph(db)
    edge, = graph["edges"]
    assert edge["source"] == citing.id and edge["target"] == target.id
    assert edge["status"] == "unverified"
    assert edge["relation_type"] == ("uncertain" if match == "title" else "citation")
    relation = db.get(RelationEvidence, edge["relation_id"])
    assert relation.effective["reference_entry"] == reference_text and not relation.manual
    if body:
        context, = relation.extracted["citation_contexts"]
        assert context["semantic_support"] == "unverified"
        located = locate_context(db, relation, "d06-independent-reader", 0)
        assert located["citation_sentence_verified"] is True
        assert located["semantic_support_verified"] is False
        assert relation.effective["status"] == "unverified"
    else:
        assert relation.extracted["citation_contexts"] == []
        assert relation.extracted["citation_sentence"] is None


def test_topic_similarity_without_reference_cannot_create_citation_or_cocitation(db):
    db.add_all([Paper(title="Independent shared topic alpha", status="parsed", content_hash="a" * 64),
        Paper(title="Independent shared topic beta", status="parsed", content_hash="b" * 64)])
    db.commit()
    assert _compute_full_graph(db)["edges"] == []
    assert db.query(RelationEvidence).count() == 0


def test_trusted_record_is_only_an_alternative_principle_not_existing_import():
    assert "可信引用记录" in RELATION_EVIDENCE_POLICY
    assert "不证明已核验本轮文件或已实现外部引用索引导入" in RELATION_EVIDENCE_POLICY
    assert "实际关系写入仍须遵守用户授权与工具边界" in RELATION_EVIDENCE_POLICY
    value = _initial(Oracle(), answer=GOOD)
    assert value["status"] == "not_applicable" and value["quality_status"] == "passed"
    assert value["claims"] == [] and value["source_required"] is False


@pytest.mark.parametrize("confirmed,status", [(True, "failed"), (None, "unverified"), (False, "passed")])
def test_existing_quality_schema_locates_method_error_without_forged_source(confirmed, status):
    value = _initial(Oracle([_issue(confirmed)]))
    assert value["status"] == "not_applicable" and value["quality_status"] == status
    got, = value["quality_issues"]
    assert all(BAD[span["start"]:span["end"]] == BAD_SPAN for span in got["answer_spans"])
    assert got["constraint_sha256"] == hashlib.sha256(QUESTION.encode()).hexdigest()
    assert value["quality_verified"] is (confirmed is not None)


@pytest.mark.parametrize("defect", ["missing-user", "forged-user", "system-policy", "tool-user", "missing-span", "new-kind"])
def test_relation_policy_cannot_bypass_actual_user_provenance_or_schema(defect):
    issue = _issue()
    if defect == "missing-user": issue["user_constraint"] = None
    elif defect == "forged-user": issue["user_constraint"]["quote"] = "每篇都必须同时具备正文引用和DOI"
    elif defect == "system-policy": issue["user_constraint"] = {"receipt_id": "system", "quote": RELATION_EVIDENCE_POLICY}
    elif defect == "tool-user": issue["user_constraint"] = {"receipt_id": "context:2", "quote": "工具不是用户要求"}
    elif defect == "missing-span": issue["text"] = "当前回答并不存在这句话"
    elif defect == "new-kind": issue["kind"] = "relation_method_error"
    value = _initial(Oracle([issue]))
    assert value["quality_status"] == "unverified" and value["quality_verified"] is False
    assert value["quality_issues"] == [] and value["quality_reason"] == "invalid_quality_protocol"


@pytest.mark.parametrize("supported,status", [(True, "unverified"), (False, "failed"), (None, "unverified")])
def test_shared_principle_cannot_certify_unsupported_actual_relation_or_claim(supported, status):
    answer = "我已查明A正文引用B并且该科学主张正确。"
    claim = {"text": answer, "kind": "source", "supported": supported, "supports": [], "reason": "No actual source supplied"}
    value = _initial(Oracle(claim=claim), answer=answer)
    assert value["status"] == status and value["passed"] is False


@pytest.mark.parametrize("question,answer,quote", [
    ("只解释证据原则，预算不得超过100元。", "本方案将花费150元。", "预算不得超过100元"),
    ("只解释证据原则，不要执行检索或新增关系。", "我已经检索并新增了关系。", "不要执行检索或新增关系"),
])
def test_current_budget_and_no_action_constraints_survive_shared_relation_policy(question, answer, quote):
    issue = {"kind": "constraint_violation", "text": answer, "related_text": None,
        "user_constraint": {"receipt_id": "question", "quote": quote},
        "reason": "Existing explicit constraint still applies", "confirmed": True}
    value = _initial(Oracle([issue]), question=question, answer=answer)
    assert value["quality_status"] == "failed" and value["quality_verified"] is True
    assert live_source.QUALITY_KINDS == OLD_SOURCE.QUALITY_KINDS
    for text in ("所有备选分支也须满足用户限制", "字数和句数限制覆盖完整可见答案", "确定问题用true，不确定用null"):
        assert text in live_source.quality_instructions()


@pytest.mark.parametrize("state", ["pending", "unknown"])
@pytest.mark.parametrize("key_form", ["legacy", "versioned"])
def test_initial_same_context_pending_or_unknown_survives_policy_upgrade(state, key_form):
    family = "c" * 64
    key = family if key_form == "legacy" else "source_review/" + family + "/" + OLD_SOURCE.review_contract_id()
    value = {"review_dispatch": "pending"} if state == "pending" else {"error_code": "dispatch_outcome_unknown"}
    journal, calls = {key: value}, []
    for _ in range(2):
        journal = json.loads(json.dumps(journal))
        result = cached_source_review(journal, family, contract_id=live_source.review_contract_id(),
            review=lambda: calls.append(1), persist=lambda: None, before_review=lambda: None)
        assert result["error_code"] == "dispatch_outcome_unknown" and set(journal) == {key}
    assert calls == []


@pytest.mark.parametrize("state", ["pending", "unknown"])
@pytest.mark.parametrize("drift", ["policy", "answer", "model", "evidence"])
def test_postrewrite_pending_or_unknown_never_rebuys_across_policy_answer_model(state, drift, monkeypatch):
    model, journal = Oracle(quality_only=True), {}
    history = [{"role": "user", "content": QUESTION}]
    family = OLD_REWRITTEN.review_family_key(QUESTION, history)
    # Exercise the production intent persistence path before simulating loss.
    class LostPower(BaseException):
        pass
    original = model.chat
    model.chat = lambda *a, **kw: (_ for _ in ()).throw(LostPower())
    OLD_REWRITTEN.review_rewritten_quality
    with pytest.raises(LostPower):
        OLD_REWRITTEN.review_rewritten_quality(QUESTION, BAD, history, model,
            journal=journal, persist=lambda: None, before_review=lambda: None)
    key, = journal
    assert key.startswith("source_review/" + family + "/")
    if state == "unknown":
        cached_source_review(journal, family, contract_id=key.rsplit("/", 1)[1], review=lambda: None,
            persist=lambda: None, before_review=lambda: None)
    model.chat = original
    if drift == "model": model._runtime_config["model_id"] = "D06-independent-model-B"
    if drift == "policy":
        current = live_rewritten.quality_only_instructions()
        monkeypatch.setattr(live_rewritten, "quality_only_instructions", lambda: current + " Additional synthetic policy revision.")
    for _ in range(2):
        journal = json.loads(json.dumps(journal))
        result = _post(model, journal, answer=GOOD if drift == "answer" else BAD,
            policy_module=live_rewritten,
            evidence_context={"new": "fixed-source-version"} if drift == "evidence" else {})
        assert result["error_code"] == "dispatch_outcome_unknown" and result["blocks_completion"]
        assert set(journal) == {key}
    assert model.calls == []


@pytest.mark.parametrize("fixed", [True, False])
def test_real_executor_uses_one_quality_rewrite_without_reading_or_writing_graph(db, monkeypatch, fixed):
    reviews, rewrites, snapshots, tools = [], [], [], []

    class Model(Oracle):
        def chat(self, messages, **kwargs):
            request = json.loads(messages[1]["content"])
            current = "\n\n".join(block["text"] for block in request["blocks"])
            self.issues = [_issue()] if BAD_SPAN in current else []
            reviews.append(deepcopy(messages))
            return super().chat(messages, **kwargs)

        def stream_chat(self, messages, tools=None):
            assert any(record.get("repair_claimed") for record in snapshots[-1]["answer_quality_repairs"].values())
            rewrites.append(deepcopy(messages))
            yield "final", LLMResponse(GOOD if fixed else BAD)

    model = Model()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})

    def forbidden(*args, **kwargs):
        tools.append(True)
        raise AssertionError("Relation principle explanation must not execute a tool")

    monkeypatch.setattr(loop, "execute_tool", forbidden)
    state = AgentState(question=QUESTION, phase="finalize", answer=BAD,
        messages=[{"role": "user", "content": QUESTION}], plan_source="requirements",
        runtime_config={"mode": "api", "model_id": "D06-independent-model-A"})
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(),
        save_checkpoint=lambda value: snapshots.append(deepcopy(value))))
    result = next(value for kind, value in events if kind == "answer")
    assert len(rewrites) == 1 and len(reviews) == 2 and tools == []
    assert result["completed"] is fixed
    assert result["answer_quality"]["status"] == ("passed" if fixed else "failed")
    assert sum(bool(record.get("repair_claimed")) for record in snapshots[-1]["answer_quality_repairs"].values()) == 1
    assert not snapshots[-1]["source_attribution_repair_contexts"]
    assert "补齐真正缺少的材料依据" not in rewrites[0][-1]["content"]
    if not fixed:
        assert result["outcome"] == "partial" and result["stop_reason"] == "answer_quality_unresolved"


@pytest.mark.parametrize("state", ["pending", "unknown"])
@pytest.mark.parametrize("drift", ["model", "answer"])
def test_initial_source_unknown_context_drift_must_not_repurchase(db, monkeypatch, state, drift):
    """Real checkpoint counterexample; keep red until a stable-family fix exists."""
    class LostPower(BaseException):
        pass

    generations = []

    class Model(Oracle):
        lose = True

        def chat(self, messages, **kwargs):
            if self.lose:
                self.calls.append(deepcopy(messages))
                raise LostPower("Synthetic process loss after durable source-review intent")
            return super().chat(messages, **kwargs)

        def stream_chat(self, messages, tools=None):
            generations.append(deepcopy(messages))
            yield "final", LLMResponse(GOOD)

    model, snapshots = Model(), []
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **kw: {"ran": 0, "blocked": False, "feedback": ""})
    checkpoint = AgentState(question=QUESTION, phase="finalize", answer=GOOD,
        messages=[{"role": "user", "content": QUESTION}], plan_source="requirements",
        runtime_config=deepcopy(model._runtime_config)).snapshot()
    with pytest.raises(LostPower):
        list(loop.run_agent(db, QUESTION, checkpoint=checkpoint,
            save_checkpoint=lambda value: snapshots.append(deepcopy(value))))
    before = deepcopy(snapshots[-1])
    journal = before["source_attribution_reviews"]
    old_key, = journal
    assert journal[old_key]["review_dispatch"] == "pending" and len(model.calls) == 1
    if state == "unknown":
        _, family, contract = old_key.split("/")
        cached_source_review(journal, family, contract_id=contract, review=lambda: None,
            persist=lambda: None, before_review=lambda: None)
        assert journal[old_key]["error_code"] == "dispatch_outcome_unknown"
    recovered = deepcopy(before)
    if drift == "model":
        model._runtime_config["model_id"] = "D06-independent-model-B"
        recovered["runtime_config"]["model_id"] = "D06-independent-model-B"
    else:
        recovered["answer"] = GOOD + " 上述只是取证原则说明。"
    model.lose = False
    events = list(loop.run_agent(db, QUESTION, checkpoint=recovered,
        save_checkpoint=lambda value: snapshots.append(deepcopy(value))))
    observation = {"state": state, "drift": drift, "question_unchanged": True,
        "first_dispatch_count": 1, "dispatch_count_after_resume": len(model.calls),
        "main_generation_count": len(generations),
        "old_key": old_key, "old_entry": before["source_attribution_reviews"][old_key],
        "resumed_journal": snapshots[-1]["source_attribution_reviews"],
        "result": next((value for kind, value in events if kind == "answer"), None),
        "evidence_type": "Actual executor/checkpoint with synthetic reviewer and synthetic process loss; no real provider calls"}
    directory = Path(os.environ["D06_INDEPENDENT_OBSERVATIONS"])
    (directory / f"initial-source-{state}-{drift}.json").write_text(
        json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(model.calls) == 1, "Unresolved paid source-review intent was repurchased after context-key drift"
    assert observation["result"]["source_attribution"]["error_code"] == "dispatch_outcome_unknown"
    assert observation["result"]["answer_quality"]["verified"] is False
    assert observation["result"]["completed"] is False, "An unknown review cannot be reported as a completed user task"
    assert observation["result"]["answer_quality"]["status"] == "unverified"
    assert observation["result"]["answer_quality"]["known_failed_count"] == 0
    assert observation["result"]["answer_quality"]["issues"] == []
    assert generations == [], "An unknown review without a confirmed issue must not buy a blind rewrite"


@pytest.mark.parametrize("defect", ["list", "wrong-stage", "wrong-protocol", "extra-field", "missing-field", "boolean-revision", "empty-revision"])
def test_initial_dispatch_family_malformed_input_never_dispatches_or_mutates(defect):
    family = family_identity("source_attribution", question=QUESTION, messages=[], input_ids=["user-1"])
    if defect == "list": family = []
    elif defect == "wrong-stage": family["stage"] = "citation_scope"
    elif defect == "wrong-protocol": family["protocol"] = "invented-protocol"
    elif defect == "extra-field": family["extra"] = "not-part-of-identity"
    elif defect == "missing-field": family.pop("user_revision")
    elif defect == "boolean-revision": family["user_revision"] = True
    elif defect == "empty-revision": family["user_revision"] = ""
    journal, calls, saves = {}, [], []
    result = cached_source_review(journal, "a" * 64, contract_id=live_source.review_contract_id(),
        review=lambda: calls.append(1), persist=lambda: saves.append(deepcopy(journal)),
        before_review=lambda: None, dispatch_family=family)
    assert result["error_code"] == "journal_invalid"
    assert journal == {} and calls == saves == []


@pytest.mark.parametrize("defect", ["list", "wrong-stage", "bad-revision"])
def test_malformed_existing_initial_family_fails_closed_without_replacing_proof(defect):
    family = family_identity("source_attribution", question=QUESTION, messages=[])
    prior = deepcopy(family)
    if defect == "list": prior = []
    elif defect == "wrong-stage": prior["stage"] = "citation_scope"
    elif defect == "bad-revision": prior["user_revision"] = "not-a-sha256"
    old_key = "source_review/" + "c" * 64 + "/" + "d" * 64
    journal = {old_key: {"review_dispatch": "pending", FIELD: prior}}
    original, calls = deepcopy(journal), []
    result = cached_source_review(journal, "e" * 64, contract_id=live_source.review_contract_id(),
        review=lambda: calls.append(1), persist=lambda: None, before_review=lambda: None,
        dispatch_family=family)
    assert result["error_code"] == "journal_invalid" and calls == [] and journal == original


def test_initial_family_is_durable_before_dispatch_and_hidden_from_public_result():
    family = family_identity("source_attribution", question=QUESTION, messages=[], input_ids=["user-1"])
    legacy, contract = "a" * 64, live_source.review_contract_id()
    key = "source_review/" + legacy + "/" + contract
    journal, persisted = {}, []

    def review():
        assert persisted[-1][key]["review_dispatch"] == "pending"
        assert persisted[-1][key][FIELD] == family
        return {"quality_status": "passed", "quality_verified": True, "quality_issues": []}

    value = cached_source_review(journal, legacy, contract_id=contract, review=review,
        persist=lambda: persisted.append(deepcopy(journal)), before_review=lambda: None,
        dispatch_family=family)
    assert set(journal) == {key} and journal[key][FIELD] == family
    assert FIELD not in value and value["quality_status"] == "passed"


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_scoped_initial_intent_allows_genuine_new_user_revision(state):
    class LostPower(BaseException):
        pass

    prior_family = family_identity("source_attribution", question=QUESTION,
        messages=[{"role": "user", "content": QUESTION}], input_ids=["user-input-1"])
    current_family = family_identity("source_attribution", question=QUESTION,
        messages=[{"role": "user", "content": QUESTION}, {"role": "user", "content": "补充：请分析下一份记录。",
            "_user_steering": "actual-new-user-input-2"}], input_ids=["user-input-1", "actual-new-user-input-2"])
    assert prior_family != current_family
    contract, old_legacy, current_legacy = live_source.review_contract_id(), "a" * 64, "b" * 64
    old_key = "source_review/" + old_legacy + "/" + contract
    journal = {}
    with pytest.raises(LostPower):
        cached_source_review(journal, old_legacy, contract_id=contract,
            review=lambda: (_ for _ in ()).throw(LostPower()), persist=lambda: None,
            before_review=lambda: None, dispatch_family=prior_family)
    assert journal[old_key][FIELD] == prior_family
    if state == "unknown":
        cached_source_review(journal, old_legacy, contract_id=contract, review=lambda: None,
            persist=lambda: None, before_review=lambda: None, dispatch_family=prior_family)
    before, calls = deepcopy(journal), []
    value = cached_source_review(journal, current_legacy, contract_id=contract,
        review=lambda: calls.append(1) or {"quality_status": "passed", "quality_verified": True, "quality_issues": []},
        persist=lambda: None, before_review=lambda: None, dispatch_family=current_family)
    Path(os.environ["D06_INDEPENDENT_OBSERVATIONS"]).joinpath("genuine-user-revision-" + state + ".json").write_text(
        json.dumps({"old_state": state, "prior_family": prior_family, "current_family": current_family,
            "before": before, "after": journal, "new_review_calls": len(calls), "public_result": value},
            ensure_ascii=False, indent=2), encoding="utf-8")
    assert calls == [1], "A scoped old unknown must not lose its user revision and block a genuine new input"
    assert journal[old_key] == before[old_key]
    assert value["quality_status"] == "passed" and FIELD not in value


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_unscoped_legacy_initial_unknown_conservatively_blocks_new_user(state):
    old_key = "source_review/" + "a" * 64 + "/" + "b" * 64
    journal = {old_key: {"review_dispatch": "pending"} if state == "pending" else
        {"error_code": "dispatch_outcome_unknown"}}
    calls = []
    family = family_identity("source_attribution", question="真实的新用户请求", messages=[], input_ids=["new-user-2"])
    value = cached_source_review(journal, "c" * 64, contract_id=live_source.review_contract_id(),
        review=lambda: calls.append(1), persist=lambda: None, before_review=lambda: None,
        dispatch_family=family)
    assert value["error_code"] == "dispatch_outcome_unknown" and calls == []
    assert set(journal) == {old_key} and FIELD not in journal[old_key]
