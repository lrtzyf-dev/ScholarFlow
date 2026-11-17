"""Synthetic usage receipts for local UI acceptance, no provider is called."""
from uuid import uuid4
from app.main import app
_original_routes = list(app.router.routes)
from app.db import SessionLocal
from app.models import Conversation
from app.runtime_models import RuntimeRun
from app import resource_budget, call_ledger
from app.request_resources import request_admission


@app.post('/api/test-fixture/pricing/seed')
def seed():
    cid, run_id = uuid4().hex, uuid4().hex
    with SessionLocal.begin() as db:
        db.add(Conversation(id=cid, title='费用核对合成测试'))
        db.add(RuntimeRun(id=run_id, kind='foreground', conversation_id=cid))
    bid = resource_budget.create_budget(owner_run_id=run_id, amount_limit='3', request_estimate='1', price_version='fixture')
    with request_admission(provider='pricing.test', endpoint='https://pricing.test/v1/chat/completions',
            runtime_config={'resource_budget_id': bid, 'run_id': run_id, 'conversation_id': cid}) as admission:
        call_id = call_ledger.begin_call(provider='pricing.test', endpoint='https://pricing.test/v1/chat/completions',
            service='chat', model_id='Exact-Model', run_id=run_id)
        admission.bind_call(call_id)
        call_ledger.finish_call(call_id, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    return {'conversation_id': cid, 'run_id': run_id, 'budget_id': bid, 'call_id': call_id, 'real_provider_calls': 0}


app.router.routes[:] = [route for route in app.router.routes if route not in _original_routes] + _original_routes
