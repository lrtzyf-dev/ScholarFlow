"""Read-only HTTP views recover visible output, then follow the same execution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import queue
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import approval_store, cancel_store, chat_events, run_store
from app import db as database
from app.agent import loop
from app.api import chat
from app.db import SessionLocal
from app.foreground_run_store import finish
from app.llm import LLMResponse
from app.main import app
from app.models import DeletedConversation, ForegroundRun, QARecord
from app.schemas import ChatRequest

from tests.test_chat_detached_http import create_run, get_run, local_server, wait_for


@pytest.fixture(autouse=True)
def isolated_observers(monkeypatch):
    monkeypatch.setattr(run_store, "_active", {})
    monkeypatch.setattr(run_store, "_finished", {})
    monkeypatch.setattr(run_store, "_early_cancels", {})
    monkeypatch.setattr(chat_events, "_channels", {})
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(approval_store, "_pending", {})


def events(response):
    for line in response.iter_lines():
        if line.startswith("data: "):
            yield json.loads(line[6:])


def event_until(source, kind):
    for event in source:
        if event["kind"] == kind:
            return event
    pytest.fail(f"stream ended before {kind}")


def test_observer_waits_for_atomic_run_channel_publication(monkeypatch):
    run_id, cid = "chat_reattach_publish_001", "c_reattach_publish"
    publishing, publish, attempting_observe, complete = (threading.Event() for _ in range(4))
    original_begin, original_coordinated = chat_events.begin, run_store.coordinated
    executions = []

    def delayed_begin(*args, **kwargs):
        publishing.set()
        assert publish.wait(5), "test did not release channel publication"
        return original_begin(*args, **kwargs)

    @contextmanager
    def tracked_coordination():
        if threading.current_thread().name.startswith("reattach-observer"):
            attempting_observe.set()
        with original_coordinated():
            yield

    def fake_agent(*_args, **_kwargs):
        executions.append(True)
        yield "answer_delta", {"text": "LIVE_DRAFT"}
        assert complete.wait(5)
        yield "cancelled", {"text": "Test completed"}

    monkeypatch.setattr(chat_events, "begin", delayed_begin)
    monkeypatch.setattr(run_store, "coordinated", tracked_coordination)
    monkeypatch.setattr(chat, "run_agent", fake_agent)
    create_run(TestClient(app), run_id, cid, "q")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reattach-admission") as admission_pool, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="reattach-observer") as observer_pool:
        try:
            admission = admission_pool.submit(chat.chat_stream, ChatRequest(question="q", run_id=run_id,
                                                                           conversation_id=cid))
            assert publishing.wait(5)
            observation = observer_pool.submit(chat.observe_run, run_id, cid, 0)
            assert attempting_observe.wait(5), "observation bypassed run publication coordination"
            assert not observation.done(), "an unpublished live run was mistaken for an interrupted run"
            publish.set()
            initial, returning = admission.result(timeout=5), observation.result(timeout=5)

            async def inspect():
                await anext(initial.body_iterator)
                await initial.body_iterator.aclose()
                first = await anext(returning.body_iterator)
                recovered = json.loads(next(line[6:] for line in first.splitlines() if line.startswith("data: ")))
                assert recovered["kind"] == "snapshot" and recovered["status"] == "running"
                assert recovered["terminal"] is False
                await returning.body_iterator.aclose()

            asyncio.run(inspect())
            wait_for(lambda: bool(executions), "admitted execution did not start")
            assert executions == [True], "observation must never launch another model execution"
        finally:
            publish.set()
            complete.set()
            if run_id in run_store.active_runs():
                wait_for(lambda: run_id not in run_store.active_runs(), "publication test worker did not finish")


def test_snapshot_does_not_reemit_a_delta_recorded_before_subscription():
    channel = chat_events.begin("chat_reattach_cursor_001", "c_reattach_cursor")
    previous = channel.record("answer_delta", {"text": "BEFORE"})
    snapshot, observer = channel.subscribe()
    channel.broadcast(previous)  # Record and broadcast can straddle subscribe().
    following = channel.record("answer_delta", {"text": "AFTER"})
    channel.broadcast(following)
    channel.broadcast(channel.end("completed"))

    async def collect():
        return [json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
                async for frame in chat._observe(channel, observer, snapshot=snapshot)]

    observed = asyncio.run(collect())
    assert [event["kind"] for event in observed] == ["snapshot", "answer_delta", "stream_end"]
    assert observed[0]["partial_answer"] == "BEFORE" and observed[1] == following


def test_display_snapshot_migrates_legacy_foreground_table_idempotently(monkeypatch):
    legacy = create_engine("sqlite://")
    try:
        with legacy.begin() as connection:
            connection.execute(text("CREATE TABLE foreground_runs (id VARCHAR(96) PRIMARY KEY, "
                                    "partial_answer TEXT, updated_at DATETIME)"))
            connection.execute(text("INSERT INTO foreground_runs (id, partial_answer, updated_at) "
                                    "VALUES ('chat_legacy_snapshot', 'KEPT_DRAFT', '2026-11-14 00:00:00')"))
        monkeypatch.setattr(database, "engine", legacy)
        database._lightweight_migrations()
        with legacy.begin() as connection:
            row = connection.execute(text("SELECT partial_answer, display_snapshot FROM foreground_runs")).one()
            assert row.partial_answer == "KEPT_DRAFT" and row.display_snapshot is None
            connection.execute(text("UPDATE foreground_runs SET display_snapshot = :snapshot"),
                               {"snapshot": '{"partial_answer":"PERSISTED_SNAPSHOT","cursor":7}'})
        database._lightweight_migrations()
        with legacy.connect() as connection:
            columns = [row[1] for row in connection.execute(text("PRAGMA table_info(foreground_runs)"))]
            assert columns.count("display_snapshot") == 1
            row = connection.execute(text("SELECT partial_answer, display_snapshot FROM foreground_runs")).one()
            assert row.partial_answer == "KEPT_DRAFT"
            assert json.loads(row.display_snapshot) == {"partial_answer": "PERSISTED_SNAPSHOT", "cursor": 7}
    finally:
        legacy.dispose()


@pytest.mark.parametrize("complete_thinking_event", [True, False])
def test_returning_view_recovers_thought_then_receives_more_before_final_answer(monkeypatch, complete_thinking_event):
    run_id, cid, question = "chat_reattach_model_001", "c_reattach_model", "Continue this synthetic answer"
    detached, attached, finish_answer = (threading.Event() for _ in range(3))
    calls = []

    class Model:
        def stream_chat(self, *_args, **_kwargs):
            calls.append(True)
            yield "delta", "thinking", "THOUGHT_BEFORE_LEAVING"
            assert detached.wait(10)
            yield "delta", "thinking", "THOUGHT_WHILE_AWAY"
            yield "delta", "answer", "ANSWER_WHILE_AWAY"
            assert attached.wait(10)
            yield "delta", "thinking", "THOUGHT_AFTER_RETURN"
            yield "delta", "answer", "ANSWER_AFTER_RETURN"
            assert finish_answer.wait(10)
            yield "final", LLMResponse("ANSWER_WHILE_AWAYANSWER_AFTER_RETURN",
                                        reasoning_content=("THOUGHT_BEFORE_LEAVINGTHOUGHT_WHILE_AWAYTHOUGHT_AFTER_RETURN"
                                                           if complete_thinking_event else ""))

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    with local_server() as client:
        try:
            create_run(client, run_id, cid, question)
            with client.stream("POST", "/api/chat/stream", json={
                "question": question, "conversation_id": cid, "run_id": run_id,
            }) as response:
                assert response.status_code == 200
                event_until(events(response), "thinking_delta")
            detached.set()
            wait_for(lambda: (run := get_run(client, cid)) and run["display_snapshot"]["partial_answer"] == "ANSWER_WHILE_AWAY",
                     "background output was not restored in the public snapshot")
            with client.stream("GET", f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid}) as response:
                assert response.status_code == 200
                stream = events(response)
                snapshot = next(stream)
                assert snapshot["kind"] == "snapshot" and snapshot["terminal"] is False
                assert snapshot["thinking"] == "THOUGHT_BEFORE_LEAVINGTHOUGHT_WHILE_AWAY"
                assert snapshot["partial_answer"] == "ANSWER_WHILE_AWAY"
                assert snapshot["result"] is None
                assert calls == [True]
                attached.set()
                thought = event_until(stream, "thinking_delta")
                assert thought["text"] == "THOUGHT_AFTER_RETURN" and thought["seq"] > snapshot["cursor"]
                answer_delta = event_until(stream, "answer_delta")
                assert answer_delta["text"] == "ANSWER_AFTER_RETURN"
                with SessionLocal() as db:
                    assert db.query(QARecord).count() == 0, "output must be visible before final answer exists"
                finish_answer.set()
                result = event_until(stream, "answer")
                ending = event_until(stream, "stream_end")
                assert ending["terminal"] is True and ending["status"] == "completed"
                assert result["seq"] < ending["seq"] and calls == [True]
            wait_for(lambda: run_id not in run_store.active_runs(), "original worker did not finish")
            with SessionLocal() as db:
                run = db.get(ForegroundRun, run_id)
                assert run.display_snapshot["result"]["qa_id"] == result["qa_id"]
                assert run.display_snapshot["thinking_parts"] == [
                    "THOUGHT_BEFORE_LEAVINGTHOUGHT_WHILE_AWAYTHOUGHT_AFTER_RETURN"]
                assert db.query(QARecord).count() == 1
        finally:
            detached.set()
            attached.set()
            finish_answer.set()
            if run_id in run_store.active_runs():
                run_store.cancel(run_id)
                wait_for(lambda: run_id not in run_store.active_runs(), "test worker did not stop")


def test_two_observers_receive_reset_and_tool_history_without_reexecuting(monkeypatch):
    run_id, cid = "chat_reattach_tools_001", "c_reattach_tools"
    release, complete = threading.Event(), threading.Event()
    executions, writes = [], []

    def fake_agent(db, *_args, **kwargs):
        executions.append(True)
        yield "thinking_delta", {"text": "VISIBLE_THINKING"}
        yield "tool_call", {"name": "synthetic_write", "arguments": {"path": "fixture.txt"}}
        writes.append("fixture.txt")
        yield "tool_result", {"name": "synthetic_write", "executed": True, "result": "saved"}
        yield "answer_delta", {"text": "OLD_DRAFT"}
        yield "answer_reset", {"text": "Rewrite from verified results"}
        yield "answer_delta", {"text": "CURRENT_DRAFT"}
        assert release.wait(10)
        yield "answer_delta", {"text": "_LIVE_TAIL"}
        assert complete.wait(10)
        db.add(QARecord(id="qa_reattach_tools", conversation_id=cid, question="q", answer="CURRENT_DRAFT_LIVE_TAIL"))
        finish(db, kwargs["run_id"], "completed")
        db.commit()
        yield "answer", {"answer": "CURRENT_DRAFT_LIVE_TAIL", "qa_id": "qa_reattach_tools", "evidence": []}
        yield "done", {"text": "done"}

    monkeypatch.setattr(chat, "run_agent", fake_agent)
    with local_server() as client:
        try:
            create_run(client, run_id, cid, "q")
            with client.stream("POST", "/api/chat/stream", json={"question": "q", "conversation_id": cid, "run_id": run_id}) as response:
                event_until(events(response), "answer_reset")
            wait_for(lambda: (run := get_run(client, cid)) and run["display_snapshot"]["partial_answer"] == "CURRENT_DRAFT",
                     "reset draft was not reflected in recovered state")
            with client.stream("GET", f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid}) as first:
                one = events(first)
                snapshot_one = next(one)
                with httpx.Client(base_url=str(client.base_url), timeout=10) as another:
                    with another.stream("GET", f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid,
                                                                                          "after": snapshot_one["cursor"]}) as second:
                        two = events(second)
                        snapshot_two = next(two)
                        assert snapshot_two["cursor"] == snapshot_one["cursor"]
                        assert snapshot_two["partial_answer"] == "CURRENT_DRAFT"
                        assert any(event["kind"] == "tool_result" and event["result"] == "saved"
                                   for event in snapshot_two["events"])
                        assert "OLD_DRAFT" not in snapshot_two["partial_answer"]
                        release.set()
                        tail_one = event_until(one, "answer_delta")
                        tail_two = event_until(two, "answer_delta")
                        assert tail_one == tail_two and tail_one["seq"] > snapshot_two["cursor"]
                        assert executions == [True] and writes == ["fixture.txt"]
                        complete.set()
                        assert event_until(one, "answer")["qa_id"] == "qa_reattach_tools"
                        assert event_until(two, "answer")["qa_id"] == "qa_reattach_tools"
                        event_until(one, "stream_end")
                        event_until(two, "stream_end")
            wait_for(lambda: run_id not in run_store.active_runs(), "test worker did not finish")
            with SessionLocal() as db:
                assert db.query(QARecord).count() == 1
                persisted = db.get(ForegroundRun, run_id).display_snapshot
                assert persisted["partial_answer"] == "CURRENT_DRAFT_LIVE_TAIL" and persisted["terminal"]
            chat_events._channels.clear()
            ended = client.get(f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid})
            saved = [json.loads(line[6:]) for line in ended.text.splitlines() if line.startswith("data: ")]
            assert saved[0]["kind"] == "snapshot" and saved[0]["result"]["qa_id"] == "qa_reattach_tools"
            assert saved[-1]["kind"] == "stream_end" and saved[-1]["status"] == "completed"
            assert executions == [True] and writes == ["fixture.txt"]
        finally:
            release.set()
            complete.set()
            if run_id in run_store.active_runs():
                run_store.cancel(run_id)
                wait_for(lambda: run_id not in run_store.active_runs(), "test worker did not stop")


def test_public_commentary_replays_in_tool_order_then_streams_without_duplicate_model_calls(monkeypatch):
    run_id, cid = "chat_commentary_replay_001", "c_commentary_replay"
    away, returned, final = (threading.Event() for _ in range(3))
    model_calls, tools = [], []
    progress = ["Check the initial inventory.", "The first page is empty; check the next page.",
                "The second page is empty too; confirm the final page."]

    class Model:
        def stream_chat(self, *_args, **_kwargs):
            index = len(model_calls)
            model_calls.append(index)
            if index < 3:
                if index:
                    assert (away if index == 1 else returned).wait(10)
                if index == 1:
                    yield "delta", "thinking", "PROVIDER_REASONING"
                yield "delta", "answer", progress[index]
                yield "final", LLMResponse(progress[index], [{"id": f"page-{index}", "name": "list_papers",
                                                             "arguments": {"page": index + 1}}],
                                            reasoning_content="PROVIDER_REASONING" if index == 1 else "")
            else:
                assert final.wait(10)
                yield "delta", "answer", "FINAL_INVENTORY"
                yield "final", LLMResponse("FINAL_INVENTORY")

    def tool(_db, name, arguments, **_kwargs):
        tools.append(arguments["page"])
        return json.dumps({"papers": [], "page": arguments["page"]})

    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    monkeypatch.setattr(loop, "execute_tool", tool)
    with local_server() as client:
        try:
            create_run(client, run_id, cid, "Check the inventory pages")
            with client.stream("POST", "/api/chat/stream", json={
                "question": "Check the inventory pages", "conversation_id": cid, "run_id": run_id,
            }) as response:
                source = events(response)
                first_comment = event_until(source, "commentary")
                assert first_comment["text"] == progress[0] and first_comment["draft_to_commentary"] is True
                event_until(source, "tool_result")
            away.set()
            wait_for(lambda: len(tools) == 2 and sum(event["kind"] == "tool_result" for event in
                     get_run(client, cid)["display_snapshot"]["events"]) == 2, "second page was not processed while away")
            with client.stream("GET", f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid}) as response:
                source = events(response)
                snapshot = next(source)
                assert snapshot["partial_answer"] == "" and snapshot["result"] is None
                assert snapshot["thinking_parts"] == ["PROVIDER_REASONING"] and snapshot["thinking"] == ""
                relevant = [event for event in snapshot["events"] if event["kind"] in {
                    "commentary", "tool_call", "tool_result"}]
                assert [event["kind"] for event in relevant] == ["commentary", "tool_call", "tool_result"] * 2
                assert [event["text"] for event in relevant if event["kind"] == "commentary"] == progress[:2]
                assert [event["seq"] for event in relevant] == sorted(event["seq"] for event in relevant)
                returned.set()
                third = event_until(source, "commentary")
                assert third["text"] == progress[2] and third["seq"] > snapshot["cursor"]
                event_until(source, "tool_result")
                with SessionLocal() as db:
                    assert db.query(QARecord).count() == 0
                final.set()
                assert event_until(source, "answer")["answer"] == "FINAL_INVENTORY"
                assert event_until(source, "stream_end")["status"] == "completed"
            wait_for(lambda: run_id not in run_store.active_runs(), "commentary worker did not finish")
            assert model_calls == [0, 1, 2, 3] and tools == [1, 2, 3]
            with SessionLocal() as db:
                record = db.query(QARecord).one()
                saved = db.get(ForegroundRun, run_id).display_snapshot
                assert saved["partial_answer"] == "FINAL_INVENTORY"
                assert saved["thinking_parts"] == ["PROVIDER_REASONING"]
                assert [event["text"] for event in record.trace if event["kind"] == "commentary"] == progress
                assert not any(event["kind"] == "thinking" and event.get("preamble") for event in record.trace)
            chat_events._channels.clear()
            recovered = client.get(f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid})
            saved_snapshot = next(event for event in events(recovered) if event["kind"] == "snapshot")
            assert [event["text"] for event in saved_snapshot["events"] if event["kind"] == "commentary"] == progress
            assert saved_snapshot["thinking_parts"] == ["PROVIDER_REASONING"]
        finally:
            away.set()
            returned.set()
            final.set()
            if run_id in run_store.active_runs():
                run_store.cancel(run_id)
                wait_for(lambda: run_id not in run_store.active_runs(), "commentary worker did not stop")


def test_legacy_public_preamble_is_not_restored_as_reasoning():
    saved = {"thinking_parts": ["PUBLIC_PROGRESS", "REAL_REASONING"], "events": [
        {"kind": "thinking", "text": "PUBLIC_PROGRESS", "preamble": True},
        {"kind": "tool_call", "name": "fixture"},
        {"kind": "thinking", "text": "REAL_REASONING"},
    ]}
    snapshot = chat_events.initial_snapshot("chat_legacy_preamble_001", "c_legacy_preamble", saved)
    assert snapshot["thinking_parts"] == ["REAL_REASONING"] and snapshot["events"] == saved["events"]
    channel = chat_events.begin("chat_legacy_preamble_002", "c_legacy_preamble")
    channel.record("thinking_delta", {"text": "ACTUAL_REASONING_IN_FLIGHT"})
    channel.record("thinking", {"text": "OLD_PUBLIC_PREAMBLE", "preamble": True})
    channel.record("commentary", {"text": "PUBLIC_COMMENTARY", "source": "model", "draft_to_commentary": True})
    snapshot = channel.snapshot()
    assert snapshot["thinking_parts"] == [] and snapshot["thinking"] == "ACTUAL_REASONING_IN_FLIGHT"


def test_observation_is_conversation_scoped_and_never_exposes_private_state(db):
    run_id, cid = "chat_reattach_private_001", "c_reattach_private"
    db.add(ForegroundRun(id=run_id, conversation_id=cid, question="q", status="completed", partial_answer="VISIBLE",
                         runtime_config={"mode": "api", "model_id": "Model-Case", "credential_ref": "PRIVATE_CREDENTIAL"},
                         agent_checkpoint={"private": "PRIVATE_EXECUTOR_STATE"}))
    db.commit()
    client = TestClient(app)
    assert client.get(f"/api/chat/runs/{run_id}/events", params={"conversation_id": "wrong"}).status_code == 404
    response = client.get(f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid})
    assert response.status_code == 200 and "VISIBLE" in response.text and "Model-Case" in response.text
    assert "PRIVATE_CREDENTIAL" not in response.text and "PRIVATE_EXECUTOR_STATE" not in response.text
    assert "agent_checkpoint" not in response.text and "credential_ref" not in response.text
    db.add(DeletedConversation(id=cid))
    db.commit()
    assert client.get(f"/api/chat/runs/{run_id}/events", params={"conversation_id": cid}).status_code == 410


def test_orphaned_saved_run_is_terminal_and_dismissed_run_is_not_observable(db):
    cid = "c_reattach_orphan"
    db.add_all([
        ForegroundRun(id="chat_reattach_orphan_001", conversation_id=cid, question="q", status="running", partial_answer="SAVED_BEFORE_RESTART"),
        ForegroundRun(id="chat_reattach_dismissed", conversation_id=cid, question="q", status="dismissed"),
    ])
    db.commit()
    client = TestClient(app)
    response = client.get("/api/chat/runs/chat_reattach_orphan_001/events", params={"conversation_id": cid})
    body = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert body[0]["partial_answer"] == "SAVED_BEFORE_RESTART"
    assert body[0]["terminal"] is True and body[0]["status"] == "interrupted"
    assert body[-1]["kind"] == "stream_end" and not run_store.active_runs()
    assert client.get("/api/chat/runs/chat_reattach_dismissed/events", params={"conversation_id": cid}).status_code == 404


def test_bounded_slow_observer_coalesces_to_snapshot_and_terminal_hubs_are_pruned(monkeypatch):
    channel = chat_events.begin("chat_reattach_bounded_001", "c_bounded")
    first, observer = channel.subscribe()
    for _ in range(500):
        channel.broadcast(channel.record("answer_delta", {"text": "x"}))
    assert observer.qsize() <= chat_events.OBSERVER_QUEUE_SIZE
    queued = []
    while not observer.empty():
        queued.append(observer.get_nowait())
    assert chat_events.RESYNC in queued
    snapshot = channel.snapshot()
    assert snapshot["partial_answer"] == "x" * 500 and snapshot["cursor"] > first["cursor"]
    for _ in range(300):
        channel.record("tool_result", {"name": "fixture", "result": "x" * 5000})
    snapshot = channel.snapshot()
    assert len(snapshot["events"]) <= chat_events.MAX_EVENTS and snapshot["history_truncated"]
    assert sum(len(json.dumps(event, ensure_ascii=False)) for event in snapshot["events"]) <= chat_events.MAX_EVENT_CHARS
    channel.unsubscribe(observer)
    monkeypatch.setattr(chat_events, "MAX_FINISHED", 2)
    for index in range(4):
        item = chat_events.begin(f"chat_reattach_prune_{index:03}", "c_bounded")
        item.end("completed")
        chat_events.release_finished()
    assert len([item for item in chat_events._channels.values() if item.closed_at is not None]) <= 2
    ephemeral = chat_events.begin("chat_reattach_no_row_001", None, durable=False)
    ephemeral.end("failed")
    chat_events.release_finished()
    assert chat_events.get("chat_reattach_no_row_001") is None
