"""Disposable Vue/HTTP matrix fixtures. No external transmission or model calls."""
from copy import deepcopy
from uuid import uuid4
import hashlib
from fastapi import Body
from sqlalchemy import select, text

from app.main import app
_original_routes = list(app.router.routes)
from app import matrix_run, research_runs as runs, research_store as matrices, source_store as sources, run_lifecycle
from app.agent.checkpoint import AgentState
from app.db import SessionLocal
from app.models import Conversation, Paper
from app.ledger_models import CallRecord
from app.research_models import ResearchMatrix
from app.runtime_models import RuntimeRun, RunAttempt
from app.source_models import SourceLibraryBinding, SourceLibraryImport

# Actual API creation/resume still uses real transactions and fixed snapshots.
# Explicit fixture execution controls timing without opening any model service.
runs.start = lambda *args, **kwargs: None
FIELDS = [{"key": "method", "label": "研究方法", "query": "method performance"}]


def new_run(db, item):
    rid = "research_" + uuid4().hex
    db.rollback(); db.execute(text("BEGIN IMMEDIATE"))
    _, attempt, _ = runs.create(db, kind="matrix", run_id=rid, conversation_id=item.conversation_id,
        request_id="ui-" + uuid4().hex, inputs={"matrix_id": item.id, "expected_version": item.version,
            "field_keys": None, "paper_ids": None})
    db.commit()
    return rid, attempt.id


@app.post('/api/test-fixture/matrix/seed')
def seed():
    result = {}
    with SessionLocal() as db:
        for label in ('A', 'B'):
            cid = 'matrix-ui-' + label + '-' + uuid4().hex[:10]
            db.add(Conversation(id=cid, title='合成矩阵会话 ' + label))
            paper_ids = []
            for number in range(2):
                raw = ('# Synthetic method\nThe method compares model performance ' + label + str(number) + '.\n').encode()
                paper = Paper(title='合成文献 ' + label + str(number), status='parsed', content_hash=hashlib.sha256(raw).hexdigest())
                db.add(paper); db.flush()
                scope = sources.SourceScope('library', paper.id)
                fixed = sources.capture_source(db, scope=scope,
                    authorization=sources.SourceAuthorization(scope, 'library:' + paper.id, 'library'),
                    kind='library', title=paper.title, raw_bytes=raw, text=raw.decode(), media_type='text/markdown')
                db.add_all([SourceLibraryBinding(paper_id=paper.id, revision_id=fixed['revision_id']),
                    SourceLibraryImport(revision_id=fixed['revision_id'], paper_id=paper.id,
                        raw_sha256=paper.content_hash, request_id='ui-' + paper.id)])
                paper_ids.append(paper.id)
            db.flush()
            item = matrices.create_matrix(db, cid, '矩阵 ' + label, paper_ids, deepcopy(FIELDS)); db.commit()
            rid, attempt = new_run(db, item)
            result[label] = {'cid': cid, 'matrix_id': item.id, 'papers': paper_ids, 'run_id': rid, 'attempt_id': attempt}
        result['real_provider_calls'] = 0
    return result


@app.post('/api/test-fixture/matrix/{run_id}/execute')
def execute(run_id: str):
    with SessionLocal() as db:
        attempt = run_lifecycle.current_attempt(db, run_id)
        aid = attempt.id
    runs.execute(run_id, aid)
    with SessionLocal() as db:
        run = db.get(RuntimeRun, run_id)
        return runs.public(db, run_id, run.conversation_id, expected_kind='matrix')


@app.post('/api/test-fixture/matrix/{run_id}/interrupt')
def interrupt(run_id: str, body: dict = Body(default={})):
    with SessionLocal() as db:
        run = db.get(RuntimeRun, run_id); attempt = run_lifecycle.current_attempt(db, run_id)
        if body.get('unknown'):
            state = AgentState.model_validate(attempt.checkpoint)
            state.pending_operation = {'key': 'synthetic-ui-unknown', 'kind': 'tool', 'name': 'matrix_query_embedding', 'arguments': {'synthetic': True}}
            attempt.checkpoint = state.snapshot()
        run_lifecycle.finish_attempt(db, run_id, attempt.id, attempt.writer_token, 'interrupted', '合成中断，未发送外部请求')
        db.commit()
        return runs.public(db, run_id, run.conversation_id, expected_kind='matrix')


@app.get('/api/test-fixture/matrix/stats')
def stats():
    with SessionLocal() as db:
        return {'runs': db.query(RuntimeRun).filter_by(kind='matrix').count(),
            'attempts': db.query(RunAttempt).count(), 'matrices': db.query(ResearchMatrix).count(),
            'ledger_calls': db.query(CallRecord).count(), 'real_provider_calls': 0}


app.router.routes[:] = [route for route in app.router.routes if route not in _original_routes] + _original_routes
