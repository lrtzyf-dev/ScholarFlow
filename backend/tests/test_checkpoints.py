"""Crash boundaries, saved context and no repeated external operations on resume."""
import json
import threading
import time
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import approval_store, cancel_store, task_runner
from app.agent import loop
from app.agent.checkpoint import AgentState, CheckpointNeedsReview
from app.db import SessionLocal
from app.llm import LLMResponse
from app.main import app
from app.models import QARecord, TaskRecord


class PowerLoss(BaseException):
    pass


class Model:
    def __init__(self, guarded=False):
        self.messages = []
        self.guarded = guarded

    def stream_chat(self, messages, tools=None):
        self.messages.append(deepcopy(messages))
        if not any(m['role'] == 'tool' for m in messages):
            calls = [{"id": "first", "name": "mcp__custom__write" if self.guarded else "retrieve_evidence",
                      "arguments": {"query": "first"}}]
            if not self.guarded:
                calls.append({"id": "second", "name": "retrieve_evidence", "arguments": {"query": "second"}})
            yield "final", LLMResponse("", calls, usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
        else:
            yield "final", LLMResponse("First result[^e1]. Second result[^e2].", usage={
                "prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11})


@pytest.fixture(autouse=True)
def isolated_execution(monkeypatch):
    monkeypatch.setattr(task_runner, "_running", {})
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(approval_store, "_pending", {})
    monkeypatch.setattr(loop, "make_plan", lambda *a: [
        {"title": "Search", "phase": "search", "status": "pending"},
        {"title": "Write", "phase": "write", "status": "pending"}])
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    monkeypatch.setattr(loop, "audit_answer", lambda *a, **k: {"passed": True, "unsupported": [], "mode": "test"})
    monkeypatch.setattr(loop.settings, "approval_mode", "auto")
    monkeypatch.setattr(task_runner.notifier, "notify", lambda *a, **k: None)
    yield
    for task_id, thread in list(task_runner._running.items()):
        cancel_store.request_cancel(task_id)
        approval_store.resolve_for_run(task_id)
        if hasattr(thread, 'join'):
            thread.join(2)


def new_task(db, **kwargs):
    task = TaskRecord(type="agent", title="Checkpoint test", conversation_id="c_checkpoint",
        payload={"question": "Question", "skills": []}, status="running", events=[], **kwargs)
    db.add(task)
    db.commit()
    return task


def writer(db, task_id):
    def persist(snapshot):
        task = db.get(TaskRecord, task_id)
        task.checkpoint = snapshot
        db.commit()
    return persist


def drive(db, task, **kwargs):
    return loop.run_agent(db, "Question", conversation_id="c_checkpoint", run_id=task.id,
                          checkpoint=deepcopy(task.checkpoint), save_checkpoint=writer(db, task.id), **kwargs)


def evidence_tool(calls):
    def invoke(db, name, arguments, **kwargs):
        calls.append(arguments["query"])
        return json.dumps({"evidence": [{"id": "e1", "quote": arguments['query'], "paper_title": "Paper", "page": 1}]})
    return invoke


def test_resume_keeps_model_context_evidence_usage_and_does_not_repeat_tools(db, monkeypatch):
    model, calls = Model(), []
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "execute_tool", evidence_tool(calls))
    task = new_task(db)
    source = drive(db, task)
    for kind, _ in source:
        if kind == "tool_result":
            break
    source.close()
    task_id = task.id
    db.close()
    with SessionLocal() as restored_db:
        restored = restored_db.get(TaskRecord, task_id)
        events = list(drive(restored_db, restored))
        answer = next(e for k, e in events if k == "answer")
        assert calls == ["first", "second"]
        assert answer["evidence_ids"] == ["e1", "e2"]
        assert answer["usage"] == {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16}
        assert len(model.messages) == 2, "completed model tool decisions must not be regenerated"
        messages = model.messages[-1]
        tool_messages = [m for m in messages if m['role'] == 'tool']
        assert [m['tool_call_id'] for m in tool_messages] == ['first', 'second']
        assert [json.loads(m['content'])['evidence'][0]['id'] for m in tool_messages] == ['e1', 'e2']
        assert restored_db.query(QARecord).count() == 1
        assert restored.checkpoint['phase'] == 'completed'


@pytest.mark.parametrize('resolution', ['skip', 'retry'])
def test_unknown_tool_result_requires_explicit_resolution_without_automatic_replay(db, monkeypatch, resolution):
    model, calls = Model(), []
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    original = evidence_tool(calls)
    def crash_after_effect(*args, **kwargs):
        original(*args, **kwargs)
        raise PowerLoss()
    monkeypatch.setattr(loop, "execute_tool", crash_after_effect)
    task = new_task(db)
    with pytest.raises(PowerLoss):
        list(drive(db, task))
    assert calls == ['first']
    assert task.checkpoint['pending_operation']['name'] == 'retrieve_evidence'
    with pytest.raises(CheckpointNeedsReview):
        list(drive(db, task))
    assert calls == ['first']
    state = AgentState.model_validate(task.checkpoint)
    state.resolution = resolution
    task.checkpoint = state.snapshot()
    db.commit()
    monkeypatch.setattr(loop, "execute_tool", original)
    list(drive(db, task))
    assert calls == (['first', 'first', 'second'] if resolution == 'retry' else ['first', 'second'])
    if resolution == 'skip':
        messages = [m for m in model.messages[-1] if m['role'] == 'tool']
        skipped = json.loads(messages[0]['content'])
        assert skipped['execution_unknown'] and skipped['skipped_by_user']


def test_crash_in_post_hook_does_not_repeat_completed_tool(db, monkeypatch):
    model, calls = Model(), []
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "execute_tool", evidence_tool(calls))
    def hooks(event, *args, **kwargs):
        if event == 'PostToolUse':
            raise PowerLoss()
        return {"ran": 0, "blocked": False, "feedback": ""}
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', hooks)
    task = new_task(db)
    with pytest.raises(PowerLoss):
        list(drive(db, task))
    assert task.checkpoint['pending_operation']['kind'] == 'hook'
    state = AgentState.model_validate(task.checkpoint)
    state.resolution = 'skip'
    task.checkpoint = state.snapshot()
    db.commit()
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **k: {"ran": 0, "blocked": False, "feedback": ""})
    list(drive(db, task))
    assert calls == ['first', 'second']


def test_commit_failure_before_tool_prevents_external_action(db, monkeypatch):
    calls = []
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'execute_tool', evidence_tool(calls))
    task = new_task(db)
    persist = writer(db, task.id)
    def failed_write(snapshot):
        if (snapshot.get('pending_operation') or {}).get('kind') == 'tool':
            raise OSError('disk full')
        persist(snapshot)
    with pytest.raises(OSError, match='disk full'):
        list(loop.run_agent(db, 'Question', conversation_id='c_checkpoint', run_id=task.id, save_checkpoint=failed_write))
    assert calls == []
    assert db.query(QARecord).count() == 0


def test_crash_after_final_commit_resumes_delivery_without_duplicate_answer(db, monkeypatch):
    model, calls = Model(), []
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop, 'execute_tool', evidence_tool(calls))
    task = new_task(db)
    persist = writer(db, task.id)
    def crash_after_commit(snapshot):
        persist(snapshot)
        if snapshot['phase'] == 'deliver':
            raise PowerLoss()
    with pytest.raises(PowerLoss):
        list(loop.run_agent(db, 'Question', conversation_id='c_checkpoint', run_id=task.id, save_checkpoint=crash_after_commit))
    assert db.query(QARecord).count() == 1
    qa_id = db.query(QARecord).one().id
    answer = next(e for k, e in drive(db, task) if k == 'answer')
    assert answer['qa_id'] == qa_id
    assert calls == ['first', 'second']
    assert len(model.messages) == 2
    assert db.query(QARecord).count() == 1


def test_startup_reconciles_orphans_and_retains_checkpoint_and_progress(db):
    snapshot = AgentState(question='Question', conversation_id='c_checkpoint', phase='model').snapshot()
    task = new_task(db, checkpoint=snapshot, progress=0.6, plan=[{'title': 'Saved step', 'status': 'done'}])
    task.events = [{'kind': 'tool_result', 'name': 'saved result'}]
    db.commit()
    task_runner.recover_interrupted_tasks(db)
    db.refresh(task)
    assert task.status == 'interrupted'
    assert task.progress == 0.6
    assert task.checkpoint == snapshot
    assert task.events[0]['name'] == 'saved result'
    assert task_runner.task_to_dict(task)['recovery']['available']


def test_resume_preserves_old_work_and_restart_creates_new_record(db, monkeypatch):
    scheduled = []
    class Thread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            scheduled.append(self)
    monkeypatch.setattr(task_runner.threading, 'Thread', Thread)
    state = AgentState(question='Question', conversation_id='c_checkpoint', phase='model')
    task = new_task(db, checkpoint=state.snapshot(), progress=0.5)
    task.status = 'interrupted'
    task.events = [{'kind': 'tool_result', 'name': 'retained'}]
    db.commit()
    client = TestClient(app)
    assert client.post(f'/api/tasks/{task.id}/resume').status_code == 200
    assert client.post(f'/api/tasks/{task.id}/resume').status_code == 200
    assert len(scheduled) == 1, 'duplicate resume must not launch another worker'
    db.refresh(task)
    assert task.progress == 0.5
    assert task.events[0]['name'] == 'retained'
    task_runner._running.clear()
    task.status = 'cancelled'
    db.commit()
    response = client.post(f'/api/tasks/{task.id}/restart')
    assert response.status_code == 200
    assert response.json()['id'] != task.id
    db.refresh(task)
    assert task.status == 'cancelled'
    assert task.checkpoint == state.snapshot()


def test_old_or_invalid_checkpoint_is_explicitly_unavailable(db):
    task = new_task(db)
    client = TestClient(app)
    assert client.post(f'/api/tasks/{task.id}/resume').status_code == 409
    task.checkpoint = {'phase': 'completed', 'version': 99}
    db.commit()
    task_runner.recover_interrupted_tasks(db)
    db.refresh(task)
    assert task.status == 'interrupted'
    assert not task_runner.task_to_dict(task)['recovery']['available']
    assert client.post(f'/api/tasks/{task.id}/resume').status_code == 409


def test_unknown_operation_resume_api_requires_resolution(db):
    state = AgentState(question='Question', phase='tools', calls=[{'id': 'write', 'name': 'write_file', 'arguments': {}}], pending_operation={
        'key': 'tool/0/0', 'kind': 'tool', 'name': 'write_file', 'arguments': {'path': 'report.md'}})
    task = new_task(db, checkpoint=state.snapshot())
    response = TestClient(app).post(f'/api/tasks/{task.id}/resume')
    assert response.status_code == 409
    assert '未确认' in response.json()['detail']


def test_checkpoint_column_migrates_legacy_task_table_idempotently(monkeypatch):
    from app import db as database
    legacy = create_engine('sqlite://')
    with legacy.begin() as connection:
        connection.execute(text('CREATE TABLE tasks (id VARCHAR(32) PRIMARY KEY)'))
    monkeypatch.setattr(database, 'engine', legacy)
    database._lightweight_migrations()
    database._lightweight_migrations()
    with legacy.connect() as connection:
        assert 'checkpoint' in {row[1] for row in connection.execute(text('PRAGMA table_info(tasks)'))}
    legacy.dispose()


def test_shutdown_wakes_approval_and_resume_requests_new_permission(db, monkeypatch):
    model, calls = Model(guarded=True), []
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop, 'execute_tool', evidence_tool(calls))
    task = task_runner.start_agent_task(db, 'Question', None, [], 'c_checkpoint')
    def wait_approval():
        for _ in range(200):
            pending = approval_store.list_for_run(task['id'])
            if pending:
                return pending[0]
            time.sleep(0.01)
        pytest.fail('worker did not request permission')
    old_approval = wait_approval()
    task_runner.shutdown_background_tasks(timeout=2)
    assert task['id'] not in task_runner.running_task_ids()
    db.expire_all()
    stopped = db.get(TaskRecord, task['id'])
    assert stopped.status == 'interrupted'
    assert calls == []
    assert not approval_store.list_for_run(stopped.id)
    task_runner._shutting_down.clear()
    task_runner.resume_task(db, stopped.id)
    new_approval = wait_approval()
    assert new_approval['approval_id'] != old_approval['approval_id']
    assert approval_store.resolve(new_approval['approval_id'], 'approve')
    thread = task_runner._running.get(stopped.id)
    if thread:
        thread.join(3)
    assert not task_runner.running_task_ids()
    db.refresh(stopped)
    assert stopped.status == 'done'
    assert calls == ['first']
    assert db.query(QARecord).count() == 1


def test_online_citation_audit_accepts_the_shared_llm_response_type(db):
    from app.agent.audit import audit_answer
    class Auditor:
        def chat(self, *args, **kwargs):
            return LLMResponse('{"results": [{"index": 0, "supported": false, "reason": "Contradicted"}]}')
    audit = audit_answer(db, 'The model has 8 heads[^e1].', [
        {'id': 'e1', 'quote': 'The model has 4 heads.', 'paper_title': 'Reference'}], llm=Auditor())
    assert audit['passed'] is False
    assert audit['mode'] == 'llm'


def test_shutdown_timeout_keeps_running_work_and_deletion_guard(db, monkeypatch):
    from app.models import Conversation
    model = Model()
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return '{}'
    monkeypatch.setattr(loop, 'execute_tool', blocked)
    db.add(Conversation(id='c_checkpoint', title='Shutdown test'))
    db.commit()
    started = task_runner.start_agent_task(db, 'Question', None, [], 'c_checkpoint')
    assert entered.wait(2)
    try:
        task_runner.shutdown_background_tasks(timeout=0.01)
        assert started['id'] in task_runner.running_task_ids()
        db.expire_all()
        task = db.get(TaskRecord, started['id'])
        assert task.status == 'interrupted'
        assert task.checkpoint['pending_operation']['kind'] == 'tool'
        assert TestClient(app).delete('/api/conversations/c_checkpoint').status_code == 409
        assert TestClient(app).post('/api/tasks/start', json={'question': 'new'}).status_code == 503
    finally:
        thread = task_runner._running.get(started['id'])
        release.set()
        if thread:
            thread.join(3)
    assert not task_runner.running_task_ids()
    db.refresh(task)
    assert task.status == 'interrupted'
    assert task.checkpoint['pending_operation'] is None


@pytest.mark.parametrize('boundary,resolution', [
    ('after_tool', None), ('during_tool', 'skip'), ('post_hook', 'skip'), ('after_commit', None),
])
def test_real_process_exit_restores_saved_work_in_fresh_process(tmp_path, boundary, resolution):
    import os
    import subprocess
    import sys
    from pathlib import Path
    probe = Path(__file__).with_name('checkpoint_process_probe.py')
    environment = dict(os.environ, DATA_DIR=str(tmp_path), LLM_API_KEY='', EMBEDDING_API_KEY='',
                       REDIS_URL='', DESKTOP_NOTIFICATIONS='false')
    stopped = subprocess.run([sys.executable, str(probe), 'start', boundary], env=environment,
                              capture_output=True, text=True, timeout=20)
    assert stopped.returncode == 73, stopped.stdout + stopped.stderr
    resumed = subprocess.run([sys.executable, str(probe), 'resume', boundary, resolution or ''],
                              env=environment, capture_output=True, text=True, timeout=20)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    result = json.loads((tmp_path / 'probe-result.json').read_text())
    assert result['before_status'] == 'interrupted'
    assert result['status'] == 'done'
    assert result['qa_count'] == 1
    assert result['effects'] == ['first', 'second'], 'a saved or uncertain side effect was repeated'
    assert result['history_retained']
    if boundary == 'during_tool':
        assert result['unknown_result_recorded']
