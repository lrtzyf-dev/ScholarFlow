"""D06 harness counterexamples only; no real provider or service traffic."""
from contextlib import contextmanager
from copy import deepcopy
import asyncio
import hashlib
import json
import threading

import httpx
import pytest

from scripts import verify_real_acceptance as subject


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_pre_meter_budget_denial_is_resource_boundary_with_no_dispatch_proof(db):
    from app.agent.answer_review_journal import cached_review
    from app.llm import APIClient
    from app.resource_budget import ResourceBudgetExceeded
    client = APIClient(synthetic=True)
    gate = subject.EvaluationRequestBudget(1, 30, {"provider.example.test"})
    gate.metered_model_attempts = 1
    entered = []

    @contextmanager
    def original_meter(self, *args, **kwargs):
        entered.append(True)
        yield None

    wrapped = gate.wrap_meter(original_meter)
    journal = {}

    def review():
        with wrapped(client, "https://provider.example.test", "model", {}):
            pytest.fail("Denied admission entered the model body")

    with pytest.raises(ResourceBudgetExceeded):
        cached_review(journal, "new", review=review, persist=lambda: None,
            before_review=lambda: None, llm=client)
    assert entered == [] and journal == {}
    receipt = gate.snapshot()["admission_denials"][0]
    assert receipt["boundary"] == "model_meter_admission"
    assert receipt["dispatch_started"] is False
    assert receipt["metered_attempts_before"] == receipt["metered_attempts_after"] == 1
    assert receipt["http_attempts_before"] == receipt["http_attempts_after"] == 0
    assert receipt["last_started_call_id_before"] == receipt["last_started_call_id_after"]


def test_pre_meter_stop_is_cancellation_boundary_and_does_not_spend(db):
    from app.request_resources import ResourceRequestCancelled
    gate = subject.EvaluationRequestBudget(2, 30, {"provider.example.test"})
    gate.stopped.set()

    @contextmanager
    def original_meter(self, *args, **kwargs):
        pytest.fail("Stopped admission reached the original meter")
        yield None

    from app.llm import APIClient
    with pytest.raises(ResourceRequestCancelled):
        with gate.wrap_meter(original_meter)(APIClient(synthetic=True), "unused", "model", {}):
            pass
    assert gate.snapshot()["metered_model_attempts"] == 0
    assert gate.snapshot()["admission_denials"][0]["reason"] == "evaluation_stopped"


def test_transport_budget_after_started_meter_keeps_pending_unknown(db):
    from app.agent.answer_review_journal import cached_review
    from app.llm import APIClient
    from app.resource_budget import ResourceBudgetExceeded
    client = APIClient(synthetic=True)
    gate = subject.EvaluationRequestBudget(1, 30, {"provider.example.test"})
    gate.real_provider_calls = 1

    @contextmanager
    def started_meter(self, *args, **kwargs):
        self._call_context.last_started_call_id = "synthetic-started-call"
        yield None

    transport = gate.wrap_transport(lambda *a, **k: pytest.fail("Denied transport was entered"))

    def review():
        with gate.wrap_meter(started_meter)(client, "url", "model", {}):
            transport(None, httpx.Request("POST", "https://provider.example.test/v1"))

    journal = {}
    with pytest.raises(ResourceBudgetExceeded):
        cached_review(journal, "current", review=review, persist=lambda: None,
            before_review=lambda: None, llm=client)
    assert journal == {"current": {"review_dispatch": "pending"}}
    receipt = gate.snapshot()["admission_denials"][0]
    assert receipt["boundary"] == "http_transport_admission"
    assert receipt["dispatch_started"] is False
    assert receipt["http_attempts_before"] == receipt["http_attempts_after"] == 1


def test_transport_started_timeout_remains_unknown_and_is_not_marked_denied(db):
    from app.agent.answer_review_journal import cached_review
    from app.llm import APIClient
    client = APIClient(synthetic=True)
    gate = subject.EvaluationRequestBudget(2, 30, {"provider.example.test"})

    @contextmanager
    def started_meter(self, *args, **kwargs):
        self._call_context.last_started_call_id = "synthetic-possibly-sent-call"
        yield None

    def timed_out(transport, request):
        raise httpx.ReadTimeout("Synthetic response lost", request=request)

    transport = gate.wrap_transport(timed_out)
    journal = {}

    def review():
        with gate.wrap_meter(started_meter)(client, "url", "model", {}):
            transport(None, httpx.Request("POST", "https://provider.example.test/v1"))

    with pytest.raises(httpx.ReadTimeout):
        cached_review(journal, "current", review=review, persist=lambda: None,
            before_review=lambda: None, llm=client)
    assert journal == {"current": {"review_dispatch": "pending"}}
    assert gate.snapshot()["real_provider_calls"] == 1
    assert gate.snapshot()["admission_denials"] == []


def test_unconfigured_transport_never_dispatches_or_copies_request_secrets():
    gate = subject.EvaluationRequestBudget(2, 30, {"provider.example.test"})
    transport = gate.wrap_transport(lambda *a, **k: pytest.fail("External host escaped"))
    with pytest.raises(RuntimeError):
        transport(None, httpx.Request("POST", "https://other.example.test/private?token=PRIVATE-URL",
            headers={"authorization": "PRIVATE-HEADER"}, content=b"PRIVATE-BODY"))
    assert gate.snapshot()["real_provider_calls"] == 0
    assert "PRIVATE-" not in json.dumps(gate.snapshot())


class ObserverClient:
    """run_chat's real pool workers consume production _observe/channel events."""
    def __init__(self, mutation=None, terminal_snapshot_fallback=False):
        self.mutation = mutation
        self.terminal_snapshot_fallback = terminal_snapshot_fallback
        self.attached = threading.Event()
        self.calls = 0
        self.channel = None
        self.posts = []
        self.observer_requests = []

    @staticmethod
    def response(value=None, events=None):
        return httpx.Response(200, json=value) if events is None else httpx.Response(200,
            text="\n".join("data:" + json.dumps(value) for value in events))

    def post(self, path, json=None, **kwargs):
        from app import chat_events
        self.posts.append(path)
        if path == "/api/foreground-runs":
            self.rid = json["id"]
            self.cid = json["conversation_id"]
            self.channel = chat_events.begin(self.rid, self.cid, durable=False, attempt_id="synthetic-attempt")
            return self.response({"id": self.rid})
        if path == "/api/chat/stream":
            self.calls += 1
            assert self.attached.wait(8), "Observer did not attach to the running channel"
            events = []
            for kind, value in [("thinking_delta", {"text": "PRIVATE-THINKING"}),
                    ("answer_delta", {"text": "Public subsequent output."}),
                    ("answer", {"answer": "Public subsequent output.", "completed": True, "outcome": "completed",
                        "provider_context": "PRIVATE-PROVIDER", "agent_checkpoint": "PRIVATE-CHECKPOINT"})]:
                event = self.channel.record(kind, value)
                self.channel.broadcast(event)
                events.append(event)
            terminal = self.channel.end("completed")
            if not self.terminal_snapshot_fallback:
                self.channel.broadcast(terminal)
            events.append(terminal)
            return self.response(events=events)
        raise AssertionError(path)

    def get(self, path, params=None):
        if path == "/api/foreground-runs":
            return self.response({"runs": [] if self.channel is None else
                [{"id": self.rid, "status": "running", "workspace_revision": 0}]})
        if path.endswith("/events"):
            from app.api.chat import _observe
            self.observer_requests.append(deepcopy(params))
            snapshot, queue = self.channel.subscribe()
            first = len(self.observer_requests) == 1
            if first:
                self.attached.set()

            async def collect():
                return "".join([frame async for frame in _observe(self.channel, queue, snapshot=snapshot)])

            response = httpx.Response(200, text=asyncio.run(collect()))
            if first and self.mutation:
                events = subject.events_from(response)
                events = self.mutation(events)
                response = self.response(events=events)
            return response
        raise AssertionError(path)


def drive_observer(monkeypatch, mutation=None, terminal_snapshot_fallback=False):
    from app import call_ledger
    client = ObserverClient(mutation, terminal_snapshot_fallback=terminal_snapshot_fallback)
    monkeypatch.setattr(call_ledger, "summarize_calls", lambda **k: {"call_count": client.calls})
    case = {"prompts": ["Explain the fixed file."], "control": {"kind": "reattach"}, "machine_checks": {}}
    result = subject.run_chat(client, case, "observer-conversation", {}, 15)
    return client, case, result


def test_run_chat_retains_actual_live_observer_output_cursor_and_no_replay(db, monkeypatch):
    client, case, result = drive_observer(monkeypatch)
    receipt = result["controller"]["observer_receipt"]
    assert receipt["live_followup_verified"] is True
    assert receipt["initial_snapshot"]["status"] == "running"
    assert receipt["initial_snapshot"]["terminal"] is False
    assert receipt["later_output_events"] >= 1
    assert receipt["final_answer_sha256"] == digest(result["answer"])
    assert receipt["terminal_call_count_before"] == receipt["terminal_call_count_after"] == 1
    assert result["controller"]["reattach_no_replay"] is True
    assert client.posts == ["/api/foreground-runs", "/api/chat/stream"]
    assert len(client.observer_requests) == 2
    assert "PRIVATE-" not in json.dumps(receipt)
    checks = subject.evaluate_receipts(case, result, None, "observer-conversation", "synthetic")
    assert checks["reattach_followup_observed"] is True


def test_production_terminal_snapshot_fallback_inherits_its_verified_attempt(db, monkeypatch):
    _, _, result = drive_observer(monkeypatch, terminal_snapshot_fallback=True)
    receipt = result["controller"]["observer_receipt"]
    assert receipt["public_events"][-2]["kind"] == "snapshot"
    assert receipt["public_events"][-1]["kind"] == "stream_end"
    assert receipt["public_events"][-1]["attempt_id"] is None
    assert receipt["public_events"][-1]["seq"] == receipt["public_events"][-2]["seq"]
    assert receipt["live_followup_verified"] is True, receipt["validation_errors"]


def wrong_run(events):
    events[0]["run_id"] = "chat_foreignrun"
    return events


def wrong_conversation(events):
    events[0]["conversation_id"] = "foreign-conversation"
    return events


def stale_output(events):
    for value in events[1:]:
        if value["kind"] in {"answer_delta", "answer"}:
            value["seq"] = events[0]["cursor"]
    return events


def only_terminal(events):
    return [events[0], next(value for value in events if value["kind"] == "stream_end")]


def wrong_answer(events):
    next(value for value in events if value["kind"] == "answer")["answer"] = "Foreign answer."
    return events


@pytest.mark.parametrize("mutation", [wrong_run, wrong_conversation, stale_output, only_terminal, wrong_answer])
def test_live_observer_missing_or_foreign_output_cannot_pass(db, monkeypatch, mutation):
    _, case, result = drive_observer(monkeypatch, mutation)
    receipt = result["controller"]["observer_receipt"]
    assert receipt["live_followup_verified"] is False
    assert subject.evaluate_receipts(case, result, None, "observer-conversation", "synthetic")["reattach_followup_observed"] is False


def test_legacy_boolean_without_observer_evidence_cannot_pass(db):
    case = {"prompts": ["Explain."], "control": {"kind": "reattach"}, "machine_checks": {"reattach_no_replay": True}}
    result = {"answer": "Public answer.", "turns": [{"http_status": 200, "answer": "Public answer.",
        "completed": True, "outcome": "completed"}], "controller": {"reattach_no_replay": True,
        "live_observation_started": True}}
    checks = subject.evaluate_receipts(case, result, None, "observer-conversation", "synthetic")
    assert checks["reattach_followup_observed"] is False


def test_main_uses_real_budget_wrappers_and_persists_denial_without_transport(db, monkeypatch, tmp_path):
    from app.llm import APIClient
    from app.resource_budget import ResourceBudgetExceeded
    monkeypatch.setattr(subject, "model_configuration", lambda *a: {"api_key": "fabricated-d06-key",
        "base_url": "https://api.deepseek.com/v1", "model_id": "Synthetic-D06",
        "storage_kind": "synthetic_test_only"})
    # conftest already owns a unique synthetic DATA_DIR; main must not move
    # this in-process test onto a second globals/engine configuration.
    monkeypatch.setattr(subject, "isolated_environment", lambda *a: {})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a, **k: pytest.fail("A real HTTP escaped"))

    def spend_then_deny(*args, **kwargs):
        client = APIClient(synthetic=True)
        with client._meter("https://api.deepseek.com/v1/chat/completions", "Synthetic-D06", {}):
            pass
        with pytest.raises(ResourceBudgetExceeded):
            with client._meter("https://api.deepseek.com/v1/chat/completions", "Synthetic-D06", {}):
                pytest.fail("Budget cap was bypassed")
        return {"turns": [{"http_status": 200, "run_id": "synthetic-budget-run", "answer": "Public answer.",
            "completed": True, "outcome": "completed", "evidence": [], "tools": []}],
            "answer": "Public answer.", "evidence": [], "tools": []}

    monkeypatch.setattr(subject, "run_chat", spend_then_deny)
    monkeypatch.setattr(subject, "collect_research_outputs", lambda *a, **k: {})
    destination = tmp_path / "budget-run.json"
    assert subject.main(["--case", "DAILY01", "--report", str(destination), "--run-real",
        "--credentials-dir", str(tmp_path), "--max-calls", "1"]) == 0
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["real_provider_calls"] == 0
    assert report["metered_model_attempts"] == 1
    assert report["evaluation_budget"]["admission_denials"][0]["reason"] == "maximum_metered_attempts"
    assert report["evaluation_budget"]["admission_denials"][0]["dispatch_started"] is False
    assert report["passed"] is False and report["commercial_acceptance"] is False
    assert "fabricated-d06-key" not in destination.read_text(encoding="utf-8")


def test_actual_foreground_and_observer_endpoints_record_live_output_without_model_replay(db, monkeypatch):
    from app import call_ledger, chat_events, task_runner
    from app.agent import loop
    from app.llm import LLMResponse
    from app.main import app
    from app.runtime_context import current_runtime_config
    from fastapi.testclient import TestClient
    import time

    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    counter = {"calls": 0}

    class Model:
        def stream_chat(self, *args, **kwargs):
            counter["calls"] += 1
            rid = current_runtime_config()["run_id"]
            until = time.monotonic() + 8
            while time.monotonic() < until:
                channel = chat_events.get(rid)
                if channel is not None and channel.subscribers:
                    break
                time.sleep(.01)
            else:
                pytest.fail("Real observer endpoint did not subscribe to the admitted run")
            yield "delta", "answer", "Public actual endpoint output."
            yield "final", LLMResponse("Public actual endpoint output.")

    monkeypatch.setattr(loop, "get_llm", Model)
    monkeypatch.setattr(call_ledger, "summarize_calls", lambda **k: {"call_count": counter["calls"]})
    case = {"prompts": ["Give a plain independent explanation."], "control": {"kind": "reattach"}, "machine_checks": {}}
    with TestClient(app) as client:
        cid = client.post("/api/conversations", json={"title": "D06 actual observer endpoints"}).json()["id"]
        result = subject.run_chat(client, case, cid, {}, 20)
    receipt = result["controller"]["observer_receipt"]
    assert receipt["live_followup_verified"] is True, receipt
    assert receipt["terminal_no_replay_verified"] is True
    assert receipt["terminal_call_count_before"] == receipt["terminal_call_count_after"] == counter["calls"] == 1
    assert receipt["final_answer_sha256"] == digest("Public actual endpoint output.")
