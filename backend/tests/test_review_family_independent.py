"""Independent paid-review recovery probes using real adapters and local HTTP.

The APIClient meter deliberately uses its production actual-usage branch;
every HTTP byte goes through MockTransport with fabricated credentials/data.
No provider or commercial charge exists outside the isolated test ledger.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import httpx
import pytest

from app import call_ledger, config, resource_budget
from app.agent import audit, citation_scope
from app.llm import APIClient
from app.rag.citation import citation_gate
from app.runtime_context import bind_runtime_config


USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
EVIDENCE = [{"id": "e1", "paper_title": "Fabricated independent review record", "page": 1,
    "quote": "The fabricated record reports eight attention heads in the control experiment."}]
SOURCE = "合成材料的对照实验使用八个注意力头。[^e1]"
ANSWER = SOURCE + "\n\n梯度下降可以通过迭代更新参数降低目标函数。"
SUPPLEMENT = "\n\n补充说明：这段基础概念解释采用另一种表达形式。"
BASE = "https://independent-review.example.test/v1"
MODEL = "Independent-Exact-Review-Model"
HTTPClient = httpx.Client
OUT = Path(__file__).resolve().parents[2] / "verification/review-family-independent"


@pytest.fixture
def controlled_provider(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_base_url", BASE)
    monkeypatch.setattr(config.settings, "llm_api_key", "fabricated-independent-review-only-key")
    monkeypatch.setattr(config.settings, "llm_model", MODEL)
    requests = []
    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        text = body["messages"][-1]["content"]
        try:
            scope = json.loads(text)
        except ValueError:
            scope = None
        if isinstance(scope, dict) and "uncited_candidates" in scope:
            results = [{"index": row["index"], "requires_citation": False,
                "reason": "Fabricated independent presentation explanation.", "kind": "independent",
                "process_scope": "none", "supports": []} for row in scope["uncited_candidates"]]
        else:
            indices = [int(i) for i in re.findall(r"^\[论断 (\d+)\]", text, re.M)]
            assert indices
            results = [{"index": i, "supported": True, "reason": "Fabricated supported source claim.",
                "support_kind": "source", "process_scope": "none", "supports": []} for i in indices]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"results": results}, ensure_ascii=False)}}], "usage": USAGE})
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs:
        HTTPClient(*args, transport=httpx.MockTransport(handle), **kwargs))
    return requests


def invoke(db, stage, model, journal, *, answer=ANSWER, persist=lambda: None):
    if stage == "scope":
        return audit.review_citation_scope("概括合成材料并解释一个基础概念", answer, EVIDENCE,
            citation_gate(answer, ["e1"]), model, messages=[], review_journal=journal,
            persist=persist, before_review=lambda: None)
    return audit.audit_answer(db, answer, EVIDENCE, llm=model, messages=[],
        audit_journal=journal, persist=persist, before_batch=lambda: None)


def alter_instructions(monkeypatch, stage):
    if stage == "scope":
        monkeypatch.setattr(citation_scope, "PROCESS_INSTRUCTIONS", citation_scope.PROCESS_INSTRUCTIONS +
            " 本次审查保留原始索引，并使用简短的审阅说明。")
    else:
        original = audit._build_audit_text
        monkeypatch.setattr(audit, "_build_audit_text", lambda *args, **kwargs:
            original(*args, **kwargs) + "\n本次审查保留原始索引，并使用简短的审阅说明。")


@pytest.mark.parametrize("stage", ["scope", "audit"])
@pytest.mark.parametrize("legacy_state", ["pending", "unknown"])
@pytest.mark.parametrize("mutation", ["answer", "instructions"])
def test_possibly_paid_legacy_review_cannot_be_rebought_by_changing_the_exact_content_key(
        db, controlled_provider, monkeypatch, stage, legacy_state, mutation):
    requests = controlled_provider
    model, journal, durable = APIClient(), {}, []
    def interrupted_result_save():
        # Do not assume metadata and pending writes share one checkpoint.
        # Fail only after a locally completed response, preserving the latest
        # successfully persisted predispatch snapshot for restoration.
        if requests:
            raise OSError("fabricated result checkpoint save failure after successful HTTP")
        durable.append(deepcopy(journal))
    with pytest.raises(OSError):
        invoke(db, stage, model, journal, persist=interrupted_result_save)
    assert len(requests) == 1 and durable
    first = call_ledger.list_calls()["items"]
    assert len(first) == 1 and first[0]["state"] == "completed"
    assert first[0]["actual_usage"] == USAGE and first[0]["usage_source"] == "actual"
    pending = json.loads(json.dumps(durable[-1]))
    # Restore the actual legacy schema, not a newly generated family record.
    # The first-failure frozen source had no such metadata; current producers
    # may add it alongside pending without changing the exact legacy keys.
    for record in pending.values():
        if isinstance(record, dict):
            record.pop("_paid_review_family", None)
    same = deepcopy(pending)
    same_result = invoke(db, stage, model, same)
    assert len(requests) == 1 and same_result["error_code"] == "dispatch_outcome_unknown"
    restored = same if legacy_state == "unknown" else pending
    old_keys = list(restored)
    answer = ANSWER + SUPPLEMENT if mutation == "answer" else ANSWER
    if mutation == "instructions":
        alter_instructions(monkeypatch, stage)
    result = invoke(db, stage, model, restored, answer=answer)
    ledger = call_ledger.list_calls()["items"]
    OUT.mkdir(exist_ok=True)
    receipt_path = OUT / f"{stage}-{legacy_state}-{mutation}-initial.json"
    if receipt_path.exists():
        # Preserve independently frozen first-failure receipts across repair
        # runs. Rechecks belong to the production source which generated them.
        folder = OUT / "rechecks"
        folder.mkdir(exist_ok=True)
        source = Path(audit.__file__).read_bytes()
        receipt_path = folder / (f"{stage}-{legacy_state}-{mutation}-" +
            hashlib.sha256(source).hexdigest()[:12] + ".json")
    receipt_path.write_text(json.dumps({
        "stage": stage, "legacy_state": legacy_state, "mutation": mutation,
        "external_http_calls": 0, "local_mock_requests": len(requests),
        "old_keys": old_keys, "current_keys": list(restored),
        "request_body_sha256": [hashlib.sha256(r.content).hexdigest() for r in requests],
        "public_result": result, "ledger": ledger,
        "note": "All transport is local; actual usage is fabricated units exercising the production meter branch."
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert len(requests) == 1, "changing answer/instructions must not automatically rebuy an unresolved legacy paid review"
    assert len(ledger) == 1 and not result["passed"] and result.get("verified") is not True


@pytest.mark.parametrize("stage", ["scope", "audit"])
def test_proven_unsubmitted_budget_wait_can_resume_without_being_treated_as_a_paid_unknown(
        db, controlled_provider, stage):
    requests = controlled_provider
    model, journal, durable = APIClient(), {}, []
    identity = resource_budget.create_budget(owner_run_id="family-independent-" + stage,
        amount_limit=".1", request_estimate=".2", price_version="fabricated-independent-estimate")
    with bind_runtime_config({"resource_budget_id": identity, "mode": "api", "base_url": BASE, "model_id": MODEL}):
        with pytest.raises(resource_budget.ResourceBudgetExceeded):
            invoke(db, stage, model, journal, persist=lambda: durable.append(deepcopy(journal)))
        assert not requests and not call_ledger.list_calls()["items"]
        assert journal == durable[-1]
        if stage == "scope":
            assert journal == {}
        else:
            assert all(not row["batches"] for row in journal.values())
        before = resource_budget.budget_snapshot(identity)
        resource_budget.increase_budget(identity, amount_limit="1", expected_revision=before["revision"])
        result = invoke(db, stage, model, journal, persist=lambda: durable.append(deepcopy(journal)))
        again = invoke(db, stage, model, journal)
    assert result["passed"] and again["passed"] and len(requests) == 1
    assert len(call_ledger.list_calls()["items"]) == 1
