from copy import deepcopy
import threading

import pytest

from app import task_runner, cancel_store
from app.agent import loop
from app.agent.checkpoint import AgentState, prepare_resume, recovery_info
from app.agent.active_context import ContextCapacityExceeded
from app.models import QARecord, TaskRecord
from app.resource_budget import ResourceBudgetExceeded
from app.request_resources import ResourceRequestCancelled
from app.llm import LLMResponse


class Model:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.error:
            raise self.error
        yield 'final', LLMResponse('保留成果并继续完成。')


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **k: {'ran': 0, 'blocked': False, 'feedback': ''})
    monkeypatch.setattr(task_runner.notifier, 'notify', lambda *a, **k: None)
    monkeypatch.setattr(task_runner, '_running', {})
    monkeypatch.setattr(task_runner, '_shutting_down', threading.Event())
    monkeypatch.setattr(cancel_store, '_flags', {})


@pytest.mark.parametrize('error,kind', [(ResourceBudgetExceeded('用户预算不足'), 'budget'), (ContextCapacityExceeded('当前模型上下文不足'), 'context')])
def test_model_resource_pause_has_no_false_answer_and_resumes_without_extra_step_cap(db, monkeypatch, error, kind):
    model, snapshots = Model(error), []
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    events = list(loop.run_agent(db, '继续已有任务', save_checkpoint=lambda state: snapshots.append(deepcopy(state))))
    assert [k for k, _ in events if k in ('paused', 'error', 'answer', 'done')] == ['paused']
    state = AgentState.model_validate(snapshots[-1])
    assert state.phase == 'model' and state.resource_pause['kind'] == kind
    assert state.step < state.max_steps and not state.budget_exhausted
    assert db.query(QARecord).count() == 0
    assert recovery_info(state.snapshot())['kind'] == 'resources'
    resumed, new_work = prepare_resume(state.snapshot())
    assert new_work and resumed.resource_pause is None
    assert resumed.messages == state.messages
    assert resumed.max_steps == state.max_steps
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    list(loop.run_agent(db, '继续已有任务', checkpoint=resumed.snapshot()))
    assert db.query(QARecord).count() == 1


def test_queue_cancellation_is_cancelled_not_provider_failure(db, monkeypatch):
    monkeypatch.setattr(loop, 'get_llm', lambda: Model(ResourceRequestCancelled('排队已取消')))
    events = list(loop.run_agent(db, '排队任务'))
    assert 'cancelled' in [k for k, _ in events]
    assert not any(k in ('answer', 'done', 'error', 'paused') for k, _ in events)


def test_background_projects_actual_resource_reason_not_round_limit(db, monkeypatch):
    monkeypatch.setattr(loop, 'get_llm', lambda: Model(ResourceBudgetExceeded('费用待核对，请先调整预算')))
    task = TaskRecord(type='agent', title='预算测试', conversation_id='resource-conv',
        payload={'question': '预算测试', 'skills': [], 'workspace': None}, status='pending', events=[])
    db.add(task); db.commit()
    task_runner._run_in_background(task.id, '预算测试', None, [], 'resource-conv')
    db.expire_all()
    current = db.get(TaskRecord, task.id)
    assert current.status == 'paused', current.error
    assert current.error == '费用待核对，请先调整预算'
    assert current.checkpoint['phase'] == 'model'
    assert current.result is None
    assert recovery_info(current.checkpoint)['available']


def test_paused_checkpoint_keeps_unknown_operation_and_requires_reconciliation():
    state = AgentState(question='整理资料', phase='tools', calls=[{'id': 'tool-call'}],
        pending_operation={'key': 'tool/0/0', 'kind': 'tool', 'name': 'remote-write', 'arguments': {}},
        resource_pause={'kind': 'budget', 'message': '预算不足'})
    info = recovery_info(state.snapshot())
    assert info['reason'] == '上一步执行结果待确认'
    resumed, _ = prepare_resume(state.snapshot())
    assert resumed.pending_operation == state.pending_operation
    assert resumed.phase == 'tools'
