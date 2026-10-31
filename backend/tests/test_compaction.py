"""Persistent history coverage, accounting, privacy, cancellation and source reuse."""
import json
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import cancel_store, conversation_memory as memory, run_store, task_runner
from app.agent import loop
from app.config import settings
from app.db import SessionLocal
from app.llm import LLMResponse
from app.main import app
from app.models import CompactionCall, Conversation, ConversationMemory, QARecord
from app.agent.tools import execute_tool

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(settings, 'memory_recall', True)
    monkeypatch.setattr(settings, 'llm_api_key', '')
    monkeypatch.setattr(settings, 'llm_base_url', '')
    for name in ('_active', '_finished', '_early_cancels'):
        monkeypatch.setattr(run_store, name, {})
    monkeypatch.setattr(cancel_store, '_flags', {})
    monkeypatch.setattr(task_runner, '_running', {})
    monkeypatch.setattr(task_runner, '_shutting_down', threading.Event())


def seed(db, cid='c_long', count=24, start=0):
    if not db.get(Conversation, cid):
        db.add(Conversation(id=cid, title='Long memory'))
    rows = []
    for i in range(start, start + count):
        row = QARecord(id=f'{cid}_{i:04}', conversation_id=cid, question=f'Question {i}: constraint-{i}',
                       answer=f'Value {i + 0.125} mg. Earlier evidence [^e1].', in_memory=1,
                       evidence=[dict(id='e1', paper_id='p_source', paper_title='Verified paper', page=i + 1,
                                      chunk_id=f'chunk_{i}', quote=f'Value {i + 0.125} mg.')],
                       created_at=datetime(2026, 1, 1) + timedelta(minutes=i))
        rows.append(row)
        db.add(row)
    db.commit()
    return rows


class Model:
    def __init__(self, *, usage=True):
        self.inputs = []
        self.with_usage = usage
    def chat(self, messages, tools=None, **kwargs):
        data = json.loads(messages[-1]['content'])
        self.inputs.append(data)
        # Deterministically preserve the first known fact and newest record source.
        first = data['previous_summary'] or data['new_history'][0][:500]
        summary = first[:1200] + '\n' + data['new_history'][-1][:500]
        return LLMResponse(summary, usage={'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130} if self.with_usage else None)


def online(monkeypatch, model):
    monkeypatch.setattr(settings, 'llm_api_key', 'test-only')
    monkeypatch.setattr(settings, 'llm_base_url', 'http://127.0.0.1:1')
    monkeypatch.setattr('app.llm.get_llm', lambda: model)


def test_all_older_turns_incremental_cache_survives_fresh_session_and_counts_once(db, monkeypatch):
    seed(db)
    model = Model()
    online(monkeypatch, model)
    messages, info = loop._load_history(db, 'c_long', llm=model)
    assert info == {**info, 'total': 24, 'summarized': 20, 'mode': 'model'}
    assert '0.125 mg' in messages[0]['content']
    assert '[历史证据:c_long_0000:e1]' in messages[0]['content']
    assert messages[0]['role'] == 'user', 'historical data must not become a system instruction'
    assert len(messages) == 9 and 'Question 20' in messages[1]['content']
    assert '[^e1]' not in messages[-1]['content']
    first_calls = len(model.inputs)
    with SessionLocal() as fresh:
        cached, _ = loop._load_history(fresh, 'c_long', llm=model)
        assert cached == messages
        assert len(model.inputs) == first_calls
    seed(db, count=2, start=24)
    _, info = loop._load_history(db, 'c_long', llm=model)
    assert info['summarized'] == 22
    newly_processed = '\n'.join(s for call in model.inputs[first_calls:] for s in call['new_history'])
    assert 'c_long_0020' in newly_processed and 'c_long_0021' in newly_processed
    assert 'c_long_0000' not in newly_processed
    db.expire_all()
    state = memory.snapshot(db, 'c_long')
    assert state['covered_turns'] == 22 and state['pending_turns'] == 0
    assert state['usage']['usage']['total_tokens'] == len(model.inputs) * 130
    total = client.get('/api/conversations/usage/summary').json()
    assert total['usage']['total_tokens'] == state['usage']['usage']['total_tokens']


def test_large_originals_are_processed_in_bounded_batches_without_silent_tail_drop(db, monkeypatch):
    rows = seed(db, count=5)
    rows[0].answer = '头部' + ('中段数据' * 12000) + 'IMPORTANT_TAIL_987'
    db.commit()
    model = Model()
    online(monkeypatch, model)
    state = memory.compact(db.get_bind(), 'c_long', model)
    assert state['covered_turns'] == 1
    bodies = [body for call in model.inputs for body in call['new_history']]
    assert 'IMPORTANT_TAIL_987' in ''.join(bodies)
    assert all(sum(map(len, call['new_history'])) <= memory.BATCH_CHARS for call in model.inputs)
    assert all(len(call['previous_summary']) <= memory.SUMMARY_CHARS for call in model.inputs)
    original = memory.read_history(db, 'c_long', {'record_id': rows[0].id})
    parts = [original['text']]
    while original['next_offset']:
        original = memory.read_history(db, 'c_long', {'record_id': rows[0].id, 'offset': original['next_offset']})
        parts.append(original['text'])
    assert 'IMPORTANT_TAIL_987' in ''.join(parts)


def test_source_privacy_mutation_invalidates_summary_and_original_recall_is_scoped(db, monkeypatch):
    rows = seed(db, count=8)
    other = seed(db, 'c_other', count=1)[0]
    model = Model()
    online(monkeypatch, model)
    memory.compact(db.get_bind(), 'c_long', model)
    rows[0].in_memory = 0
    db.commit()
    db.expire_all()
    state = memory.snapshot(db, 'c_long')
    assert state['stale'] and not state['summary']
    memory.compact(db.get_bind(), 'c_long', model)
    db.expire_all()
    assert rows[0].id not in {item['id'] for item in memory.snapshot(db, 'c_long')['sources']}
    assert 'error' in memory.read_history(db, 'c_long', {'record_id': rows[0].id})
    assert 'error' in memory.read_history(db, 'c_long', {'record_id': other.id, 'conversation_id': 'c_other'})
    monkeypatch.setattr(settings, 'memory_recall', False)
    assert loop._load_history(db, 'c_long', llm=model)[0] == []
    assert 'error' in memory.read_history(db, 'c_long', {'record_id': rows[1].id})


def test_removing_all_compactable_sources_clears_invalid_cache(db):
    rows = seed(db, count=6)
    memory.compact(db.get_bind(), 'c_long', None)
    rows[0].in_memory = rows[1].in_memory = 0
    db.commit()
    messages, info = loop._load_history(db, 'c_long')
    assert info['summarized'] == 0 and len(messages) == 8


def test_recall_disabled_while_summarizing_prevents_history_injection(db, monkeypatch):
    seed(db, count=5)
    class SwitchOff(Model):
        def chat(self, *args, **kwargs):
            result = super().chat(*args, **kwargs)
            settings.memory_recall = False
            return result
    model = SwitchOff()
    online(monkeypatch, model)
    messages, info = loop._load_history(db, 'c_long', llm=model)
    assert messages == [] and info['recall_off']
    assert memory.usage_summary(db)['usage']['total_tokens'] == 130


@pytest.mark.parametrize('failure', ['exception', 'empty', 'oversized', 'tool'])
def test_failed_summary_is_labelled_and_preserves_original_with_honest_usage(db, monkeypatch, failure):
    seed(db, count=5)
    class BadModel:
        def chat(self, *args, **kwargs):
            if failure == 'exception': raise TimeoutError('unavailable')
            return LLMResponse('x' * 9000 if failure == 'oversized' else '' if failure == 'empty' else 'unsafe',
                               tool_calls=[{'name': 'write'}] if failure == 'tool' else None)
    model = BadModel()
    online(monkeypatch, model)
    result = memory.compact(db.get_bind(), 'c_long', model)
    assert result['mode'] == 'degraded' and result['covered_turns'] == 1
    assert db.query(QARecord).count() == 5
    assert result['usage']['unknown_calls'] == (1 if failure == 'exception' else 0)
    assert result['usage']['estimated_calls'] == (0 if failure == 'exception' else 1)
    assert result['usage']['provider_calls'] == 0


def test_manual_concurrency_stopping_and_delete_guard_leave_no_stale_summary(db, monkeypatch):
    seed(db, count=6)
    entered, release = threading.Event(), threading.Event()
    class Blocking(Model):
        def chat(self, *args, **kwargs):
            entered.set()
            assert release.wait(8)
            return super().chat(*args, **kwargs)
    model = Blocking()
    online(monkeypatch, model)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: client.post('/api/conversations/c_long/memory/compact', json={'expected_revision': 0}))
        try:
            assert entered.wait(5)
            assert client.get('/api/conversations/c_long/memory').json()['busy']
            assert client.post('/api/conversations/c_long/memory/compact', json={'expected_revision': 0}).status_code == 409
            assert client.delete('/api/conversations/c_long').status_code == 409
            assert client.put('/api/conversations/c_long/archive', json={'archived': True, 'expected_revision': 0}).status_code == 409
            assert client.post('/api/conversations/c_long/memory/cancel').status_code == 200
        finally:
            release.set()
        response = future.result(timeout=5)
    assert response.status_code == 409 and '停止' in response.text
    db.expire_all()
    assert db.get(ConversationMemory, 'c_long') is None
    assert memory.usage_summary(db)['usage']['total_tokens'] == 130, 'returned usage is saved even if cancelled'
    assert not run_store.active_runs()


def test_simultaneous_auto_compaction_calls_model_once(db, monkeypatch):
    seed(db, count=6)
    model = Model()
    online(monkeypatch, model)
    bind = db.get_bind()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: memory.compact(bind, 'c_long', model), range(2)))
    assert len(model.inputs) == 1
    assert results[0]['revision'] == results[1]['revision']


def test_partial_commit_resumes_only_unfinished_input_and_old_content_change_rebuilds(db, monkeypatch):
    rows = seed(db, count=11)
    for row in rows:
        row.answer += ' Original detail.' * 700
    db.commit()
    class Interrupted(Model):
        def chat(self, *args, **kwargs):
            if self.inputs: raise KeyboardInterrupt('simulated process exit')
            return super().chat(*args, **kwargs)
    interrupted = Interrupted()
    online(monkeypatch, interrupted)
    with pytest.raises(KeyboardInterrupt):
        memory.compact(db.get_bind(), 'c_long', interrupted)
    with SessionLocal() as fresh:
        saved = fresh.get(ConversationMemory, 'c_long')
        assert saved and saved.sources
        prefix = len(saved.sources)
    successor = Model()
    result = memory.compact(db.get_bind(), 'c_long', successor)
    assert result['covered_turns'] == 7
    expected = memory._units(rows[:-4])[prefix:]
    assert [body for call in successor.inputs for body in call['new_history']] == [body for _, body in expected]
    assert result['usage']['unknown_calls'] == 1
    rows[0].answer = 'Corrected value 42 mg.'
    db.commit()
    db.expire_all()
    assert memory.snapshot(db, 'c_long')['stale']
    rebuilt = memory.compact(db.get_bind(), 'c_long', successor)
    assert 'Corrected value 42 mg' in rebuilt['summary']


def test_revision_conflict_fork_rebuild_archive_and_delete_accounting(db, monkeypatch):
    rows = seed(db, count=8)
    model = Model()
    online(monkeypatch, model)
    first = client.post('/api/conversations/c_long/memory/compact', json={'expected_revision': 0})
    assert first.status_code == 200
    revision = first.json()['revision']
    assert client.post('/api/conversations/c_long/memory/compact', json={'expected_revision': 0, 'force': True}).status_code == 409
    count = len(model.inputs)
    assert client.post('/api/conversations/c_long/memory/compact', json={'expected_revision': revision}).status_code == 200
    assert len(model.inputs) == count
    fork = client.post('/api/conversations/c_long/fork', json={'after_message_id': rows[5].id}).json()['id']
    assert client.get(f'/api/conversations/{fork}/memory').json()['summary'] == ''
    fork_memory = client.post(f'/api/conversations/{fork}/memory/compact', json={'expected_revision': 0}).json()
    assert fork_memory['covered_turns'] == 2
    assert all(item['id'].startswith('qa_') for item in fork_memory['sources'])
    assert client.put('/api/conversations/c_long/archive', json={'archived': True, 'expected_revision': 0}).status_code == 200
    assert client.get('/api/conversations/c_long/memory').json()['summary']
    assert client.post('/api/conversations/c_long/memory/compact', json={'expected_revision': revision}).status_code == 409
    before = client.get('/api/conversations/usage/summary').json()['usage']
    assert client.delete('/api/conversations/c_long').status_code == 200
    db.expire_all()
    assert db.get(ConversationMemory, 'c_long') is None
    assert client.get('/api/conversations/c_long/memory').status_code == 410
    assert client.get('/api/conversations/usage/summary').json()['usage'] == before


def test_scoped_history_tool_returns_original_evidence_for_current_run_numbering(db):
    seed(db, count=16)
    search = json.loads(execute_tool(db, 'read_conversation_history', {'query': 'constraint-0'}, conversation_id='c_long'))
    assert search['records'][0]['record_id'] == 'c_long_0000'
    first = memory.read_history(db, 'c_long', {})
    assert len(first['records']) == 10 and first['next_offset'] == 10
    read = execute_tool(db, 'read_conversation_history', {'record_id': 'c_long_0000', 'evidence_id': 'e1'}, conversation_id='c_long')
    result, counter, evidence = loop._renumber_evidence(read, 7)
    assert counter == 8 and evidence[0]['id'] == 'e8'
    assert evidence[0]['quote'] == 'Value 0.125 mg.' and evidence[0]['page'] == 1
    assert '[历史证据:c_long_0000:e1]' in json.loads(result)['text']
    assert 'error' in json.loads(execute_tool(db, 'read_conversation_history', {'record_id': 'c_long_0000'}))


def test_plan_auto_compaction_never_executes_hooks_and_history_tool_stays_readonly(db, monkeypatch):
    seed(db, count=6)
    class Planner:
        def stream_chat(self, messages, tools=None):
            if not any(item['role'] == 'tool' for item in messages):
                yield 'final', LLMResponse('', [{'id': 'old', 'name': 'read_conversation_history',
                                                'arguments': {'record_id': 'c_long_0000', 'evidence_id': 'e1'}}])
            else:
                yield 'final', LLMResponse('Plan after reading saved history. Nothing has been executed.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Planner())
    monkeypatch.setattr(loop, 'make_plan', lambda *args: [])
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **kw: pytest.fail('planning executed a hook'))
    events = list(loop.run_agent(db, 'Plan', conversation_id='c_long', execution_mode='plan'))
    assert any(kind == 'tool_result' and data['name'] == 'read_conversation_history' for kind, data in events)
    assert not any(kind == 'policy_block' for kind, _ in events)
    db.expire_all()
    assert db.get(ConversationMemory, 'c_long').mode == 'extractive'
    assert db.query(CompactionCall).count() == 0
