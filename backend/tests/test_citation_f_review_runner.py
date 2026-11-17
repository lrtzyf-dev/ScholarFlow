"""The F review entrypoint is inert by default and rebuilds only fixed fixtures."""
import json
import os
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.llm import LLMResponse
from app.main import app
from scripts import verify_citation_f_review as runner


def test_default_f_review_plan_never_reads_credentials_or_opens_http(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The default plan may not read credentials or execute requests")

    monkeypatch.setattr(runner, "model_configuration", forbidden)
    monkeypatch.setattr(runner, "execute", forbidden)
    monkeypatch.setattr(httpx.Client, "post", forbidden)
    path = tmp_path / "plan.json"
    assert runner.main(["--report", str(path), "--credentials-dir", str(tmp_path / "does-not-exist")]) == 0
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["credentials_read"] is False and value["real_provider_calls"] == 0
    assert value["maximum_real_requests"] == 4 and value["execution_status"] == "not_run"
    assert [case["id"] for case in value["cases"]] == ["DOC05", "DOC12"]
    assert value["commercial_acceptance"] is False


def test_single_f_case_plan_does_not_claim_short_budget_covers_retained_claims(tmp_path):
    path = tmp_path / "single-plan.json"
    assert runner.main(["--report", str(path), "--case", "DOC05", "--max-calls", "4"]) == 0
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["maximum_real_requests"] == 4 and value["estimated_requests_per_case"] == {"DOC05": 5}
    assert value["budget_covers_all_selected_cases"] is False


def test_explicit_public_subrange_plan_is_one_request_and_never_whole_answer_acceptance(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "execute", lambda *a, **k: pytest.fail("Plan must remain offline"))
    path = tmp_path / "subrange-plan.json"
    assert runner.main(["--report", str(path), "--case", "DOC05", "--claim-start", "8", "--claim-end", "12"]) == 0
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["maximum_real_requests"] == 1 and value["not_whole_answer_acceptance"] is True
    assert value["kind"] == "public_claim_subrange_diagnostic_plan" and value["credentials_read"] is False
    assert [claim["global_index"] for claim in value["selected_claims"]] == [8, 9, 10, 11]
    assert "原缓存" in value["selected_claims"][0]["text"]


@pytest.mark.parametrize("arguments", [["--max-calls", "5"], ["--max-calls", "0"],
    ["--case", "DOC05", "--case", "DOC05"], ["--run-real"]])
def test_f_runner_rejects_invalid_budget_duplicate_or_unconfigured_execution(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(runner, "execute", lambda *a, **k: pytest.fail("Invalid execution must not begin"))
    with pytest.raises(SystemExit) as exc:
        runner.main(["--report", str(tmp_path / "invalid.json"), *arguments])
    assert exc.value.code == 2


@pytest.mark.parametrize("arguments", [["--claim-start", "8"],
    ["--case", "DOC12", "--claim-start", "8", "--claim-end", "12"],
    ["--case", "DOC05", "--claim-start", "7", "--claim-end", "12"],
    ["--case", "DOC05", "--claim-start", "8", "--claim-end", "99"]])
def test_subrange_rejects_ambiguous_case_missing_bound_and_overlarge_selection(tmp_path, arguments):
    with pytest.raises(SystemExit) as exc:
        runner.main(["--report", str(tmp_path / "invalid-range.json"), *arguments])
    assert exc.value.code == 2


@pytest.mark.parametrize("case_id", ["DOC05", "DOC12"])
def test_f_case_rebuilds_authenticated_sources_and_two_successful_receipts_before_review(db, case_id):
    case, = runner.inputs([case_id])
    with TestClient(app) as client:
        bundle = runner.rebuild_case(client, case)
        assert bundle["answer"] == case["answer"] and bundle["draft_sha256"] == case["draft_sha256"]
        assert len(bundle["evidence"]) == len(bundle["process_receipts"]) == 2
        assert [entry["id"] for entry in bundle["evidence"]] == ["e1", "e2"]
        if case_id == "DOC05":
            assert [receipt["tool_name"] for receipt in bundle["process_receipts"]] == [
                "read_attached_source", "verify_attached_table"]
            computed = json.loads(bundle["process_receipts"][1]["content"])
            assert computed["result"]["outputs"][0]["value"] == 1550
            assert computed["result"]["formula_cache_used_for_computation"] is False
        else:
            assert all(receipt["whole_extracted_text_returned"] for receipt in bundle["process_receipts"])
            assert all(json.loads(receipt["content"])["read_range"]["total_lines"] == 3
                       for receipt in bundle["process_receipts"])

        class Reviewer:
            mode = "api"
            calls = 0
            def chat(self, messages, **kwargs):
                self.calls += 1
                if "引用适用范围审查员" in messages[0]["content"]:
                    prompt = json.loads(messages[1]["content"])
                    assert prompt["full_answer"] == case["answer"] and len(prompt["process_receipts"]) == 2
                    return LLMResponse(json.dumps({"results": [{"index": item["index"], "requires_citation": True,
                        "reason": "Synthetic replay boundary", "kind": "mixed", "process_scope": "none", "supports": []}
                        for item in prompt["uncited_candidates"]]}))
                assert json.dumps(case["answer"], ensure_ascii=False) in messages[1]["content"]
                assert "source_metadata=" in messages[1]["content"] and "process_receipts=" in messages[1]["content"]
                return LLMResponse('{"results":[]}')

        model = Reviewer()
        result = runner.review_case(bundle, model)
        assert model.calls == 1 + result["audit"]["batch_count"]
        from app.agent.audit_batches import MAX_CLAIMS
        assert all(batch["claim_count"] <= MAX_CLAIMS for batch in result["audit"]["batches"])
        assert not result["machine_review_passed"]
        assert result["audit"]["unknown_count"] == result["audit"]["factual_claims"] > 0
        assert result["audit"]["known_failed_count"] == 0


def test_public_subrange_reviews_only_original_global_tail_once_without_scope_or_whole_pass(db):
    case, = runner.inputs(["DOC05"])
    with TestClient(app) as client:
        bundle = runner.rebuild_case(client, case)
        class Reviewer:
            calls = []
            def chat(self, prompt, **kwargs):
                assert prompt[0]["content"] == "你是严谨的引用审计员，只输出 JSON。"
                indices = [int(value) for value in re.findall(r"^\[论断 (\d+)\]", prompt[1]["content"], re.M)]
                self.calls.append(indices)
                assert indices == [8, 9, 10, 11] and json.dumps(case["answer"], ensure_ascii=False) in prompt[1]["content"]
                return LLMResponse(json.dumps({"results": [{"index": index, "supported": False,
                    "reason": "Synthetic selected-range boundary", "support_kind": "derived" if index == 10 else "uncertain",
                    "process_scope": "none", "supports": []} for index in indices]}))
        model = Reviewer()
        result = runner.review_case(bundle, model, claim_range=(8, 12))
        assert len(model.calls) == 1 and result["not_whole_answer_acceptance"] is True
        assert result["machine_review_passed"] is False and result["audit"]["factual_claims"] == 4
        assert result["audit"]["known_failed_count"] == 1 and result["audit"]["unknown_count"] == 3


def test_optin_subrange_executes_one_mock_http_only_after_pending_report_is_saved(db, tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(runner, "model_configuration", lambda *a: {"api_key": "synthetic-subrange-key",
        "base_url": "https://api.deepseek.com/v1", "model_id": "deepseek-flash", "storage_kind": "synthetic_fixture"})
    monkeypatch.setattr(runner, "review_hashes", lambda: {"local_test": "frozen"})
    monkeypatch.setattr(config.settings, "reasoning_effort", "")
    report_path = tmp_path / "one-mock-http.json"
    observed = []
    def dispatch(transport, request):
        pending = json.loads(report_path.read_text(encoding="utf-8"))
        assert pending["active_dispatch"]["state"] == "pending"
        body = json.loads(request.content)
        assert body["reasoning_effort"] == "low" and body["max_tokens"] == 8192
        indices = [int(value) for value in re.findall(r"^\[论断 (\d+)\]", body["messages"][1]["content"], re.M)]
        assert indices == [8, 9, 10, 11]
        observed.append(indices)
        return httpx.Response(200, request=request, json={"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"results": [
                {"index": index, "supported": True, "reason": "Synthetic diagnostic contract", "support_kind": "source",
                 "process_scope": "none", "supports": []} for index in indices]})}}]})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", dispatch)
    args = SimpleNamespace(claim_start=8, claim_end=12, max_calls=4, timeout_seconds=15,
        credentials_dir=tmp_path / "not-read", report=report_path)
    names = ("DATA_DIR", "APPDATA", "LOCALAPPDATA", "LLM_API_KEY", "LLM_DEMO_MODE", "EMBEDDING_API_KEY",
             "REDIS_URL", "DESKTOP_NOTIFICATIONS", "MEMORY_CAPTURE", "SCHOLARFLOW_TEST_TRANSPORT")
    environment = {name: os.environ.get(name) for name in names}
    try:
        assert runner.execute(args, runner.inputs(["DOC05"])) == 0
    finally:
        for name, value in environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(observed) == report["real_provider_calls"] == report["maximum_real_requests"] == 1
    assert report["active_dispatch"]["state"] == "returned"
    assert report["kind"] == "public_claim_subrange_diagnostic" and report["not_whole_answer_acceptance"] is True
    assert report["machine_review_passed"] is False and report["commercial_acceptance"] is False
    assert report["selected_audit_passed"] is True and report["usage"]["actual_usage"]["total_tokens"] == 15
