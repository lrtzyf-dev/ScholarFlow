"""Opaque stage rollover at committed boundaries; no paid network requests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from sqlalchemy.orm import Session

from app.agent import loop, tools as tools_module
from app.agent.active_context import ContextCapacityExceeded, compact_messages, progress_fingerprint
from app.agent.checkpoint import AgentState, CheckpointNeedsReview, prepare_resume, recovery_info
from app.agent.memory_stage import renew_for_capacity, stage_progress
from app.config import settings
from app.llm import APIClient, LLMResponse
from app.models import QARecord
from app.resource_budget import ResourceBudgetExceeded

OPAQUE = 'native-private-signature-α-' * 5000
QUESTION = 'Preserve my original objective and produce the requested answer.'
WRITE = 'mcp__synthetic__write_once'
ARGS = {'path': 'result.txt', 'text': 'confirmed result'}
LIMITS = {'mode': 'demo', 'context_window_tokens': 30000, 'max_output_tokens': 1024,
          'source_revision_ids': [], 'source_bindings': []}
SPEC = {'name': WRITE, 'parameters': {'type': 'object', 'properties': {
    'path': {'type': 'string'}, 'text': {'type': 'string'}}}}


def native_response(call_id='native-1'):
    calls = [{'id': call_id, 'name': WRITE, 'arguments': ARGS}]
    return LLMResponse('', tool_calls=calls, provider_payload={'version': 1, 'scope': 'old-synthetic-scope',
        'protocol': 'openai_chat', 'assistant': {'role': 'assistant', 'server_tool_calls': [
            {'signature': OPAQUE, 'status': 'completed'}]}})


def committed_state(*, native=True):
    assistant = native_response().assistant_message()
    if not native:
        assistant.pop('_provider')
    return AgentState(question=QUESTION, phase='model', step=1, runtime_config=deepcopy(LIMITS),
        messages=[{'role': 'system', 'content': 'Trusted system policy.'},
                  {'role': 'user', 'content': QUESTION, '_task_root': True}, assistant,
                  {'role': 'tool', 'tool_call_id': 'native-1', 'content': '{"status":"completed"}'},
                  {'role': 'user', 'content': 'Do not change the requested output.', '_user_steering': 'correct-1'}],
        applied_input_ids=['correct-1'],
        operations={'tool/0/0': {'executed': True, 'error': '', 'result': '{"status":"completed"}'}},
        trace=[{'kind': 'tool_call', 'name': WRITE, 'arguments': ARGS},
               {'kind': 'tool_result', 'name': WRITE, 'operation_key': 'tool/0/0'}])


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(settings, 'approval_mode', 'full_access')
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **k: {'ran': 0, 'blocked': False, 'feedback': ''})
    monkeypatch.setattr(loop, 'get_tool_specs', lambda: [deepcopy(SPEC)])
    monkeypatch.setattr(loop, 'tool_risk', lambda name: 'write' if name == WRITE else 'safe')
    monkeypatch.setattr(tools_module, 'tool_risk', lambda name: 'write' if name == WRITE else 'safe')


def test_oversized_native_response_auto_renews_after_complete_batch_without_repeating_write(db, monkeypatch):
    calls, writes, snapshots = [], [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            calls.append(deepcopy(messages))
            if len(calls) == 1:
                yield 'final', native_response()
            elif len(calls) == 2:
                assert OPAQUE not in json.dumps(messages, ensure_ascii=False)
                assert QUESTION in json.dumps(messages)
                yield 'final', LLMResponse('', tool_calls=[{'id': 'different-id', 'name': WRITE, 'arguments': ARGS}])
            else:
                yield 'final', LLMResponse('The original result is preserved.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('once') or '{"status":"completed"}')
    events = list(loop.run_agent(db, QUESTION, runtime_config=deepcopy(LIMITS),
        save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    assert len(calls) == 3 and writes == ['once']
    assert any(k == 'status' and v.get('stage_reason') == 'native_capacity' for k, v in events)
    assert any(k == 'tool_result' and 'not_replayed' in v['preview'] for k, v in events)
    assert not any(k in ('error', 'paused') for k, _ in events)
    state = AgentState.model_validate(snapshots[-1])
    assert state.memory_generation == 1 and state.context_stage_progress
    db.flush()  # A supplied checkpoint writer owns the surrounding commit.
    assert state.phase == 'completed' and db.query(QARecord).count() == 1


def test_restart_after_stage_commit_preserves_receipts_and_corrections(db, monkeypatch):
    state, snapshots, requests, writes = committed_state(), [], [], []
    before = state.snapshot()
    class Model:
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            yield 'final', LLMResponse('Preserved output.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('unexpected'))
    stream = loop.run_agent(db, QUESTION, checkpoint=state.snapshot(), save_checkpoint=lambda s: snapshots.append(deepcopy(s)))
    for kind, value in stream:
        if kind == 'status' and value.get('stage_reason') == 'native_capacity':
            stream.close()  # Simulate a process ending after the committed handoff.
            break
    restored = json.loads(json.dumps(snapshots[-1]))
    assert requests == [] and restored['memory_generation'] == 1
    assert restored['operations'] == before['operations']
    assert restored['memory_stage_log'][0]['stage_digest'] == hashlib.sha256(
        json.dumps(before['messages'], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    with Session(db.get_bind()) as fresh:
        events = list(loop.run_agent(fresh, QUESTION, checkpoint=restored))
    assert writes == [] and len(requests) == 1
    assert 'Do not change the requested output.' in json.dumps(requests[0])
    assert OPAQUE not in json.dumps(requests[0], ensure_ascii=False)
    assert any(k == 'answer' for k, _ in events)


def test_actual_fresh_python_process_restores_stage_and_blocks_duplicate_output(db, tmp_path):
    state = committed_state()
    renew_for_capacity(db, state, None, reason='native_capacity')
    source, result = tmp_path / 'checkpoint.json', tmp_path / 'fresh-result.json'
    source.write_text(json.dumps(state.snapshot(), ensure_ascii=False), encoding='utf-8')
    probe = Path(__file__).with_name('native_stage_restart_probe.py')
    child = subprocess.run([sys.executable, str(probe), str(source), str(result)], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=45)
    assert child.returncode == 0, child.stderr[-4000:]
    observed = json.loads(result.read_text(encoding='utf-8'))
    assert observed['ok'] and observed['generation'] == 1 and observed['phase'] == 'completed'
    assert observed['context_stage_progress'] == state.context_stage_progress
    assert observed['duplicate_blocked'] and not observed['opaque_forwarded']
    assert observed['real_provider_calls'] == 0


def test_unknown_operation_requires_reconciliation_before_model_or_rebuild(db, monkeypatch):
    state, requests = committed_state(), []
    state.phase, state.calls, state.tool_index = 'tools', [{'id': 'unconfirmed', 'name': WRITE, 'arguments': ARGS}], 0
    state.pending_operation = {'key': 'tool/1/0', 'kind': 'tool', 'name': WRITE, 'arguments': ARGS}
    before = state.snapshot()
    monkeypatch.setattr(loop, 'get_llm', lambda: requests.append('model'))
    with pytest.raises(CheckpointNeedsReview):
        list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot()))
    assert state.snapshot() == before and requests == []
    assert recovery_info(state.snapshot())['pending_operation'] == state.pending_operation
    with pytest.raises(ContextCapacityExceeded, match='回执'):
        renew_for_capacity(db, state, None, reason='native_capacity')
    assert state.snapshot() == before


def test_skipped_unknown_receipt_migrates_across_restart_and_never_resends_with_new_call_id(db, monkeypatch):
    state, requests, writes, snapshots = committed_state(), [], [], []
    state.operations['tool/0/0'] = {'executed': False, 'error': '',
        'result': '{"execution_unknown":true,"skipped_by_user":true,"error":"Result remains unknown"}'}
    state.resource_pause = {'kind': 'budget', 'message': 'Earlier user budget'}
    resumed, _ = prepare_resume(json.loads(json.dumps(state.snapshot())))
    class Model:
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            if len(requests) == 1:
                yield 'final', LLMResponse('', tool_calls=[{'id': 'new-id-after-skip', 'name': WRITE, 'arguments': ARGS}])
            else:
                yield 'final', LLMResponse('The previous write needs manual verification.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('unsafe automatic replay'))
    with Session(db.get_bind()) as fresh:
        events = list(loop.run_agent(fresh, QUESTION, checkpoint=resumed.snapshot(),
            save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    assert writes == [] and len(requests) == 2
    blocked = [json.loads(value['preview']) for kind, value in events if kind == 'tool_result' and value['name'] == WRITE]
    assert blocked and blocked[0]['execution_unknown'] and blocked[0]['not_replayed']
    assert blocked[0]['already_executed'] is False
    assert any(value.get('execution_unknown') for value in snapshots[-1]['memory_side_effects'].values())
    assert '"execution_unknown": true' in json.dumps(requests[0], ensure_ascii=False).replace('\\"', '"')


def test_latest_authoritative_receipt_wins_over_older_unknown_during_migration(monkeypatch):
    from app.agent.memory_stage import recover_side_effect_identities
    state = committed_state()
    state.operations['tool/0/0']['result'] = '{"execution_unknown":true}'
    state.operations['tool/1/0'] = {'executed': True, 'error': '', 'result': '{"status":"completed"}'}
    state.trace.extend([{'kind': 'tool_call', 'name': WRITE, 'arguments': ARGS},
        {'kind': 'tool_result', 'name': WRITE, 'operation_key': 'tool/1/0'}])
    signature = hashlib.sha256(json.dumps([WRITE, ARGS], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    state.memory_side_effects[signature] = {'operation_key': 'tool/1/0', 'user_input_count': 0}
    recover_side_effect_identities(state)
    assert state.memory_side_effects[signature] == {'operation_key': 'tool/1/0', 'user_input_count': 0}
    state.operations['tool/2/0'] = {'executed': False, 'result': '{"execution_unknown":true}'}
    state.trace.extend([{'kind': 'tool_call', 'name': WRITE, 'arguments': ARGS},
        {'kind': 'tool_result', 'name': WRITE, 'operation_key': 'tool/2/0'}])
    recover_side_effect_identities(state)
    assert state.memory_side_effects[signature]['execution_unknown']
    assert state.memory_side_effects[signature]['operation_key'] == 'tool/2/0'


def test_skip_unknown_cannot_auto_repeat_even_without_capacity_renewal(db, monkeypatch):
    state, writes, requests, snapshots = committed_state(native=False), [], [], []
    pending_call = {'id': 'interrupted-write', 'name': WRITE, 'arguments': ARGS}
    state.messages.append(LLMResponse('', tool_calls=[pending_call]).assistant_message())
    state.phase, state.calls, state.tool_index = 'tools', [pending_call], 0
    state.pending_operation = {'key': 'tool/1/0', 'kind': 'tool', 'name': WRITE, 'arguments': ARGS}
    state.resolution = 'skip'
    class Model:
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            yield 'final', LLMResponse('', tool_calls=[{'id': 'different-id', 'name': WRITE, 'arguments': ARGS}]) if len(requests) == 1 else LLMResponse('The interrupted write remains unconfirmed.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('unsafe'))
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(), save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    assert not writes and len(requests) == 2 and snapshots[-1]['memory_generation'] == 0
    assert any(k == 'tool_result' and 'not_replayed' in value['preview'] for k, value in events)


def test_tool_reported_unknown_is_not_success_even_when_transport_returned_normally(db, monkeypatch):
    writes, requests, snapshots = [], [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            yield 'final', LLMResponse('', tool_calls=[{'id': f'unknown-{len(requests)}', 'name': WRITE, 'arguments': ARGS}]) if len(requests) < 3 else LLMResponse('Actual write result needs verification.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('first-call-only') or '{"execution_unknown":true}')
    events = list(loop.run_agent(db, QUESTION, runtime_config=deepcopy(LIMITS), save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    assert writes == ['first-call-only'] and len(requests) == 3
    assert snapshots[-1]['result']['completed'] is False
    assert any(f.get('execution_unknown') for f in snapshots[-1]['tool_failures'])
    assert any(k == 'tool_result' and 'not_replayed' in value['preview'] for k, value in events)


def test_capacity_preview_does_not_publish_memory_load_receipt(db):
    from app.conversation_memory import load_history
    from app.memory_governance_models import MemoryLoad
    from app.models import Conversation
    db.add(Conversation(id='native-memory', title='isolated context'))
    db.add(QARecord(id='native-memory-record', conversation_id='native-memory', question='old question', answer='old answer', in_memory=1))
    db.commit()
    _, info = load_history(db, 'native-memory', None, run_id='accepted-run')
    before = db.get(MemoryLoad, 'accepted-run').generation
    state = committed_state()
    state.conversation_id, state.memory_fingerprint = 'native-memory', info['fingerprint']
    state.messages[1]['content'] += 'preserve original requirements ' * 5000
    with pytest.raises(ContextCapacityExceeded):
        renew_for_capacity(db, state, 'accepted-run', reason='native_capacity')
    with Session(db.get_bind()) as fresh:
        assert fresh.get(MemoryLoad, 'accepted-run').generation == before
    assert state.memory_generation == 0 and state.context_stage_progress is None


def test_memory_change_during_native_candidate_build_keeps_original_stage(db, monkeypatch):
    from app.agent import memory_stage
    from app import memory_governance
    from app.conversation_memory import load_history
    from app.models import Conversation
    db.add(Conversation(id='native-race', title='isolated race'))
    db.add(QARecord(id='native-race-record', conversation_id='native-race', question='old', answer='private old', in_memory=1))
    db.commit()
    _, info = load_history(db, 'native-race', None, run_id='race-run')
    state = committed_state()
    state.conversation_id, state.memory_fingerprint = 'native-race', info['fingerprint']
    original, rebuild = deepcopy(state.messages), memory_stage.rebuild_stage
    def change_then_build(db, candidate, *a, **k):
        with Session(db.get_bind()) as other:
            memory_governance.mutate(other, 'native-race', 'native-race-record', expected_revision=0, action='delete')
        return rebuild(db, candidate, *a, **k)
    monkeypatch.setattr(memory_stage, 'rebuild_stage', change_then_build)
    with pytest.raises(memory_governance.MemoryScopeChanged):
        renew_for_capacity(db, state, 'race-run', reason='native_capacity')
    assert state.messages == original and state.memory_generation == 0


@pytest.mark.parametrize('incomplete', ['half_batch', 'orphan_call', 'orphan_result'])
def test_partial_protocol_batches_never_retire_or_replay(db, incomplete):
    state = committed_state()
    if incomplete == 'half_batch':
        state.phase, state.calls, state.tool_index = 'tools', [{'id': 'second'}], 0
    elif incomplete == 'orphan_call':
        state.messages = state.messages[:3]
    else:
        state.messages.insert(4, {'role': 'tool', 'tool_call_id': 'not-a-call', 'content': 'unknown'})
    before = state.snapshot()
    with pytest.raises(ContextCapacityExceeded, match='批次'):
        renew_for_capacity(db, state, None, reason='native_capacity')
    assert state.snapshot() == before


def test_rebuilt_root_still_too_large_pauses_once_without_dispatch_or_publishing_stage(db, monkeypatch):
    state, calls, snapshots = committed_state(), [], []
    state.messages[1]['content'] += 'immutable original requirement ' * 5000
    monkeypatch.setattr(loop, 'get_llm', lambda: calls.append('unexpected'))
    # Constructing a model object is permitted, dispatch is not.
    class Model:
        def stream_chat(self, *a, **k): calls.append('dispatch'); yield 'final', LLMResponse('wrong')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(), save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    assert calls == [] and [k for k, _ in events if k in ('paused', 'answer', 'error')] == ['paused']
    assert snapshots[-1]['memory_generation'] == 0
    assert snapshots[-1]['messages'] == state.messages
    assert snapshots[-1]['resource_pause']['kind'] == 'context'


def test_no_new_progress_native_overflow_pauses_after_one_auto_stage(db, monkeypatch):
    state, calls, snapshots = committed_state(), [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            calls.append(deepcopy(messages))
            yield 'final', native_response('new-call-id-with-same-write')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(), save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    assert len(calls) == 1
    assert [k for k, _ in events if k in ('paused', 'error', 'answer')] == ['paused']
    assert snapshots[-1]['memory_generation'] == 1
    assert '未取得新进展' in snapshots[-1]['resource_pause']['message']
    assert progress_fingerprint(WRITE, ARGS, {'already_executed': True, 'not_replayed': True}) is None


def test_real_new_results_unlock_a_later_stage_but_new_call_ids_do_not(db):
    state = committed_state()
    renew_for_capacity(db, state, None, reason='native_capacity')
    original_anchor = state.context_stage_progress
    state.operations['tool/99/0'] = {'executed': True, 'result': '{"already_executed":true}'}
    assert stage_progress(state) == original_anchor
    state.segment_result_fingerprints.append(progress_fingerprint('read_project_file', {'path': 'new.txt'}, 'new content'))
    assert stage_progress(state) != original_anchor
    renew_for_capacity(db, state, None, reason='native_capacity')
    assert state.memory_generation == 2


def test_budget_pause_after_renewal_resumes_without_replaying_output(db, monkeypatch):
    state, snapshots, requests, writes = committed_state(), [], [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            raise ResourceBudgetExceeded('Synthetic user budget reached')
            yield
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(), save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    saved = AgentState.model_validate(snapshots[-1])
    assert saved.resource_pause['kind'] == 'budget' and saved.memory_generation == 1
    assert len(requests) == 1 and any(k == 'paused' for k, _ in events)
    resumed, _ = prepare_resume(saved.snapshot())
    class Finish:
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            yield 'final', LLMResponse('', tool_calls=[{'id': 'retry', 'name': WRITE, 'arguments': ARGS}]) if len(requests) == 2 else LLMResponse('Done.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Finish())
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('unexpected'))
    with Session(db.get_bind()) as fresh:
        events = list(loop.run_agent(fresh, QUESTION, checkpoint=resumed.snapshot()))
    assert writes == [] and any(k == 'answer' for k, _ in events)
    assert all(OPAQUE not in json.dumps(request, ensure_ascii=False) for request in requests)


def test_private_identity_switch_retires_entire_stage_even_below_capacity(db, monkeypatch):
    state, requests = committed_state(), []
    state.messages[2]['_provider']['assistant']['server_tool_calls'][0]['signature'] = 'small-private-old-scope'
    class Model:
        def context_identity(self): return {'version': 1, 'protocol': 'other-protocol', 'scope': 'new-scope'}
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages)); yield 'final', LLMResponse('Done.')
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot()))
    assert len(requests) == 1 and 'small-private-old-scope' not in json.dumps(requests)
    assert not any(m.get('_provider') for m in requests[0])
    assert any(k == 'status' and v.get('stage_reason') == 'provider_identity' for k, v in events)


def test_fixed_evidence_positions_survive_without_forwarding_quote_or_provider_blocks(db):
    state = committed_state()
    state.runtime_config['source_revision_ids'] = ['allowed-revision']
    state.evidence = [{'id': 'e1', 'source_id': 'source1', 'revision_id': 'allowed-revision', 'locator_id': 'locator1',
        'quote': 'fixed original bytes', 'source_location': {'line_start': 12, 'line_end': 18, 'encrypted_content': 'never-forward'}},
        {'id': 'e2', 'revision_id': 'removed-revision', 'locator_id': 'removed-locator', 'quote': 'revoked'}]
    renew_for_capacity(db, state, None, reason='native_capacity')
    handoff = state.messages[1]['content']
    assert 'allowed-revision' in handoff and 'locator1' in handoff and 'line_start' in handoff
    assert 'fixed original bytes' not in handoff and 'never-forward' not in handoff and 'removed-locator' not in handoff
    assert hashlib.sha256(b'fixed original bytes').hexdigest() in handoff
    assert state.evidence[0]['quote'] == 'fixed original bytes'


def test_completed_delivery_never_rolls_over_or_calls_model(db, monkeypatch):
    state, calls = committed_state(), []
    state.phase, state.answer = 'deliver', 'Already committed output.'
    state.result = {'answer': state.answer, 'completed': True}
    monkeypatch.setattr(loop, 'get_llm', lambda: calls.append('construct'))
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot()))
    assert not any(k == 'paused' for k, _ in events)
    assert any(k == 'answer' for k, _ in events)
    assert state.memory_generation == 0


def test_direct_native_block_remains_immutable_when_capacity_check_requests_new_stage():
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'},
                {'role': 'assistant', 'content': 'answer', 'server_tool_calls': [{'signature': OPAQUE}]}]
    original = deepcopy(messages)
    with pytest.raises(ContextCapacityExceeded) as raised:
        compact_messages(messages, window_tokens=30000, output_reserve=1024)
    assert raised.value.stage_reason == 'native_capacity' and messages == original


def test_real_adapter_http_boundary_retires_oversized_private_payload_before_next_dispatch(db, monkeypatch):
    """Exercise actual wire adapter/admission/ledger, with synthetic HTTP only."""
    bodies, writes, snapshots = [], [], []
    class Client(APIClient):
        def _connection(self, reasoning_effort):
            return 'https://api.deepseek.com/v1', 'synthetic-stage-key', 'deepseek-flash', ''
        def stream_chat(self, messages, tools=None):
            yield 'final', self.chat(messages, tools=tools)
    model = Client(synthetic=True, context_limits=LIMITS)
    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            message = {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'actual-wire-call', 'type': 'function', 'function': {'name': WRITE, 'arguments': json.dumps(ARGS)}}],
                'server_tool_calls': [{'signature': OPAQUE, 'status': 'completed'}]}
        else:
            assert OPAQUE not in request.content.decode()
            assert not any('server_tool_calls' in m for m in body['messages'])
            message = {'role': 'assistant', 'content': 'The confirmed result remains available.'}
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'tool_calls' if len(bodies) == 1 else 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}})
    real = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda *a, **k: real(*a, transport=httpx.MockTransport(handle), **k))
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop, 'execute_tool', lambda *a, **k: writes.append('once') or '{"status":"completed"}')
    events = list(loop.run_agent(db, QUESTION, runtime_config=deepcopy(LIMITS),
        save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    # Source-attribution review may add an auxiliary metered call; the two
    # executor requests both have tools and only the first executes a write.
    assert len([body for body in bodies if body.get('tools')]) == 2 and writes == ['once']
    assert any(k == 'answer' for k, _ in events) and not any(k in ('paused', 'error') for k, _ in events)
    assert snapshots[-1]['memory_generation'] == 1
    from app.ledger_models import CallRecord
    with Session(db.get_bind()) as fresh:
        rows = fresh.query(CallRecord).all()
        assert len(rows) == len(bodies) and all(row.synthetic for row in rows)


@pytest.mark.parametrize('private_field', ['reasoning_content', 'reasoning', 'ordinary_content'])
def test_unbound_legacy_reasoning_retires_at_actual_api_boundary_without_text_keyword_rules(db, monkeypatch, private_field):
    state, bodies, snapshots = committed_state(native=False), [], []
    private = 'legacy-private-reasoning-with-no-provider-identity'
    if private_field == 'ordinary_content':
        state.messages[2]['content'] = 'Public prose mentions reasoning_content and reasoning without containing private fields.'
    else:
        state.messages[2][private_field] = private
    class Client(APIClient):
        def _connection(self, reasoning_effort):
            return 'https://api.deepseek.com/v1', 'synthetic-legacy-stage-key', 'deepseek-flash', ''
        def stream_chat(self, messages, tools=None):
            yield 'final', self.chat(messages, tools=tools)
    model = Client(synthetic=True, context_limits=LIMITS)
    def handle(request):
        body = json.loads(request.content); bodies.append(body)
        assert private not in request.content.decode()
        assert not any('reasoning_content' in m or 'reasoning' in m for m in body['messages'])
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Understood.'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 1, 'total_tokens': 6}})
    real = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda *a, **k: real(*a, transport=httpx.MockTransport(handle), **k))
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    events = list(loop.run_agent(db, QUESTION, checkpoint=state.snapshot(), save_checkpoint=lambda s: snapshots.append(deepcopy(s))))
    assert len([body for body in bodies if body.get('tools')]) == 1 and any(k == 'answer' for k, _ in events)
    expected = private_field != 'ordinary_content'
    assert bool(snapshots[-1]['memory_generation']) is expected
    assert any(k == 'status' and value.get('stage_reason') == 'provider_identity' for k, value in events) is expected
    if not expected:
        assert state.messages[2]['content'] in json.dumps(bodies[0])
