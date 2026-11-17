from datetime import datetime
from fastapi.testclient import TestClient
from app.main import app
from app.models import Conversation,QARecord

client=TestClient(app)


def test_history_windows_keep_exact_persistent_ids_trace_and_cross_scope_anchor_private(db):
    db.add_all([Conversation(id='window-a'),Conversation(id='window-b')])
    for i in range(101):
        db.add(QARecord(id=f'window-{i:03}',conversation_id='window-a',question=f'question {i}',answer=f'answer {i}',created_at=datetime(2026,1,1),
            trace=[{'kind':'commentary','text':f'event {i}'}]))
    db.add(QARecord(id='foreign',conversation_id='window-b',question='private',answer='private'));db.commit()
    newest=client.get('/api/conversations/window-a?limit=30').json()
    assert newest['history_window']=={'total':101,'offset':71,'limit':30,'previous_offset':41,'next_offset':None}
    assert newest['messages'][0]['id']=='window-071'
    earlier=client.get('/api/conversations/window-a?limit=30&offset=41').json()
    assert earlier['messages'][-1]['id']=='window-070'
    assert not {m['id'] for m in earlier['messages']} & {m['id'] for m in newest['messages']}
    anchor=client.get('/api/conversations/window-a?limit=30&anchor=window-025').json()
    assert anchor['history_window']['offset']==10
    record=next(m for m in anchor['messages'] if m['id']=='window-025')
    assert record['trace']==[{'kind':'commentary','text':'event 25'}]
    foreign=client.get('/api/conversations/window-a?limit=30&anchor=foreign').json()
    assert foreign==newest
    assert len(client.get('/api/conversations/window-a').json()['messages'])==101,'legacy consumers remain compatible'
    assert client.get('/api/conversations/window-a?limit=101').status_code==422


def test_plan_claims_are_bounded_and_scoped_without_answer_bodies(db):
    db.add_all([Conversation(id='claims-a'),Conversation(id='claims-b')])
    db.add_all([QARecord(id='plan-a',conversation_id='claims-a',execution_mode='plan',plan_run_id='run-a',answer='private body'),
        QARecord(id='plan-b',conversation_id='claims-b',execution_mode='plan',plan_run_id='run-b')]);db.commit()
    result=client.post('/api/conversations/claims-a/plan-claims',json={'record_ids':['plan-a','plan-b']})
    assert result.json()=={'items':[{'id':'plan-a','plan_run_id':'run-a'}]}
    assert client.post('/api/conversations/claims-a/plan-claims',json={'record_ids':['plan-a']*101}).status_code==422
