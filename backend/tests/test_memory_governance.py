"""Durable memory ownership, revocation and actual request-boundary coverage."""
from contextlib import contextmanager
from copy import deepcopy
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import conversation_memory as history, memory_governance as memory
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.agent.memory_stage import ensure_current
from app.config import settings
from app.db import SessionLocal
from app.llm import APIClient, LLMResponse
from app.main import app
from app.memory_governance_models import MemoryItem, MemoryLoad
from app.models import Conversation, ConversationMemory, DeletedConversation, QARecord
from app.runtime_context import bind_runtime_config
from app.timeutil import utcnow

client = TestClient(app)
SECRET = 'revoked historical material α-secret'


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(settings, 'memory_recall', True)
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **k: {'ran': 0, 'blocked': False, 'feedback': ''})


def seed(db, cid='memory-conv', n=1):
    db.add(Conversation(id=cid, title='Memory test'))
    rows = [QARecord(id=f'{cid}-{i}', conversation_id=cid, question=f'Question {i}',
                     answer=SECRET if i == 0 else f'Answer {i}', in_memory=1) for i in range(n)]
    db.add_all(rows); db.commit()
    return rows


def change(db, rid, action, revision=0, text=None, cid='memory-conv'):
    return memory.mutate(db, cid, rid, expected_revision=revision, action=action, replacement=text)


def test_edits_exclusion_and_tombstone_preserve_original_and_survive_fresh_session(db):
    row = seed(db)[0]
    change(db, row.id, 'edit', text='User approved replacement')
    assert row.answer == SECRET
    assert history.read_history(db, row.conversation_id, {'record_id': row.id})['text'].endswith('User approved replacement')
    change(db, row.id, 'exclude', 1)
    assert memory.effective_rows(db, row.conversation_id) == []
    change(db, row.id, 'include', 2)
    assert memory.effective_rows(db, row.conversation_id)[0].answer == 'User approved replacement'
    change(db, row.id, 'delete', 3)
    with SessionLocal() as fresh:
        assert fresh.get(QARecord, row.id).answer == SECRET
        assert fresh.get(MemoryItem, row.id).state == 'deleted'
        for _ in range(2):
            history.compact(fresh.get_bind(), row.conversation_id, None, force=True)
            assert history.load_history(fresh, row.conversation_id, None)[0] == []
        assert history.read_history(fresh, row.conversation_id, {'record_id': row.id}).get('error')
        assert memory.items(fresh, row.conversation_id)['items'][0]['text'] == ''
        change(fresh, row.id, 'include', 4)
        assert memory.effective_rows(fresh, row.conversation_id)[0].answer == SECRET


def test_cas_scope_archived_deleted_and_request_validation(db):
    row = seed(db)[0]
    assert client.post(f'/api/memory/{row.conversation_id}/items/{row.id}', json={
        'action': 'edit', 'expected_revision': 0, 'text': '  corrected  '}).status_code == 200
    assert client.post(f'/api/memory/{row.conversation_id}/items/{row.id}', json={
        'action': 'delete', 'expected_revision': 0}).status_code == 409
    seed(db, 'other')
    assert client.post(f'/api/memory/other/items/{row.id}', json={
        'action': 'delete', 'expected_revision': 0}).status_code == 404
    conv = db.get(Conversation, row.conversation_id); conv.archived_at = utcnow(); db.commit()
    assert client.get(f'/api/memory/{conv.id}/items').status_code == 200
    assert client.post(f'/api/memory/{conv.id}/items/{row.id}', json={
        'action': 'delete', 'expected_revision': 1}).status_code == 409
    db.add(DeletedConversation(id=conv.id)); db.commit()
    assert client.get(f'/api/memory/{conv.id}/items').status_code == 410


def test_actual_load_receipt_has_exact_ids_modes_no_copied_text_and_handles_legacy_attempt(db):
    rows = seed(db, n=7)
    messages, info = history.load_history(db, 'memory-conv', None, run_id='legacy-background-run')
    with SessionLocal() as fresh:
        receipt = fresh.get(MemoryLoad, 'legacy-background-run')
        assert receipt.attempt_id is None
        assert receipt.fingerprint == info['fingerprint']
        assert [e['id'] for e in receipt.entries] == [r.id for r in rows]
        assert [e['mode'] for e in receipt.entries] == ['summary'] * 3 + ['recent'] * 4
        assert SECRET not in json.dumps(receipt.entries, ensure_ascii=False)
        assert set(receipt.entries[0]) == {'id', 'digest', 'mode', 'edited'}
        assert memory.items(fresh, 'memory-conv')['loads'][0]['stale'] is False
    change(db, rows[0].id, 'delete')
    assert memory.items(db, 'memory-conv')['loads'][0]['stale'] is True
    assert SECRET in json.dumps(messages, ensure_ascii=False)


def test_compaction_in_flight_recent_edit_cannot_publish_stale_summary(db):
    rows = seed(db, n=6)
    class ChangingModel:
        def chat(self, *args, **kwargs):
            with SessionLocal() as other:
                change(other, rows[-1].id, 'edit', text='Recent replacement')
            return LLMResponse('Unaccepted summary containing ' + SECRET)
    with bind_runtime_config({'mode': 'api'}), pytest.raises(history.MemoryConflict):
        history.compact(db.get_bind(), 'memory-conv', ChangingModel())
    with SessionLocal() as fresh:
        assert fresh.get(ConversationMemory, 'memory-conv') is None


def test_next_round_rebuilds_whole_stage_keeps_operations_and_never_repeats_write(db, monkeypatch):
    row = seed(db)[0]
    snapshots, inputs, writes = [], [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            inputs.append(deepcopy(messages))
            if len(inputs) == 1:
                yield 'final', LLMResponse('', tool_calls=[{'id': 'once', 'name': 'mcp__test__save', 'arguments': {}}])
            else:
                yield 'final', LLMResponse('已保留成果并完成当前请求。')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'get_tool_specs', lambda: [{'name': 'mcp__test__save', 'parameters': {'type': 'object'}}])
    monkeypatch.setattr(loop, 'tool_risk', lambda name: 'safe')
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('side effect') or json.dumps({'saved': True}))
    events = []
    for kind, value in loop.run_agent(db, '当前任务保留', conversation_id='memory-conv',
                                      save_checkpoint=lambda s: snapshots.append(deepcopy(s))):
        events.append((kind, value))
        if kind == 'tool_result':
            with SessionLocal() as other:
                change(other, row.id, 'delete')
    assert len(inputs) == 2
    assert SECRET in json.dumps(inputs[0], ensure_ascii=False) and SECRET not in json.dumps(inputs[1], ensure_ascii=False)
    assert '当前任务保留' in json.dumps(inputs[1], ensure_ascii=False)
    assert writes == ['side effect']
    assert snapshots[-1]['memory_generation'] == 1
    assert len([k for k in snapshots[-1]['operations'] if k.startswith('tool/')]) == 1
    assert any(k == 'memory' and 'generation' in v for k, v in events)
    assert any(k == 'answer' for k, _ in events)


def test_stage_discards_opaque_envelopes_and_archive_preserves_user_correction_and_unknown_receipt(db):
    row = seed(db)[0]
    _, info = history.load_history(db, 'memory-conv', None)
    envelope = {'assistant': {'server_tool_calls': [{'signed_payload': SECRET}], 'content': SECRET}}
    state = AgentState(question='original task', conversation_id='memory-conv', phase='model',
        memory_fingerprint=info['fingerprint'], messages=[{'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': 'original task', '_task_root': True},
            {'role': 'assistant', 'content': SECRET, '_provider': envelope},
            {'role': 'user', 'content': 'do not write files', '_user_steering': 'explicit-input'}],
        context_archive={'old': {'content': SECRET}}, readonly_cache={'old': {'content': SECRET}},
        operations={'tool/0/0': {'executed': True, 'result': SECRET}},
        todo=[{'title': SECRET, 'phase': 'write', 'status': 'pending'}])
    old_envelope = deepcopy(envelope)
    change(db, row.id, 'delete')
    assert ensure_current(db, state, None)
    assert envelope == old_envelope  # Native block is discarded, never patched.
    assert SECRET not in json.dumps(state.messages, ensure_ascii=False)
    assert 'do not write files' in json.dumps(state.messages)
    assert state.operations['tool/0/0']['result'] == SECRET
    assert state.context_archive == state.readonly_cache == {} and state.todo == []
    state.pending_operation = {'key': 'tool/1/0', 'kind': 'tool', 'name': 'write', 'arguments': {}}
    change(db, row.id, 'include', 1)
    saved = state.snapshot()
    with pytest.raises(memory.MemoryScopeChanged): ensure_current(db, state, None)
    assert state.snapshot() == saved


def test_provider_queue_revocation_fence_never_reaches_http(db, monkeypatch):
    row = seed(db)[0]
    history.load_history(db, 'memory-conv', None, run_id='queued-main')
    from app import request_resources
    admission = request_resources.request_admission
    @contextmanager
    def edit_after_queue(**kwargs):
        with admission(**kwargs) as receipt:
            with SessionLocal() as other: change(other, row.id, 'delete')
            yield receipt
    monkeypatch.setattr(request_resources, 'request_admission', edit_after_queue)
    monkeypatch.setattr(settings, 'llm_base_url', 'https://memory.example.test/v1')
    monkeypatch.setattr(settings, 'llm_api_key', 'synthetic-memory-test-key')
    monkeypatch.setattr(settings, 'llm_model', 'Case-Sensitive-Test')
    dispatched = []
    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda *a, **k: real_client(*a,
        transport=httpx.MockTransport(lambda request: dispatched.append(request) or httpx.Response(200, json={})), **k))
    with bind_runtime_config({'conversation_id': 'memory-conv', 'run_id': 'queued-main'}), pytest.raises(memory.MemoryScopeChanged):
        APIClient(synthetic=True).chat([{'role': 'user', 'content': SECRET}])
    assert dispatched == []


def test_partial_tool_batch_can_rebuild_without_replaying_completed_or_reusing_operation_keys(db):
    row = seed(db)[0]
    _, info = history.load_history(db, 'memory-conv', None)
    state = AgentState(question='current', conversation_id='memory-conv', phase='tools', step=3,
        calls=[{'id': 'done'}, {'id': 'not-started'}], tool_index=1,
        memory_fingerprint=info['fingerprint'],
        messages=[{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current', '_task_root': True},
                  {'role': 'assistant', 'content': SECRET, 'tool_calls': [{'id': 'done'}, {'id': 'not-started'}]},
                  {'role': 'tool', 'tool_call_id': 'done', 'content': SECRET}],
        operations={'tool/3/0': {'executed': True, 'result': 'written once'}})
    change(db, row.id, 'delete')
    assert ensure_current(db, state, None)
    assert state.step == 4 and state.calls == [] and state.tool_index == 0
    assert state.operations == {'tool/3/0': {'executed': True, 'result': 'written once'}}
    assert state.phase == 'model' and SECRET not in json.dumps(state.messages, ensure_ascii=False)


def test_new_model_call_id_after_rebuild_cannot_repeat_confirmed_side_effect(db, monkeypatch):
    row = seed(db)[0]
    calls, writes = [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            calls.append(deepcopy(messages))
            if len(calls) <= 2:
                yield 'final', LLMResponse('', tool_calls=[{'id': f'new-id-{len(calls)}', 'name': 'mcp__test__write_once',
                    'arguments': {'path': 'result.txt', 'text': 'new task output'}}])
            else: yield 'final', LLMResponse('已有成果已保留。')
    model = Model()
    monkeypatch.setattr(settings, 'approval_mode', 'full_access')
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop, 'get_tool_specs', lambda: [{'name': 'mcp__test__write_once', 'parameters': {'type': 'object'}}])
    monkeypatch.setattr(loop, 'tool_risk', lambda name: 'write')
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('once') or json.dumps({'status': 'completed'}))
    changed, events = False, []
    for kind, value in loop.run_agent(db, '写入一次当前结果', conversation_id='memory-conv'):
        events.append((kind, value))
        if kind == 'tool_result' and not changed:
            with SessionLocal() as other: change(other, row.id, 'delete')
            changed = True
    assert writes == ['once'] and len(calls) == 3
    assert SECRET not in json.dumps(calls[1:], ensure_ascii=False)
    assert any(k == 'tool_result' and 'not_replayed' in v['preview'] for k, v in events)


def test_legacy_success_trace_migrates_to_hash_receipt_without_returning_raw_arguments(db, monkeypatch):
    from app.agent import tools
    from app.agent.memory_stage import recover_side_effect_identities
    monkeypatch.setattr(tools, 'tool_risk', lambda name: 'write')
    state = AgentState(question='legacy', phase='model',
        operations={'tool/0/0': {'executed': True, 'result': '{"status":"completed"}'}},
        trace=[{'kind': 'tool_call', 'name': 'write', 'arguments': {'text': SECRET}},
               {'kind': 'tool_result', 'name': 'write', 'operation_key': 'tool/0/0'}])
    recover_side_effect_identities(state)
    assert len(state.memory_side_effects) == 1
    assert list(state.memory_side_effects.values()) == [{'operation_key': 'tool/0/0', 'user_input_count': 0}]
    assert SECRET not in json.dumps(state.memory_side_effects, ensure_ascii=False)


@pytest.mark.parametrize('action', ['exclude', 'delete', 'edit'])
def test_fork_and_history_revision_copy_memory_policy_without_changing_chat(db, action):
    rows = seed(db, n=2)
    change(db, rows[0].id, action, text='replacement' if action == 'edit' else None)
    fork = client.post('/api/conversations/memory-conv/fork', json={'after_message_id': rows[1].id})
    assert fork.status_code == 200
    from app.history_revisions import create_revision
    revision = create_revision(db, 'memory-conv', rows[1].id, 'new question', 'answer_only', 'memory-revision-test')
    db.commit()
    for cid in [fork.json()['id'], revision['branch_conversation_id']]:
        with SessionLocal() as fresh:
            original = fresh.query(QARecord).filter_by(conversation_id=cid).order_by(QARecord.created_at, QARecord.id).first()
            assert original.answer == SECRET
            item = fresh.get(MemoryItem, original.id)
            assert item.state == ('included' if action == 'edit' else action + 'd' if action == 'exclude' else 'deleted')
            effective = memory.effective_rows(fresh, cid)
            assert all(r.answer != SECRET for r in effective)


def test_recall_off_midrun_rebuilds_empty_history_and_child_fingerprint_does_not_adopt_new_policy(db, monkeypatch):
    row = seed(db)[0]
    _, info = history.load_history(db, 'memory-conv', None, run_id='parent')
    binding = memory.inherit_loaded_memory(db, {'run_id': 'parent', 'conversation_id': 'memory-conv'})
    assert binding['memory_fingerprint'] == info['fingerprint']
    monkeypatch.setattr(settings, 'memory_recall', False)
    with pytest.raises(memory.MemoryScopeChanged):
        memory.assert_request_memory({**binding, 'subtask_id': 'child'})
    state = AgentState(question='current', phase='model', conversation_id='memory-conv',
        memory_fingerprint=info['fingerprint'], messages=[{'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': 'current', '_task_root': True}, {'role': 'assistant', 'content': SECRET}])
    assert ensure_current(db, state, 'parent')
    assert SECRET not in json.dumps(state.messages, ensure_ascii=False)
    with SessionLocal() as fresh:
        assert fresh.get(MemoryLoad, 'parent').entries == []
