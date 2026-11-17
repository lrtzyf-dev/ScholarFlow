"""Isolated child-grant UI fixture, real ownership/CAS/SQLite, no HTTP models."""
from copy import deepcopy
from uuid import uuid4
from fastapi import Body
from sqlalchemy import select

from matrix_frontend_fixture import app, seed as matrix_seed
_existing_routes = list(app.router.routes)
from app import research_children as children, research_runs as runs, run_lifecycle, run_store
from app.agent.checkpoint import AgentState
from app.db import SessionLocal
from app.models import ForegroundRun
from app.research_models import ResearchMatrix, ResearchChildRetryGrant, ResearchChildLink
from app.ledger_models import CallRecord
from app.runtime_models import RuntimeRun, RunAttempt
from tests.test_research_children import parent, arguments, pending_checkpoint

# Admission and links remain production code; timing is explicitly controlled.
runs.execute = lambda *args, **kwargs: None


@app.post('/api/test-fixture/research-child/seed')
def seed():
    matrices = matrix_seed()
    values = {}
    for label in ('A', 'B'):
        with SessionLocal() as db:
            original = matrices[label]
            item = db.get(ResearchMatrix, original['matrix_id'])
            binding = parent(db, cid=original['cid'], name='chat_' + uuid4().hex,
                question='合成科研父任务 ' + label)
            args = arguments(item, 'ui-child-' + uuid4().hex)
            child = children.execute(children.START, args, binding=binding)
            db.expire_all()
            attempt = db.get(RunAttempt, child['attempt_id'])
            state = AgentState.model_validate(attempt.checkpoint)
            state.phase = 'model'
            state.pending_operation = {'key': 'controlled-ui-unknown', 'kind': 'tool',
                'name': 'matrix_query_embedding', 'arguments': {'synthetic': label}}
            attempt.checkpoint = state.snapshot()
            run_lifecycle.finish_attempt(db, child['run_id'], attempt.id, attempt.writer_token,
                'interrupted', '合成未知调用，未派发供应商请求')
            parent_attempt = db.get(RunAttempt, binding['attempt_id'])
            parent_state = pending_checkpoint(db, binding, args)
            parent_state['phase'] = 'model'
            parent_attempt.checkpoint = parent_state
            run_lifecycle.finish_attempt(db, binding['run_id'], parent_attempt.id,
                parent_attempt.writer_token, 'interrupted', '等待用户核对科研子任务')
            db.add(ForegroundRun(id=binding['run_id'], conversation_id=original['cid'],
                question='合成科研父任务 ' + label, paper_id=None, status='interrupted', phase='model',
                agent_checkpoint=parent_state, runtime_config=binding))
            db.commit()
            run_store.finish(binding['run_id'], 'interrupted', attempt_id=parent_attempt.id)
            values[label] = {'cid': original['cid'], 'parent_id': binding['run_id'],
                'child_id': child['run_id'], 'matrix_id': item.id}
    return values


@app.post('/api/test-fixture/research-child/{child_id}/advance')
def advance(child_id: str, body: dict = Body(default={})):
    """Controlled replacement identity, not a claim of a real model resumption."""
    with SessionLocal() as db:
        link = db.scalar(select(ResearchChildLink).where(ResearchChildLink.child_run_id == child_id))
        child = db.get(RuntimeRun, child_id)
        old = db.get(RunAttempt, child.current_attempt_id)
        checkpoint = deepcopy(old.checkpoint)
        checkpoint['pending_operation']['arguments']['generation'] = uuid4().hex
        fresh = RunAttempt(id=uuid4().hex, run_id=child_id, number=old.number + 1,
            writer_token=uuid4().hex, status='interrupted', checkpoint=checkpoint, result={})
        db.add(fresh); db.flush()
        checkpoint['attempt_id'] = fresh.id
        checkpoint['writer_token'] = fresh.writer_token
        fresh.checkpoint = checkpoint
        child.current_attempt_id = fresh.id; child.revision += 1
        parent_run = db.get(RuntimeRun, link.parent_run_id)
        if body.get('parent_revision', True): parent_run.revision += 1
        db.commit()
        return {'child_attempt_id': fresh.id, 'parent_revision': parent_run.revision}


@app.get('/api/test-fixture/research-child/stats')
def stats():
    with SessionLocal() as db:
        return {'grants': db.query(ResearchChildRetryGrant).count(),
            'attempts': db.query(RunAttempt).count(), 'ledger_calls': db.query(CallRecord).count(),
            'real_provider_calls': 0}


app.router.routes[:] = [route for route in app.router.routes if route not in _existing_routes] + _existing_routes
