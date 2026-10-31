"""Crash a disposable subprocess at a real durable execution boundary."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import task_runner
from app.agent import loop
from app.config import DATA_DIR
from app.db import SessionLocal, init_db
from app.llm import LLMResponse
from app.models import QARecord, TaskRecord

action, boundary = sys.argv[1:3]
resolution = (sys.argv[3] or None) if len(sys.argv) > 3 else None
init_db()
effects_path = DATA_DIR / 'probe-effects.txt'
id_path = DATA_DIR / 'probe-task-id.txt'


def crash():
    os._exit(73)


class Model:
    def stream_chat(self, messages, tools=None):
        if not any(m['role'] == 'tool' for m in messages):
            yield 'final', LLMResponse('', tool_calls=[
                {'id': 'first', 'name': 'list_papers', 'arguments': {'query': 'first'}},
                {'id': 'second', 'name': 'list_papers', 'arguments': {'query': 'second'}},
            ])
        else:
            yield 'final', LLMResponse('Process restart verification complete.')


def tool(db, name, args, **kwargs):
    with effects_path.open('a', encoding='utf-8') as log:
        log.write(args['query'] + '\n')
        log.flush()
        os.fsync(log.fileno())
    if action == 'start' and boundary == 'during_tool' and args['query'] == 'first':
        crash()
    return json.dumps({'result': args['query']})


def hooks(event, *args, **kwargs):
    if action == 'start' and boundary == 'post_hook' and event == 'PostToolUse':
        crash()
    return {'ran': 0, 'blocked': False, 'feedback': ''}


original = loop.run_agent
def agent(*args, **kwargs):
    persist = kwargs['save_checkpoint']
    def checkpoint(snapshot):
        persist(snapshot)
        if action == 'start' and boundary == 'after_commit' and snapshot['phase'] == 'deliver':
            crash()
    kwargs['save_checkpoint'] = checkpoint
    for kind, event in original(*args, **kwargs):
        yield kind, event
        if action == 'start' and boundary == 'after_tool' and kind == 'tool_result':
            crash()

loop.get_llm = lambda: Model()
loop.make_plan = lambda *args: [{'title': 'Search', 'phase': 'read', 'status': 'pending'},
                                {'title': 'Write', 'phase': 'write', 'status': 'pending'}]
loop.execute_tool = tool
loop.hooks_store.run_hooks = hooks
task_runner.run_agent = agent
task_runner.notifier.notify = lambda *a, **k: None

with SessionLocal() as db:
    if action == 'start':
        task = TaskRecord(type='agent', title='Process checkpoint probe', status='pending',
            conversation_id='c_process_probe', payload={'question': 'Process probe'}, events=[])
        db.add(task)
        db.commit()
        id_path.write_text(task.id)
        task_runner._run_in_background(task.id, 'Process probe', None, [], 'c_process_probe')
        raise AssertionError('expected process interruption did not occur')
    else:
        task_id = id_path.read_text()
        task_runner.recover_interrupted_tasks(db)
        task = db.get(TaskRecord, task_id)
        before_status, old_events = task.status, list(task.events or [])
        task_runner.resume_task(db, task_id, resolution)
        thread = task_runner._running.get(task_id)
        if thread:
            thread.join(8)
        assert not task_runner.running_task_ids()
        db.refresh(task)
        result = {'before_status': before_status, 'status': task.status,
            'qa_count': db.query(QARecord).count(), 'effects': effects_path.read_text().splitlines(),
            'history_retained': all(event in (task.events or []) for event in old_events),
            'unknown_result_recorded': any('execution_unknown' in m.get('content', '')
                for m in (task.checkpoint or {}).get('messages', []) if m.get('role') == 'tool')}
        (DATA_DIR / 'probe-result.json').write_text(json.dumps(result))
